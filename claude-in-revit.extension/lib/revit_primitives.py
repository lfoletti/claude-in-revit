"""revit_primitives.py — thin wrappers around the Revit API.

Two responsibilities for V0:

1. **Transactions.** The Revit API's `Transaction` needs `Start()`/`Commit()`/
   `RollBack()` bookkeeping that's easy to fumble (especially on exception
   paths). `transaction(doc, name)` is a context manager that commits on
   success, rolls back on exception, and re-raises so callers see the
   original stack trace. It nests cleanly inside `kg.transaction()` so the
   `@kg_synced` decorator (kg_sync.py, next phase) can pair the two and
   undo both sides if either fails — the atomicity guarantee of §4.1.

2. **Unit conversions.** Revit's internal length unit is feet. The LLM and
   our KG speak metres (Swiss/European project, design doc §11). The Revit
   2025 API uses the post-2022 `UnitTypeId` (ForgeTypeId) style; the older
   `DisplayUnitType` was deprecated and shouldn't be used here.

Plus a handful of `FilteredElementCollector` shortcuts that will be
re-used by every Revit-backed tool. Lookups return raw Revit `Element`
objects — converting them to KG-shaped dicts is `kg_sync.py`'s job, not
this module's.

**This module is Revit-only.** The imports below pull on `Autodesk.Revit.DB`,
which only lives inside Revit's process. Don't import it from the
hors-Revit pytest harness; it will raise `ImportError`. The pushbutton
scripts run under pyRevit/CPython where PythonNet has pre-loaded the Revit
assemblies, so no explicit `clr.AddReference("RevitAPI")` is needed.
"""
from __future__ import annotations

from contextlib import contextmanager

from Autodesk.Revit.DB import (  # noqa: F401  (Element re-exported for callers)
    BuiltInCategory,
    CurveElement,
    DetailCurve,
    Element,
    ExternalDefinitionCreationOptions,
    FilteredElementCollector,
    GroupTypeId,
    Level,
    ModelCurve,
    SpecTypeId,
    Transaction,
    UnitTypeId,
    UnitUtils,
)


# ----- Transactions ------------------------------------------------------


@contextmanager
def transaction(doc, name):
    """Open a Revit `Transaction`, commit on success, roll back on exception.

    Args:
        doc: The Revit `Document` (typically `UIDocument.Document` inside
            pyRevit; available via `__revit__.ActiveUIDocument.Document`).
        name: Human-readable transaction name (appears in Revit's Undo stack).

    Re-raises any exception after rollback, so callers see the original
    stack trace. Guards against double-end states (a nested commit that
    already closed the transaction, or a transaction that was never
    started) via `HasStarted()` / `HasEnded()`.
    """
    t = Transaction(doc, name)
    t.Start()
    try:
        yield t
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise


# ----- Unit conversions --------------------------------------------------


def meters_to_internal(m):
    """Convert a length in metres → Revit's internal unit (feet)."""
    return UnitUtils.ConvertToInternalUnits(float(m), UnitTypeId.Meters)


def internal_to_meters(feet):
    """Convert a length from Revit's internal unit (feet) → metres."""
    return UnitUtils.ConvertFromInternalUnits(float(feet), UnitTypeId.Meters)


def sqm_to_internal(area_m2):
    """Convert an area in m² → Revit's internal unit (square feet)."""
    return UnitUtils.ConvertToInternalUnits(float(area_m2), UnitTypeId.SquareMeters)


def internal_to_sqm(area_sf):
    """Convert an area from Revit's internal sq feet → m²."""
    return UnitUtils.ConvertFromInternalUnits(float(area_sf), UnitTypeId.SquareMeters)


# ----- Collectors --------------------------------------------------------


def get_element_or_raise(doc, eid_or_raw, llm_id, kind="element"):
    """Resolve `eid_or_raw` → live `Element`. Raise `ValueError` if stale.

    `doc.GetElement(ElementId)` retourne `None` silencieusement quand
    l'ElementId pointe vers un élément supprimé hors-pipeline (user qui
    delete via l'UI Revit pendant qu'on a un KG cache, ou suppression
    précédente non synchronisée). Sans ce garde-fou, le crash se traduit
    en `AttributeError: NoneType` (sur le premier `get_Parameter`),
    diagnostic LLM imprécis. Ici on remonte un message actionnable
    qui dit explicitement « run Refresh KG ».

    Args:
        doc: Revit Document.
        eid_or_raw: `ElementId` ou `int` brut (Revit-side value).
        llm_id: llm_id KG de l'élément (pour le message d'erreur).
        kind: type d'élément pour le message ("window", "wall", …).

    Returns:
        Le `Element` résolu (non-None garanti).
    """
    from Autodesk.Revit.DB import ElementId as _ElementId
    if isinstance(eid_or_raw, int):
        eid = _ElementId(eid_or_raw)
        raw_value = eid_or_raw
    else:
        eid = eid_or_raw
        raw_value = eid.Value
    element = doc.GetElement(eid)
    if element is None:
        raise ValueError(
            "Revit binding stale for {} {} (ElementId {}): element "
            "not found in document. Run Refresh KG to purge orphan "
            "KG nodes, then retry.".format(kind, llm_id, raw_value)
        )
    return element


def collect_by_category(doc, builtin_category):
    """All non-type elements in a `BuiltInCategory`.

    Wrapped in `list()` so the collector enumerator is materialised once —
    Revit collectors are one-shot iterators and re-iteration silently
    returns no results.
    """
    return list(
        FilteredElementCollector(doc)
        .OfCategory(builtin_category)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def collect_types_by_category(doc, builtin_category):
    """All *type* elements (WallType, FamilySymbol, etc.) in a category."""
    return list(
        FilteredElementCollector(doc)
        .OfCategory(builtin_category)
        .WhereElementIsElementType()
        .ToElements()
    )


def walls(doc):
    """All `Wall` instances in the model."""
    return collect_by_category(doc, BuiltInCategory.OST_Walls)


def wall_types(doc):
    """All `WallType` definitions in the model."""
    return collect_types_by_category(doc, BuiltInCategory.OST_Walls)


def levels(doc):
    """All `Level` elements.

    Uses `OfClass(Level)` rather than `OfCategory(OST_Levels)` because the
    `OST_Levels` category filter doesn't always match in 2024+; `OfClass`
    is the documented stable filter for Level lookups.
    """
    return list(FilteredElementCollector(doc).OfClass(Level).ToElements())


def _curve_elements(doc):
    """All `CurveElement` instances (parent class of ModelCurve / DetailCurve).

    Why this indirection: Revit refuses `OfClass(ModelCurve)` and
    `OfClass(DetailCurve)` at runtime with `ArgumentException` —
    *"element type that exists in the API, but not in Revit's native
    object model"*. The official workaround (per the exception message
    itself) is to filter on `CurveElement` (their concrete parent in the
    native model) and post-filter in Python via `isinstance`.
    """
    return list(FilteredElementCollector(doc).OfClass(CurveElement).ToElements())


def model_lines(doc):
    """All `ModelCurve` instances (3D model lines).

    Returns subclasses too (`ModelLine`, `ModelArc`, `ModelHermiteSpline`,
    …) — V0 callers filter to straight `Line` geometry via `.GeometryCurve`
    at conversion time and skip the rest.
    """
    return [e for e in _curve_elements(doc) if isinstance(e, ModelCurve)]


def detail_lines(doc):
    """All `DetailCurve` instances (view-bound 2D detail lines).

    Subclass-inclusive (same rationale as `model_lines`). Detail curves
    are view-specific — the KG drops the view binding for V0; the agent
    gets endpoints but doesn't know which view the line was drawn in.
    """
    return [e for e in _curve_elements(doc) if isinstance(e, DetailCurve)]


def columns(doc):
    """All column instances (architectural + structural).

    Returns elements from both `OST_Columns` (architectural) and
    `OST_StructuralColumns` (structural). The caller distinguishes
    them via `Category.Id.Value` at conversion time.
    """
    arch = collect_by_category(doc, BuiltInCategory.OST_Columns)
    struct = collect_by_category(doc, BuiltInCategory.OST_StructuralColumns)
    return arch + struct


def column_types(doc):
    """All column FamilySymbols (architectural + structural)."""
    arch = collect_types_by_category(doc, BuiltInCategory.OST_Columns)
    struct = collect_types_by_category(doc, BuiltInCategory.OST_StructuralColumns)
    return arch + struct


def floors(doc):
    """All `Floor` (sol / dalle) instances. Excludes FloorType definitions."""
    return collect_by_category(doc, BuiltInCategory.OST_Floors)


def floor_types(doc):
    """All `FloorType` definitions in the model."""
    return collect_types_by_category(doc, BuiltInCategory.OST_Floors)


def rooms(doc):
    """All `Room` instances (`OST_Rooms`).

    Returns placed AND unplaced rooms — an unplaced room has `Location is None`
    and `area == 0`, but still lives in the document and is reachable by id.
    Converters / tools filter them out when they need a placed instance
    (e.g. `_room_to_attrs` reads `.Location.Point` defensively).
    """
    return collect_by_category(doc, BuiltInCategory.OST_Rooms)


def doors(doc):
    """All hosted door instances (OST_Doors). Excludes door FamilySymbols."""
    return collect_by_category(doc, BuiltInCategory.OST_Doors)


def door_types(doc):
    """All door FamilySymbols (OST_Doors type elements)."""
    return collect_types_by_category(doc, BuiltInCategory.OST_Doors)


def windows(doc):
    """All hosted window instances (OST_Windows). Excludes window FamilySymbols."""
    return collect_by_category(doc, BuiltInCategory.OST_Windows)


def window_types(doc):
    """All window FamilySymbols (OST_Windows type elements)."""
    return collect_types_by_category(doc, BuiltInCategory.OST_Windows)


# ----- Opening dimensions (Height / Width on FamilySymbol) ---------------
#
# Door / Window families expose their opening dimensions through one of
# several parameters depending on vendor / vintage / language. We cascade
# through the standard BuiltInParameters first (most reliable when they
# apply), then fall back to `LookupParameter` with usual French/English
# names. All public helpers below are no-op-safe: failed reads return
# `None`, failed writes return `False`. Callers (the `openings_*` tools)
# treat absence as a feature-not-supported on this family, not a fatal
# error.

# Try-list order matters : Window/Door-specific BIPs ahead of the
# generic FAMILY_HEIGHT_PARAM, so we land on the more semantic one when
# both are present.
_HEIGHT_BIPS = (
    "WINDOW_HEIGHT",
    "DOOR_HEIGHT",
    "FAMILY_HEIGHT_PARAM",
    "GENERIC_HEIGHT",
)
_WIDTH_BIPS = (
    "WINDOW_WIDTH",
    "DOOR_WIDTH",
    "FAMILY_WIDTH_PARAM",
    "GENERIC_WIDTH",
)
_HEIGHT_NAMES = ("Height", "Hauteur", "Hauteur d'ouverture")
_WIDTH_NAMES = ("Width", "Largeur", "Largeur d'ouverture")


def _try_bip_param(symbol, bip_name):
    """Return the symbol's Parameter for `bip_name`, or None if missing.

    `bip_name` is a string ("WINDOW_HEIGHT") rather than the enum value so
    a missing BIP entry on this Revit version doesn't raise at import —
    we resolve dynamically and fall through on AttributeError.
    """
    from Autodesk.Revit.DB import BuiltInParameter
    bip = getattr(BuiltInParameter, bip_name, None)
    if bip is None:
        return None
    try:
        return symbol.get_Parameter(bip)
    except Exception:  # noqa: BLE001
        return None


def _read_dim_m(symbol, bip_names, lookup_names):
    """Cascade through BIPs then LookupParameter names to read a length
    in metres from `symbol`. Returns None if nothing resolved or the
    parameter doesn't carry a numeric value."""
    for name in bip_names:
        p = _try_bip_param(symbol, name)
        if p is None:
            continue
        try:
            return internal_to_meters(p.AsDouble())
        except Exception:  # noqa: BLE001
            continue
    for label in lookup_names:
        try:
            p = symbol.LookupParameter(label)
        except Exception:  # noqa: BLE001
            continue
        if p is None:
            continue
        try:
            return internal_to_meters(p.AsDouble())
        except Exception:  # noqa: BLE001
            continue
    return None


def _set_dim_m(symbol, value_m, bip_names, lookup_names):
    """Cascade-write a length (metres) onto `symbol`. Returns True on a
    confirmed set, False if no parameter accepted the write (read-only
    or absent). Silent on every exception path — these are type-side
    parameters that vary wildly across families."""
    feet = meters_to_internal(value_m)
    for name in bip_names:
        p = _try_bip_param(symbol, name)
        if p is None or p.IsReadOnly:
            continue
        try:
            if bool(p.Set(feet)):
                return True
        except Exception:  # noqa: BLE001
            continue
    for label in lookup_names:
        try:
            p = symbol.LookupParameter(label)
        except Exception:  # noqa: BLE001
            continue
        if p is None or p.IsReadOnly:
            continue
        try:
            if bool(p.Set(feet)):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def opening_read_height_m(symbol):
    """Read the opening's height (m) from a FamilySymbol (type element).
    Returns None if no compatible parameter is found."""
    return _read_dim_m(symbol, _HEIGHT_BIPS, _HEIGHT_NAMES)


def opening_read_width_m(symbol):
    """Read the opening's width (m) from a FamilySymbol. Returns None
    when no parameter exposes it on this family."""
    return _read_dim_m(symbol, _WIDTH_BIPS, _WIDTH_NAMES)


def opening_set_height(symbol, height_m):
    """Set the opening height (m) on a FamilySymbol. Returns True iff a
    compatible writable parameter was found and accepted the value.
    Must be inside an open Revit transaction."""
    return _set_dim_m(symbol, height_m, _HEIGHT_BIPS, _HEIGHT_NAMES)


def opening_set_width(symbol, width_m):
    """Set the opening width (m) on a FamilySymbol. Returns True iff a
    compatible writable parameter was found and accepted the value.
    Must be inside an open Revit transaction."""
    return _set_dim_m(symbol, width_m, _WIDTH_BIPS, _WIDTH_NAMES)


# ----- Shared parameter: claude-in-revit:llm_id --------------------------
#
# Surface UX visible dans le panneau Propriétés de Revit pour que
# l'utilisateur lise le `llm_id` d'un élément qu'il clique dans le modèle.
# **Le KG reste la source de vérité** du mapping `llm_id ↔ revit_id` :
# le param est écrit DEPUIS le KG (`set_llm_id_on_element`) après chaque
# `bind()` ou création, jamais lu PAR le KG en flow normal. La lecture
# (`get_llm_id_from_element`) sert uniquement de fallback de récupération
# si le KG est absent ou corrompu.
#
# Binding Instance sur **toutes les catégories acceptant un paramètre lié**
# (Settings.Categories.AllowsBoundParameters): anticipe les types futurs
# (Doors, Windows, Rooms, Floors, Roofs, Beams, etc.) sans avoir à
# re-binder à chaque ajout. GUID stable dans le code → un `.rvt` ouvert
# sur une autre machine voit le même param sans collision.

_SHARED_PARAM_GROUP_NAME = "claude-in-revit"
_SHARED_PARAM_NAME = "llm_id"
# Stable across machines so the parameter survives copy-pasting the file
# between collaborators. Regenerated once, frozen here.
_SHARED_PARAM_GUID = "cca44e1c-7a8d-4b3e-9f50-7c1d8ab23e0a"

# Minimal valid Revit shared-parameter file header. Revit's text format,
# UTF-16 LE with BOM. Definitions are appended by Revit when we call
# `Definitions.Create(opts)` so we only need the bare skeleton here.
_SHARED_PARAMS_FILE_HEADER = (
    "# This is a Revit shared parameter file.\r\n"
    "# Do not edit manually.\r\n"
    "*META\tVERSION\tMINVERSION\r\n"
    "META\t2\t1\r\n"
    "*GROUP\tID\tNAME\r\n"
    "*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE\t"
    "DESCRIPTION\tUSERMODIFIABLE\tHIDEWHENNOVALUE\r\n"
)


def _ensure_shared_params_file(path):
    """Create an empty-but-valid shared params file at `path` if missing."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Revit's shared-param parser expects UTF-16 LE with BOM.
    data = "﻿" + _SHARED_PARAMS_FILE_HEADER
    with open(str(path), "wb") as f:
        f.write(data.encode("utf-16-le"))


def _get_or_create_definition(doc):
    """Return the `ExternalDefinition` for `claude-in-revit:llm_id`.

    Creates the group + definition in the shared parameter file if absent.
    Idempotent — calling multiple times returns the existing definition
    after the first call. The file path comes from `config.shared_params_file()`.
    Side effect: sets `app.SharedParametersFilename` for the session.
    """
    from . import config
    import System  # PythonNet — provided by pyRevit runtime

    app = doc.Application
    path = config.shared_params_file()
    _ensure_shared_params_file(path)
    app.SharedParametersFilename = str(path)
    shared_file = app.OpenSharedParameterFile()
    if shared_file is None:
        raise RuntimeError(
            "Could not open shared parameter file: {}".format(path)
        )

    group = None
    for g in shared_file.Groups:
        if g.Name == _SHARED_PARAM_GROUP_NAME:
            group = g
            break
    if group is None:
        group = shared_file.Groups.Create(_SHARED_PARAM_GROUP_NAME)

    definition = None
    for d in group.Definitions:
        if d.Name == _SHARED_PARAM_NAME:
            definition = d
            break
    if definition is None:
        opts = ExternalDefinitionCreationOptions(
            _SHARED_PARAM_NAME, SpecTypeId.String.Text
        )
        opts.GUID = System.Guid(_SHARED_PARAM_GUID)
        definition = group.Definitions.Create(opts)
    return definition


def _all_bindable_categories(doc):
    """Build a `CategorySet` of every category accepting bound parameters.

    "Anticipates future types" per the design — Doors, Windows, Rooms,
    Floors, Roofs, Beams, etc. all get the binding upfront so adding a
    new converter to `kg_sync` later doesn't require re-binding the
    parameter. Categories that raise on `.AllowsBoundParameters` are
    skipped silently (some hidden / sub-categories misbehave).
    """
    app = doc.Application
    cat_set = app.Create.NewCategorySet()
    for cat in doc.Settings.Categories:
        try:
            if cat.AllowsBoundParameters:
                cat_set.Insert(cat)
        except Exception:  # noqa: BLE001 — defensive: some categories raise.
            continue
    return cat_set


def ensure_shared_param_binding(doc):
    """One-shot setup of `claude-in-revit:llm_id` as an Instance parameter.

    Creates the shared parameter file if missing, defines the parameter
    if absent, and binds it as an Instance parameter on all categories
    accepting bound parameters. Idempotent — if the binding already
    exists, returns `False` without re-opening a Revit transaction.

    Opens its own Revit transaction (named "claude-in-revit: bind llm_id
    shared param") so the caller must NOT be inside another Revit
    transaction when invoking this. Currently called from
    `kg_sync.full_rescan`, which runs outside any Revit transaction.

    Returns `True` if a new binding was inserted, `False` if it was
    already in place.
    """
    definition = _get_or_create_definition(doc)
    bindings_map = doc.ParameterBindings
    if bindings_map.Contains(definition):
        return False

    cat_set = _all_bindable_categories(doc)
    app = doc.Application
    binding = app.Create.NewInstanceBinding(cat_set)
    # Revit 2024+ deprecated `BuiltInParameterGroup` (enum) in favour of
    # `GroupTypeId` (ForgeTypeId). 2025 removed the old name entirely from
    # the public API, hence we use `GroupTypeId.IdentityData` here — same
    # semantic group ("Identification" in the FR Properties panel),
    # surfaced via the modern ForgeTypeId machinery.
    with transaction(doc, "claude-in-revit: bind llm_id shared param"):
        bindings_map.Insert(
            definition, binding, GroupTypeId.IdentityData,
        )
    return True


def set_llm_id_on_element(element, llm_id):
    """Mirror the KG-side llm_id onto the element's shared parameter.

    Must be called inside a Revit transaction. Returns `True` on success,
    `False` if the parameter isn't bound on this element's category
    (rare given `ensure_shared_param_binding` binds everything bindable)
    or if the write was refused. Failures are silent: the KG holds the
    authoritative mapping, so a missing UX mirror is not fatal.
    """
    if element is None or not llm_id:
        return False
    try:
        param = element.LookupParameter(_SHARED_PARAM_NAME)
    except Exception:  # noqa: BLE001
        return False
    if param is None or param.IsReadOnly:
        return False
    try:
        return bool(param.Set(str(llm_id)))
    except Exception:  # noqa: BLE001
        return False


def get_llm_id_from_element(element):
    """Read the llm_id mirror on an element. Fallback only — not for normal flow.

    The KG is the source of truth for the llm_id ↔ revit_id mapping.
    This getter exists only for recovery scenarios (KG file missing or
    corrupted) where we want to re-bootstrap the mapping from the Revit
    document alone. Returns `None` if the parameter isn't set or
    accessible.
    """
    if element is None:
        return None
    try:
        param = element.LookupParameter(_SHARED_PARAM_NAME)
    except Exception:  # noqa: BLE001
        return None
    if param is None or not param.HasValue:
        return None
    try:
        value = param.AsString()
    except Exception:  # noqa: BLE001
        return None
    return value or None
