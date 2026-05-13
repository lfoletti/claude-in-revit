"""kg_sync.py — bind the project KG to the live Revit document.

Three responsibilities (V0, §4.1 of DESIGN.md):

1. **Binding KG ↔ Revit.** `bind()` stamps a Revit ElementId on a KG node;
   `revit_id_of()` / `llm_id_of()` are the two-way lookups every Revit-backed
   tool needs. The mapping is stored as a reserved attr (`_revit_id`) on the
   KG node — see `project_kg.set_revit_id` for the implementation.

2. **`full_rescan(doc, kg)`** — drop the KG topology and rebuild it by
   walking the Revit collectors (Levels, WallTypes, Walls in V0). Hybrid
   semantics: nodes/edges/counters reset, but `turn` and `action_log` are
   preserved across the rescan so the conversational timeline stays
   continuous (decision 2026-05-11).

3. **`@kg_synced(name)`** — decorator that pairs `revit_primitives.transaction`
   with `kg.transaction()` so Revit and KG commit or roll back together.
   Outer = KG (snapshot taken first), inner = Revit (commits inside the KG
   transaction). On exception, Revit rolls back first, then the KG snapshot
   is restored. The residual drift window is `kg.persist()` failing after
   Revit committed — covered by the `refresh_kg` pushbutton (§10
   mitigation).

**Trade-off on persisted ElementIds.** REVIT_API_NOTES warns against persisting
an `ElementId.Value` across Revit sessions (the integer may be reassigned).
We accept this for V0: `_revit_id` rides along in the JSON roundtrip for
convenience, but `full_rescan` is idempotent — calling it at session start
reconciles any stale mapping. If session-mismatch becomes a real problem,
add a session-id stamp on the KG JSON and force a rescan on mismatch.

**Importability.** Top-level imports are kept Revit-free so this module can
be imported under pytest. Anything that depends on `Autodesk.Revit.DB` or
`revit_primitives` is lazy-imported inside the relevant function. The
decorator only resolves `revit_primitives` when the wrapped function is
called, so tests can monkeypatch the import.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import math
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from . import config
from .project_kg import ProjectKG


F = TypeVar("F", bound=Callable[..., Any])


# ----- Project identifier + KG lifecycle --------------------------------


PROJECT_ID_LEN = 16  # 64 bits truncated from sha256 → enough for our scale.


def project_id_for(doc: Any) -> str:
    """Stable project_id derived from a Revit `Document`.

    Algorithm (§8 of DESIGN.md, fallback path): hash `doc.PathName`. For
    an unsaved document `PathName == ""`, we fall back to `doc.Title` so
    the user can experiment before saving; the id will *change* once they
    save and reopen, intentionally — the design treats path-based ids as
    canonical.

    Tentative 1 of the design doc (shared Revit parameter
    `claude-in-revit.project_uuid`) is deferred: creating a shared param
    file, binding it to ProjectInformation, and round-tripping the value
    is substantial plumbing. Worth it once we hit a real workflow where
    `Save As` would otherwise orphan the KG; until then the hash path is
    enough.
    """
    path_name = getattr(doc, "PathName", "") or ""
    seed = path_name.strip()
    if not seed:
        seed = "title:{}".format(getattr(doc, "Title", "") or "untitled")
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:PROJECT_ID_LEN]


def open_or_create(doc: Any) -> ProjectKG:
    """Load the cached KG for this Revit doc, or create an empty one.

    The returned `ProjectKG` always has `persist_path` set, so a subsequent
    `kg.persist()` or `kg.transaction()` will write to the right file.
    Does not call `full_rescan` — the caller decides (refresh_kg button
    forces a rescan; the prompt button may load and let the LLM operate
    against the cached graph).
    """
    project_id = project_id_for(doc)
    path = config.kg_path_for(project_id)
    if path.exists():
        kg = ProjectKG.load(path)
        return kg
    return ProjectKG(project_id=project_id, persist_path=path)


# ----- Binding helpers --------------------------------------------------


def _extract_revit_id(element_or_id: Any) -> int:
    """Coerce a Revit `Element`, `ElementId`, or raw int to an int.

    Revit 2024+ exposes `ElementId.Value` as a `long`; pre-2024 it was
    `IntegerValue`. We try both, then fall back to `int(...)`.
    """
    obj = element_or_id
    if hasattr(obj, "Id"):
        obj = obj.Id
    if hasattr(obj, "Value"):
        return int(obj.Value)
    if hasattr(obj, "IntegerValue"):
        return int(obj.IntegerValue)
    return int(obj)


def bind(kg: ProjectKG, llm_id: str, element_or_id: Any) -> None:
    """Stamp a Revit element / ElementId / int onto a KG node."""
    kg.set_revit_id(llm_id, _extract_revit_id(element_or_id))


def revit_id_of(kg: ProjectKG, llm_id: str) -> Optional[Any]:
    """Return the bound ElementId for a llm_id, or None.

    Returns a real `Autodesk.Revit.DB.ElementId` instance — constructed
    lazily so this function is callable from hors-Revit code paths that
    happen to expect `None` (no binding).
    """
    raw = kg.get_revit_id(llm_id)
    if raw is None:
        return None
    from Autodesk.Revit.DB import ElementId
    return ElementId(raw)


def llm_id_of(kg: ProjectKG, element_or_id: Any) -> Optional[str]:
    """Reverse lookup: which llm_id is bound to this Revit element?"""
    return kg.find_by_revit_id(_extract_revit_id(element_or_id))


# ----- Active selection (UI bridge) -------------------------------------


# BuiltInCategory.Id.Value snapshots — `full_rescan` covers these. Anything
# else in the selection (annotation lines, dimensions, text notes, tags,
# reference planes, …) won't show up after a Refresh KG, so we shouldn't
# suggest one to the user. Integer values are stable across Revit versions
# and language packs (unlike Category.Name which is localised).
_RESCANNABLE_CATEGORY_IDS: set = {
    -2000011,  # OST_Walls — Wall instances and WallType definitions.
    -2000051,  # OST_Lines — ModelCurve (3D) + DetailCurve (view-bound 2D).
    -2000100,  # OST_Columns — architectural column instances + types.
    -2001330,  # OST_StructuralColumns — structural column instances + types.
    -2000023,  # OST_Doors — hosted door instances + FamilySymbols.
    -2000014,  # OST_Windows — hosted window instances + FamilySymbols.
    # OST_Levels (-2000240) intentionally NOT included: Levels are picked
    # up via `OfClass(Level)` (their Category is unreliable on 2024+, see
    # revit_primitives.levels() rationale), and selecting a Level via the
    # UI is uncommon enough that a Refresh KG suggestion isn't useful.
}


_OST_COLUMNS_CATEGORY_INT = -2000100
_OST_STRUCTURAL_COLUMNS_CATEGORY_INT = -2001330
_OST_DOORS_CATEGORY_INT = -2000023
_OST_WINDOWS_CATEGORY_INT = -2000014


def _column_kind(element: Any) -> str:
    """Return 'structural' or 'architectural' for a column element/type."""
    try:
        cat = getattr(element, "Category", None)
        if cat is not None and getattr(cat, "Id", None) is not None:
            if _extract_revit_id(cat.Id) == _OST_STRUCTURAL_COLUMNS_CATEGORY_INT:
                return "structural"
    except Exception:  # noqa: BLE001
        pass
    return "architectural"


def _category_summary(doc: Any, element_id: Any) -> Tuple[str, bool]:
    """Best-effort `(category_name, is_rescannable)` for an element id.

    Returns `("(inconnu)", False)` if `doc` is None or the lookup fails.
    Defensive: a flaky `.Category` access on a corrupted element should
    not abort the whole selection summary.
    """
    if doc is None:
        return "(inconnu)", False
    try:
        element = doc.GetElement(element_id)
        if element is None:
            return "(inconnu)", False
        cat = getattr(element, "Category", None)
        if cat is None:
            return "(sans catégorie)", False
        name = getattr(cat, "Name", None) or "(sans nom)"
        cat_id = getattr(cat, "Id", None)
        is_rescannable = False
        if cat_id is not None:
            is_rescannable = _extract_revit_id(cat_id) in _RESCANNABLE_CATEGORY_IDS
        return name, is_rescannable
    except Exception:  # noqa: BLE001 — diagnostic helper, never raise.
        return "(inconnu)", False


def active_selection_llm_ids(
    uidoc: Any,
    kg: ProjectKG,
    doc: Any = None,
) -> Tuple[List[str], Dict[str, int], bool]:
    """Resolve the user's current Revit selection to KG llm_ids.

    UX rationale (2026-05-11): when the user clicks elements in Revit
    and then says "supprime ce mur" / "déplace ça", the LLM should
    resolve the demonstrative against the selection rather than asking.
    The pushbutton calls this once per turn and injects the result into
    the system prompt; tools use the llm_ids as normal refs.

    Selected elements that aren't in the KG are *categorised* (annotation
    lines, dimensions, text notes, etc.) so we can give the user a
    meaningful summary instead of a bare unbound count. Detail Lines,
    Text Notes and friends are out of V0 scope — a Refresh KG won't
    bring them in. The `refresh_kg_actionable` flag tells the caller
    whether at least one unbound element falls in a category that
    *would* be covered by a rescan (Walls), so the UI can decide
    whether to suggest one.

    Returns:
        `(llm_ids, unbound_by_category, refresh_kg_actionable)` —
        - `llm_ids`: ordered list of mapped llm_ids (KG bindings).
        - `unbound_by_category`: `{category_name: count}` for the
          selected elements not in the KG. Category names use Revit's
          localised display name (e.g. `"Murs"` in FR), so the LLM
          gets it in the user's language. Empty dict if nothing
          unbound.
        - `refresh_kg_actionable`: `True` iff at least one unbound
          element is in a category covered by `full_rescan`. False for
          pure annotation-line selections — suggesting a Refresh would
          be misleading.

    Tolerates `uidoc is None` and `uidoc.Selection is None`. Passing
    `doc is None` collapses category lookup to `"(inconnu)"` (used in
    hors-Revit tests).
    """
    empty: Tuple[List[str], Dict[str, int], bool] = ([], {}, False)
    if uidoc is None:
        return empty
    selection = getattr(uidoc, "Selection", None)
    if selection is None:
        return empty
    raw_ids = list(selection.GetElementIds())

    llm_ids: List[str] = []
    unbound_by_category: Dict[str, int] = {}
    refresh_actionable = False
    for eid in raw_ids:
        match = kg.find_by_revit_id(_extract_revit_id(eid))
        if match is not None:
            llm_ids.append(match)
            continue
        cat_name, is_rescannable = _category_summary(doc, eid)
        unbound_by_category[cat_name] = unbound_by_category.get(cat_name, 0) + 1
        if is_rescannable:
            refresh_actionable = True
    return llm_ids, unbound_by_category, refresh_actionable


# ----- Element → KG attrs converters (private) --------------------------
#
# Each `_<type>_to_attrs(element, ...)` returns the `attrs` dict expected by
# `ProjectKG.add_node(node_type, attrs)`. Conversions to metres happen here
# so the KG schema only ever speaks SI units.


# Float artifacts from the feet↔metres roundtrip (`0.20000000000000004`,
# `4.999999999999992`) make the JSON noisy without adding precision the
# user cares about. Round at the SI conversion boundary so the KG stores
# clean numbers — 6 decimals = sub-micrometre precision, well past
# anything BIM needs.
_FP_PRECISION = 6


def _r(value: float) -> float:
    """Round a float at the KG storage boundary (6 decimals)."""
    return round(float(value), _FP_PRECISION)


def _level_to_attrs(level: Any) -> Dict[str, Any]:
    from . import revit_primitives as rp
    return {
        "name": level.Name,
        "elevation": _r(rp.internal_to_meters(level.Elevation)),
    }


def _wall_type_to_attrs(wall_type: Any) -> Dict[str, Any]:
    from . import revit_primitives as rp
    return {
        "name": wall_type.Name,
        "total_thickness": _r(rp.internal_to_meters(wall_type.Width)),
    }


def _column_type_to_attrs(symbol: Any) -> Dict[str, Any]:
    """Extract `family_name`, `type_name`, `kind` from a column FamilySymbol."""
    return {
        "family_name": symbol.Family.Name,
        "type_name": symbol.Name,
        "kind": _column_kind(symbol),
    }


def _column_to_attrs(
    column: Any,
    *,
    level_ref: str,
    type_ref: str,
) -> Dict[str, Any]:
    """Extract column instance attrs (`position`, `height`, `kind`).

    Position is the column's base point in metres `[x, y]` (z dropped —
    the base level handles the elevation). Height is read from the
    top-level-offset parameter if present, else 0.0 (caller can update
    later via a dedicated tool).
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter

    loc = column.Location
    pt = loc.Point  # XYZ in feet
    position = [
        _r(rp.internal_to_meters(pt.X)),
        _r(rp.internal_to_meters(pt.Y)),
    ]
    height_param = column.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
    height_m = _r(rp.internal_to_meters(height_param.AsDouble())) if height_param else 0.0
    return {
        "level_ref": level_ref,
        "type_ref": type_ref,
        "position": position,
        "height": height_m,
        "kind": _column_kind(column),
    }


def _curve_element_to_attrs(curve_element: Any) -> Dict[str, Any]:
    """Extract endpoints + length from a `CurveElement` (ModelCurve or
    DetailCurve). Arcs and splines raise so the caller can `skipped[…] += 1`.

    Stores `p1`/`p2` as 3D `[x, y, z]` in metres. Detail lines live on
    a view sketch plane so their z is usually 0, but we keep the third
    dimension explicit to stay consistent with model lines.
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import Line

    curve = curve_element.GeometryCurve
    if not isinstance(curve, Line):
        raise ValueError("Only straight Line geometry is supported in V0")
    a = curve.GetEndPoint(0)
    b = curve.GetEndPoint(1)
    p1 = [
        _r(rp.internal_to_meters(a.X)),
        _r(rp.internal_to_meters(a.Y)),
        _r(rp.internal_to_meters(a.Z)),
    ]
    p2 = [
        _r(rp.internal_to_meters(b.X)),
        _r(rp.internal_to_meters(b.Y)),
        _r(rp.internal_to_meters(b.Z)),
    ]
    length = _r(math.sqrt(sum((p2[i] - p1[i]) ** 2 for i in range(3))))
    return {"p1": p1, "p2": p2, "length": length}


def _family_type_to_attrs(symbol: Any, *, category: str) -> Dict[str, Any]:
    """Extract `family_name`, `type_name`, `category` (+ `dimensions`)
    from a FamilySymbol.

    `category` is a stable discriminator string ("Doors" | "Windows" | …)
    set by the caller from the BuiltInCategory used to collect the symbol.
    Catalog tools (`catalog_list_door_types`, `_window_types`) filter on
    this attr without re-resolving the Revit category at query time.

    `dimensions` is populated opportunistically — the cascade in
    `revit_primitives.opening_read_*` returns None on families whose
    parameter naming we don't recognise. When both height and width
    resolve, we emit `{"height_m": h, "width_m": w}` ; missing one is
    skipped (key absent rather than `None`). The LLM filters /
    selects types by these dimensions via `catalog_list_door_types` /
    `_window_types`.
    """
    from . import revit_primitives as rp
    attrs: Dict[str, Any] = {
        "family_name": symbol.Family.Name,
        "type_name": symbol.Name,
        "category": category,
    }
    dims: Dict[str, float] = {}
    h = rp.opening_read_height_m(symbol)
    if h is not None:
        dims["height_m"] = _r(h)
    w = rp.opening_read_width_m(symbol)
    if w is not None:
        dims["width_m"] = _r(w)
    if dims:
        attrs["dimensions"] = dims
    return attrs


def _opening_to_attrs(
    opening: Any,
    *,
    type_ref: str,
    host_wall_ref: str,
) -> Dict[str, Any]:
    """Extract shared attrs for a hosted Door/Window instance.

    `position` is `[x, y]` in metres on the level plane (z is implied by
    the host level's elevation + the sill height). `sill_height` and
    `head_height` come from the standard BuiltInParameters — if a
    family doesn't expose them (rare on stock Revit families) we fall
    back to 0.0 and the agent can re-read after creation.
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter

    loc = opening.Location
    pt = loc.Point  # XYZ in feet — for hosted FamilyInstance this is the
                    # world-space insertion point on the host wall.
    position = [
        _r(rp.internal_to_meters(pt.X)),
        _r(rp.internal_to_meters(pt.Y)),
    ]
    sill_param = opening.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
    head_param = opening.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
    sill_height = _r(rp.internal_to_meters(sill_param.AsDouble())) if sill_param else 0.0
    head_height = _r(rp.internal_to_meters(head_param.AsDouble())) if head_param else 0.0
    return {
        "type_ref": type_ref,
        "host_wall_ref": host_wall_ref,
        "position": position,
        "sill_height": sill_height,
        "head_height": head_height,
    }


def _room_to_attrs(
    room: Any,
    *,
    level_ref: str,
) -> Dict[str, Any]:
    """Extract Room attrs (`name`, `area`).

    `name` is read from `ROOM_NAME` BIP. If absent / empty (e.g. unplaced
    room with no user-set name), falls back to `"Room"` so the schema's
    required `name` field is never empty.

    `area` is read from `ROOM_AREA` BIP — Revit computes it from the
    boundary loops. Unplaced rooms have area=0. The value flows through
    `internal_to_sqm` (square feet → m²).

    `boundary_walls` is set to `[]` in V0 — computing the actual list of
    boundary `Wall` llm_ids requires walking `Room.GetBoundarySegments`
    and matching each segment's `ElementId` against the KG, which is
    brittle without a real Revit project to validate against. Deferred
    to the compliance work (UC8) where this list becomes load-bearing.
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter

    name_param = room.get_Parameter(BuiltInParameter.ROOM_NAME)
    raw_name = name_param.AsString() if name_param is not None else None
    name = raw_name if raw_name else "Room"
    area_param = room.get_Parameter(BuiltInParameter.ROOM_AREA)
    area_m2 = (
        _r(rp.internal_to_sqm(area_param.AsDouble()))
        if area_param is not None else 0.0
    )
    return {
        "name": name,
        "level_ref": level_ref,
        "area": area_m2,
        "boundary_walls": [],
    }


def _floor_type_to_attrs(ft: Any) -> Dict[str, Any]:
    """Extract FloorType attrs : `name` + `total_thickness` (m).

    `total_thickness` est sommée depuis la `CompoundStructure` du FloorType
    (somme des `Width` des layers, déjà en feet → convertis ici). Si le
    type n'expose pas de CompoundStructure (rare sur stock Revit), on
    retombe à `0.0` plutôt que d'échouer.
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter

    name_param = ft.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
    name = name_param.AsString() if name_param else getattr(ft, "Name", "FloorType")
    total_thickness_ft = 0.0
    try:
        cs = ft.GetCompoundStructure()
        if cs is not None:
            for layer in cs.GetLayers():
                total_thickness_ft += float(layer.Width)
    except Exception:  # noqa: BLE001
        pass
    return {
        "name": name,
        "total_thickness": _r(rp.internal_to_meters(total_thickness_ft)),
    }


def _floor_to_attrs(
    floor: Any,
    *,
    level_ref: str,
    type_ref: str,
) -> Dict[str, Any]:
    """Extract Floor attrs : boundary + area_m2.

    Le contour est extrait via `Floor.GetBoundarySegments` (méthode
    moderne 2023+, équivalent à `Sketch.GetAllElements` du legacy). Le
    1er CurveLoop est traité comme l'extérieur du sol ; les autres
    (trous, openings) sont ignorés en V0. La conversion segment → sommets
    récupère uniquement les `GetEndPoint(0)` (le 2nd point d'un segment
    est le 1er du suivant), donc on a N sommets pour N segments.

    `area_m2` vient de `HOST_AREA_COMPUTED` (autorité Revit), pas du
    shoelace côté KG — Revit a déjà fait le calcul.
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, SpatialElementBoundaryOptions

    boundary: List[List[float]] = []
    try:
        # Floor n'a pas `GetBoundarySegments` direct ; on passe par
        # `Floor.GetGeometry` et le 1er Solid pour récupérer une face
        # top. Plus robuste : `Floor.SketchId` → Sketch → CurveLoops.
        sketch_id = getattr(floor, "SketchId", None)
        if sketch_id is not None and sketch_id.Value > 0:
            from Autodesk.Revit.DB import Sketch
            sketch_elem = floor.Document.GetElement(sketch_id)
            if isinstance(sketch_elem, Sketch):
                # `Sketch.Profile` → IList<CurveArray> (legacy) ou
                # GetAllElements pour les segments. V0 : utilise Profile
                # qui est stable.
                profile = sketch_elem.Profile
                if profile is not None and profile.Size > 0:
                    curve_array = profile.get_Item(0)  # 1er loop = extérieur
                    for i in range(curve_array.Size):
                        curve = curve_array.get_Item(i)
                        p = curve.GetEndPoint(0)
                        boundary.append([
                            _r(rp.internal_to_meters(p.X)),
                            _r(rp.internal_to_meters(p.Y)),
                        ])
    except Exception:  # noqa: BLE001
        pass

    area_param = floor.get_Parameter(BuiltInParameter.HOST_AREA_COMPUTED)
    if area_param is not None:
        area_m2 = _r(rp.internal_to_sqm(area_param.AsDouble()))
    else:
        area_m2 = 0.0

    return {
        "type_ref": type_ref,
        "level_ref": level_ref,
        "boundary": boundary,
        "area_m2": area_m2,
    }


def _wall_to_attrs(
    wall: Any,
    *,
    level_ref: str,
    wall_type_ref: str,
) -> Dict[str, Any]:
    """Extract straight-wall attrs. Caller resolves type/level refs upfront.

    Curved walls (Arc LocationCurve) are not supported in V0; we fall back
    to the chord endpoints. Tracked as a known gap for the geometry phase.
    """
    from . import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter

    loc = wall.Location
    curve = loc.Curve
    a = curve.GetEndPoint(0)
    b = curve.GetEndPoint(1)
    p1 = [_r(rp.internal_to_meters(a.X)), _r(rp.internal_to_meters(a.Y))]
    p2 = [_r(rp.internal_to_meters(b.X)), _r(rp.internal_to_meters(b.Y))]
    length = _r(math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2))

    height_param = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    height_m = _r(rp.internal_to_meters(height_param.AsDouble())) if height_param else 0.0

    return {
        "type_ref": wall_type_ref,
        "level_ref": level_ref,
        "p1": p1,
        "p2": p2,
        "length": length,
        "height": height_m,
    }


# ----- Full rescan ------------------------------------------------------


def full_rescan(doc: Any, kg: ProjectKG) -> Dict[str, Any]:
    """Drop the KG topology and rebuild it from the live Revit document.

    Hybrid reset: nodes/edges are wiped; `turn`, `action_log`, and
    `_counters` are preserved (decision 2026-05-11). A single `rescan`
    entry is appended to the action log so the timeline reflects the
    boundary — individual `create` events for each rebuilt element are
    *suppressed* (otherwise the log would grow by N elements at every
    rescan, see 2026-05-12 fix).

    **Stable llm_ids across rescans.** Before clearing topology, we snapshot
    `{revit_id: llm_id}` from the existing KG. During the rebuild, each
    Revit element looks itself up by its `ElementId.Value`: if the
    snapshot knows it, we reuse the same llm_id (so `wall_007` keeps
    pointing to the same physical wall after a Refresh KG). If unknown
    (new since last scan), the typed counter allocates a fresh id —
    counters are preserved so new ids never collide with reused ones.
    The KG remains the single source of truth for the mapping; the
    Revit-side shared parameter is purely a UX mirror / recovery
    fallback (cf. 2026-05-12).

    **Defensive per-element conversion.** Each `Element` is converted
    inside an isolated try/except: if one wall type has a quirky `.Width`
    (curtain walls, stacked walls) or one wall has a non-curve `Location`,
    we skip it and keep going rather than aborting the whole scan. The
    skipped counts are surfaced in the summary so the user can see what
    didn't make it.

    **Atomic.** The whole rebuild runs inside `kg.transaction()`. If
    something fails so badly that even the per-element try/except can't
    catch it, the KG is restored to its pre-rescan state — never left
    half-cleared. Persistence is done by the transaction exit on success.

    Returns `{"levels": N, "wall_types": M, "walls": K, "skipped": {...}}`.
    """
    from . import revit_primitives as rp

    skipped = {
        "levels": 0,
        "wall_types": 0,
        "walls": 0,
        "model_lines": 0,
        "detail_lines": 0,
        "column_types": 0,
        "columns": 0,
        "door_types": 0,
        "window_types": 0,
        "doors": 0,
        "windows": 0,
        "rooms": 0,
        "floor_types": 0,
        "floors": 0,
    }

    # Snapshot `revit_id → llm_id` BEFORE clearing — drives id stability.
    preserved = kg.snapshot_revit_id_map()

    def _preserved_id(element_or_id: Any) -> Optional[str]:
        try:
            return preserved.get(_extract_revit_id(element_or_id))
        except Exception:  # noqa: BLE001 — corrupted element shouldn't abort.
            return None

    # Ensure the `claude-in-revit:llm_id` shared parameter is bound on every
    # bindable category. Soft-fail: the KG is the authoritative source for
    # the mapping, so a missing UX mirror just degrades the Properties-panel
    # display — never the data layer. Pre-call sits OUTSIDE `kg.transaction`
    # because `ensure_shared_param_binding` opens its own Revit transaction
    # and Revit refuses nested transactions of the same kind. Resolved via
    # `getattr` so hors-Revit tests with a stubbed `revit_primitives` (no
    # `ensure_shared_param_binding`) keep working without explicit opt-in.
    param_bound = False
    ensure_fn = getattr(rp, "ensure_shared_param_binding", None)
    if ensure_fn is not None:
        try:
            ensure_fn(doc)
            param_bound = True
        except Exception:  # noqa: BLE001
            param_bound = False
    stamp_fn = getattr(rp, "set_llm_id_on_element", None) if param_bound else None

    def _stamp(element: Any, llm_id: str) -> None:
        """Mirror the llm_id onto the element's shared parameter. No-op
        when the binding isn't available (stub mode or first-time setup
        failed). Silent failures are intentional — UX surface, not data."""
        if stamp_fn is None:
            return
        try:
            stamp_fn(element, llm_id)
        except Exception:  # noqa: BLE001
            pass

    # The rebuild itself stays in a single Revit transaction when there's
    # anything to stamp (so `param.Set` calls are batched into one commit),
    # otherwise drops to a null context to preserve the pure-KG path
    # exercised by tests.
    rebuild_tx = (
        rp.transaction(doc, "claude-in-revit: rescan stamp llm_id")
        if stamp_fn is not None else contextlib.nullcontext()
    )

    with kg.transaction(), rebuild_tx:
        kg._clear_topology(preserve_counters=True)  # noqa: SLF001

        # 1. Levels — no inbound refs.
        for lvl in rp.levels(doc):
            try:
                nid = kg.add_node(
                    "Level",
                    _level_to_attrs(lvl),
                    llm_id=_preserved_id(lvl),
                    _emit_log=False,
                )
                bind(kg, nid, lvl)
                _stamp(lvl, nid)
            except Exception:  # noqa: BLE001 — converter or Revit attr access.
                skipped["levels"] += 1

        # 2. WallTypes — no inbound refs. WallKind.Curtain / Stacked may
        # have a `.Width` that's 0 or raises; the try/except absorbs both.
        for wt in rp.wall_types(doc):
            try:
                nid = kg.add_node(
                    "WallType",
                    _wall_type_to_attrs(wt),
                    llm_id=_preserved_id(wt),
                    _emit_log=False,
                )
                bind(kg, nid, wt)
                _stamp(wt, nid)
            except Exception:  # noqa: BLE001
                skipped["wall_types"] += 1

        # 3. Walls — depend on Levels + WallTypes being bound already.
        # An unmapped host (level on a linked file, wall type filtered
        # out, or wall type that failed the convert above) ⇒ skip silently.
        for w in rp.walls(doc):
            try:
                level_ref = llm_id_of(kg, w.LevelId)
                wall_type_ref = llm_id_of(kg, w.WallType)
                if level_ref is None or wall_type_ref is None:
                    skipped["walls"] += 1
                    continue
                attrs = _wall_to_attrs(
                    w, level_ref=level_ref, wall_type_ref=wall_type_ref,
                )
                nid = kg.add_node(
                    "Wall", attrs, llm_id=_preserved_id(w), _emit_log=False,
                )
                bind(kg, nid, w)
                _stamp(w, nid)
                kg.add_edge(nid, level_ref, "at_level")
                kg.add_edge(nid, wall_type_ref, "is_type")
            except Exception:  # noqa: BLE001
                skipped["walls"] += 1

        # 4. ModelLines — 3D anchor curves drawn by the user.
        # Arcs / splines fall in the skipped bucket (Line geometry only V0).
        for ml in rp.model_lines(doc):
            try:
                nid = kg.add_node(
                    "ModelLine",
                    _curve_element_to_attrs(ml),
                    llm_id=_preserved_id(ml),
                    _emit_log=False,
                )
                bind(kg, nid, ml)
                _stamp(ml, nid)
            except Exception:  # noqa: BLE001
                skipped["model_lines"] += 1

        # 5. DetailLines — view-bound; we drop the view link in V0.
        for dl in rp.detail_lines(doc):
            try:
                nid = kg.add_node(
                    "DetailLine",
                    _curve_element_to_attrs(dl),
                    llm_id=_preserved_id(dl),
                    _emit_log=False,
                )
                bind(kg, nid, dl)
                _stamp(dl, nid)
            except Exception:  # noqa: BLE001
                skipped["detail_lines"] += 1

        # 6. ColumnTypes — FamilySymbols of column families. Scanned
        # before column instances so columns can resolve their type_ref.
        for ct in rp.column_types(doc):
            try:
                nid = kg.add_node(
                    "ColumnType",
                    _column_type_to_attrs(ct),
                    llm_id=_preserved_id(ct),
                    _emit_log=False,
                )
                bind(kg, nid, ct)
                _stamp(ct, nid)
            except Exception:  # noqa: BLE001
                skipped["column_types"] += 1

        # 7. Columns — depend on Levels + ColumnTypes already bound.
        for col in rp.columns(doc):
            try:
                level_ref = llm_id_of(kg, col.LevelId)
                type_ref = llm_id_of(kg, col.Symbol)
                if level_ref is None or type_ref is None:
                    skipped["columns"] += 1
                    continue
                attrs = _column_to_attrs(
                    col, level_ref=level_ref, type_ref=type_ref,
                )
                nid = kg.add_node(
                    "Column", attrs, llm_id=_preserved_id(col), _emit_log=False,
                )
                bind(kg, nid, col)
                _stamp(col, nid)
                kg.add_edge(nid, level_ref, "at_level")
                kg.add_edge(nid, type_ref, "is_type")
            except Exception:  # noqa: BLE001
                skipped["columns"] += 1

        # 8. DoorTypes / WindowTypes — FamilySymbols filtered by category.
        # Scanned before instances so doors/windows can resolve `type_ref`.
        for dt in rp.door_types(doc):
            try:
                nid = kg.add_node(
                    "FamilyType",
                    _family_type_to_attrs(dt, category="Doors"),
                    llm_id=_preserved_id(dt),
                    _emit_log=False,
                )
                bind(kg, nid, dt)
                _stamp(dt, nid)
            except Exception:  # noqa: BLE001
                skipped["door_types"] += 1

        for wt in rp.window_types(doc):
            try:
                nid = kg.add_node(
                    "FamilyType",
                    _family_type_to_attrs(wt, category="Windows"),
                    llm_id=_preserved_id(wt),
                    _emit_log=False,
                )
                bind(kg, nid, wt)
                _stamp(wt, nid)
            except Exception:  # noqa: BLE001
                skipped["window_types"] += 1

        # 9. Doors — hosted on Walls. Need Wall + DoorType refs already
        # bound. Unmapped host (door on a wall we couldn't scan, or whose
        # type failed conversion) → skip silently.
        for d in rp.doors(doc):
            try:
                host_wall_ref = llm_id_of(kg, d.Host)
                type_ref = llm_id_of(kg, d.Symbol)
                if host_wall_ref is None or type_ref is None:
                    skipped["doors"] += 1
                    continue
                # `at_level` is the host wall's level — derive from KG
                # rather than re-reading from Revit, so a Door whose
                # `.LevelId` is invalid still gets a clean edge.
                host_attrs = kg.get_node(host_wall_ref)
                level_ref = host_attrs.get("level_ref")
                attrs = _opening_to_attrs(
                    d, type_ref=type_ref, host_wall_ref=host_wall_ref,
                )
                nid = kg.add_node(
                    "Door", attrs, llm_id=_preserved_id(d), _emit_log=False,
                )
                bind(kg, nid, d)
                _stamp(d, nid)
                kg.add_edge(host_wall_ref, nid, "hosts")
                kg.add_edge(nid, type_ref, "is_type")
                if level_ref is not None:
                    kg.add_edge(nid, level_ref, "at_level")
            except Exception:  # noqa: BLE001
                skipped["doors"] += 1

        # 10. Windows — same pattern as Doors.
        for w in rp.windows(doc):
            try:
                host_wall_ref = llm_id_of(kg, w.Host)
                type_ref = llm_id_of(kg, w.Symbol)
                if host_wall_ref is None or type_ref is None:
                    skipped["windows"] += 1
                    continue
                host_attrs = kg.get_node(host_wall_ref)
                level_ref = host_attrs.get("level_ref")
                attrs = _opening_to_attrs(
                    w, type_ref=type_ref, host_wall_ref=host_wall_ref,
                )
                nid = kg.add_node(
                    "Window", attrs, llm_id=_preserved_id(w), _emit_log=False,
                )
                bind(kg, nid, w)
                _stamp(w, nid)
                kg.add_edge(host_wall_ref, nid, "hosts")
                kg.add_edge(nid, type_ref, "is_type")
                if level_ref is not None:
                    kg.add_edge(nid, level_ref, "at_level")
            except Exception:  # noqa: BLE001
                skipped["windows"] += 1

        # 10b. FloorTypes — no inbound refs. Scanned before Floor
        # instances so floors can resolve their type_ref.
        floor_types_fn = getattr(rp, "floor_types", None)
        if floor_types_fn is not None:
            for ft in floor_types_fn(doc):
                try:
                    nid = kg.add_node(
                        "FloorType",
                        _floor_type_to_attrs(ft),
                        llm_id=_preserved_id(ft),
                        _emit_log=False,
                    )
                    bind(kg, nid, ft)
                    _stamp(ft, nid)
                except Exception:  # noqa: BLE001
                    skipped["floor_types"] += 1

        # 10c. Floors — depend on Levels + FloorTypes already bound.
        floors_fn = getattr(rp, "floors", None)
        if floors_fn is not None:
            for fl in floors_fn(doc):
                try:
                    level_ref = llm_id_of(kg, fl.LevelId)
                    type_ref = llm_id_of(kg, fl.GetTypeId())
                    if level_ref is None or type_ref is None:
                        skipped["floors"] += 1
                        continue
                    attrs = _floor_to_attrs(
                        fl, level_ref=level_ref, type_ref=type_ref,
                    )
                    nid = kg.add_node(
                        "Floor", attrs, llm_id=_preserved_id(fl), _emit_log=False,
                    )
                    bind(kg, nid, fl)
                    _stamp(fl, nid)
                    kg.add_edge(nid, level_ref, "at_level")
                    kg.add_edge(nid, type_ref, "is_type")
                except Exception:  # noqa: BLE001
                    skipped["floors"] += 1

        # 11. Rooms — depend on Levels already bound. Unplaced rooms (no
        # location, area=0) are still ingested: they keep an action_log
        # presence and can be picked up by `kg.refresh()` once the user
        # encloses them with walls.
        for r in rp.rooms(doc):
            try:
                level_ref = llm_id_of(kg, r.LevelId)
                if level_ref is None:
                    skipped["rooms"] += 1
                    continue
                attrs = _room_to_attrs(r, level_ref=level_ref)
                nid = kg.add_node(
                    "Room", attrs, llm_id=_preserved_id(r), _emit_log=False,
                )
                bind(kg, nid, r)
                _stamp(r, nid)
                kg.add_edge(nid, level_ref, "at_level")
            except Exception:  # noqa: BLE001
                skipped["rooms"] += 1

        reused = sum(
            1 for _, attrs in kg._g.nodes(data=True)  # noqa: SLF001
            if attrs.get("_revit_id") is not None
            and preserved.get(int(attrs["_revit_id"])) is not None
        )
        # Door / Window FamilyTypes share the `FamilyType` node type with
        # other future hosted families; we count by `category` attr so the
        # summary reflects what was actually scanned in each pass.
        family_types_by_cat = {"Doors": 0, "Windows": 0, "other": 0}
        for nid in kg.find_by_type("FamilyType"):
            cat = kg.get_node(nid).get("category", "other")
            family_types_by_cat[cat] = family_types_by_cat.get(cat, 0) + 1
        summary = {
            "levels": kg.count_by_type("Level"),
            "wall_types": kg.count_by_type("WallType"),
            "walls": kg.count_by_type("Wall"),
            "model_lines": kg.count_by_type("ModelLine"),
            "detail_lines": kg.count_by_type("DetailLine"),
            "column_types": kg.count_by_type("ColumnType"),
            "columns": kg.count_by_type("Column"),
            "door_types": family_types_by_cat.get("Doors", 0),
            "window_types": family_types_by_cat.get("Windows", 0),
            "doors": kg.count_by_type("Door"),
            "windows": kg.count_by_type("Window"),
            "floor_types": kg.count_by_type("FloorType"),
            "floors": kg.count_by_type("Floor"),
            "rooms": kg.count_by_type("Room"),
            "skipped": dict(skipped),
            "preserved_llm_ids": reused,
        }
        kg._log("rescan", target="", summary=dict(summary))  # noqa: SLF001

    return summary


# ----- Post-mutation read-back (KG mirrors Revit reality) ----------------
#
# Discipline established 2026-05-11 (session 5) : every Revit-side
# mutation tool should call this helper *after* its `param.Set` /
# `MoveElement` / `Create` / etc. so the KG reflects what Revit actually
# committed rather than what the caller asked. Without this, the KG
# silently diverges whenever a Revit constraint overrides the request
# (family-rigid opening_height, Top Constraint on walls, snap-to-grid on
# placement, etc.) — see `_drift_note` in `tools/openings.py` for the
# user-facing alert pattern.

# Per-node-type whitelist of *volatile* attrs that should be mirrored
# from Revit after a mutation. Refs (level_ref / type_ref /
# host_wall_ref / wall_type_ref) are deliberately excluded — they are
# set at creation, never altered by geometric mutations, and re-writing
# them would force a schema validation cycle for no reason.
_REFRESH_FIELDS: Dict[str, Tuple[str, ...]] = {
    "Wall": ("p1", "p2", "length", "height"),
    "Column": ("position", "height"),
    "Door": ("position", "sill_height", "head_height"),
    "Window": ("position", "sill_height", "head_height"),
    "Floor": ("boundary", "area_m2"),
    "ModelLine": ("p1", "p2", "length"),
    "DetailLine": ("p1", "p2", "length"),
    "Level": ("name", "elevation"),
    "WallType": ("name", "total_thickness"),
    "FloorType": ("name", "total_thickness"),
    "ColumnType": ("family_name", "type_name", "kind"),
    "FamilyType": ("family_name", "type_name", "dimensions"),
    # Room area is computed by Revit from the boundary loops. The
    # `recompute_boundaries` tool calls `doc.Regenerate()` then
    # `refresh_node_from_revit` to mirror the new value — without the
    # area in this whitelist, the regenerate would be a no-op on the KG.
    "Room": ("name", "area"),
}


def refresh_node_from_revit(
    kg: ProjectKG, doc: Any, llm_id: str,
) -> Optional[Dict[str, Any]]:
    """Re-read a Revit-bound KG node and mirror the live attrs in the KG.

    Resolves the node type, looks up its Revit element via the bound
    `_revit_id`, dispatches to the appropriate `_*_to_attrs` converter,
    then `modify_node`s the volatile attrs declared in `_REFRESH_FIELDS`
    for that type. Skips refs (level_ref / type_ref / host_wall_ref…)
    which mutations never alter.

    Returns the dict of fresh attrs actually mirrored, or `None` when
    the node has no Revit binding (CLI / pytest path) or the Revit
    element is gone (deleted / invalid id).

    Tools call this **inside their `rp.transaction`** after a Set /
    Move / Create, so the KG and Revit commit / rollback together via
    the outer `kg.transaction()` posed by the dispatcher.
    """
    if not kg.has_node(llm_id):
        return None
    raw = kg.get_revit_id(llm_id)
    if raw is None:
        return None

    node = kg.get_node(llm_id)
    node_type = node.get("_type")
    fields = _REFRESH_FIELDS.get(node_type)
    if not fields:
        return None

    from Autodesk.Revit.DB import ElementId

    element = doc.GetElement(ElementId(raw))
    if element is None:
        return None

    if node_type == "Wall":
        fresh = _wall_to_attrs(
            element,
            level_ref=node.get("level_ref"),
            wall_type_ref=node.get("type_ref"),
        )
    elif node_type == "Column":
        fresh = _column_to_attrs(
            element,
            level_ref=node.get("level_ref"),
            type_ref=node.get("type_ref"),
        )
    elif node_type in ("Door", "Window"):
        fresh = _opening_to_attrs(
            element,
            type_ref=node.get("type_ref"),
            host_wall_ref=node.get("host_wall_ref"),
        )
    elif node_type in ("ModelLine", "DetailLine"):
        fresh = _curve_element_to_attrs(element)
    elif node_type == "Level":
        fresh = _level_to_attrs(element)
    elif node_type == "WallType":
        fresh = _wall_type_to_attrs(element)
    elif node_type == "FloorType":
        fresh = _floor_type_to_attrs(element)
    elif node_type == "Floor":
        fresh = _floor_to_attrs(
            element,
            level_ref=node.get("level_ref", ""),
            type_ref=node.get("type_ref", ""),
        )
    elif node_type == "ColumnType":
        fresh = _column_type_to_attrs(element)
    elif node_type == "FamilyType":
        fresh = _family_type_to_attrs(
            element, category=node.get("category", ""),
        )
    elif node_type == "Room":
        fresh = _room_to_attrs(
            element, level_ref=node.get("level_ref", ""),
        )
    else:
        return None

    updates = {k: fresh[k] for k in fields if k in fresh}
    if updates:
        kg.modify_node(llm_id, updates)
    return updates


# Drift detection helper. Compares a requested scalar / vector against
# what Revit actually committed (re-read via `refresh_node_from_revit`).
# Returns (drift: bool, note: Optional[str]) — note is None when within
# tolerance, otherwise a one-line explanation pointing at the likely
# Revit-side constraint that overrode the value.
_DRIFT_EPSILON = 5e-4  # half a mm tolerates feet↔metres round-trip.


def detect_drift(
    requested: Any, committed: Any, field: str = "value",
) -> Tuple[bool, Optional[str]]:
    """Return `(has_drift, note)` comparing requested vs committed.

    - Scalars (int / float) compared by absolute difference.
    - Lists (e.g. `[x, y]`, `[x, y, z]`) compared elementwise.
    - `None` on either side → no drift signal (caller didn't know to
      compare, or the live value wasn't readable).
    """
    if requested is None or committed is None:
        return False, None
    if isinstance(requested, (list, tuple)):
        if not isinstance(committed, (list, tuple)) or len(committed) != len(requested):
            return True, (
                "Revit committed shape {} mais on attendait {} pour {}".format(
                    committed, requested, field,
                )
            )
        diff = max(
            abs(float(c) - float(r))
            for r, c in zip(requested, committed)
        )
        if diff <= _DRIFT_EPSILON:
            return False, None
        return True, (
            "Revit a commit {} au lieu de {} demandé pour {} "
            "(écart max {:.3f} m)".format(committed, requested, field, diff)
        )
    try:
        diff = abs(float(committed) - float(requested))
    except (TypeError, ValueError):
        return False, None
    if diff <= _DRIFT_EPSILON:
        return False, None
    return True, (
        "Revit a commit {:.3f} au lieu de {:.3f} demandé pour {}".format(
            float(committed), float(requested), field,
        )
    )


# ----- Generic Revit element → KG dispatcher ----------------------------


def _stamp_param_silent(element: Any, llm_id: str) -> None:
    """Mirror the llm_id onto the element's `claude-in-revit:llm_id` shared
    parameter, swallowing any failure.

    Used by `ingest_revit_element` so callers (transforms after copy) get
    the Properties-panel display without needing to know whether the
    parameter is bound. Silent because the KG is the source of truth — a
    missing UX mirror degrades visibility but never the data layer.
    Resolved via `getattr` so a stubbed `revit_primitives` (hors-Revit
    tests) doesn't need to define the function explicitly.

    Caller's responsibility to be inside an open Revit transaction.
    """
    from . import revit_primitives as rp
    fn = getattr(rp, "set_llm_id_on_element", None)
    if fn is None:
        return
    try:
        fn(element, llm_id)
    except Exception:  # noqa: BLE001
        pass


def ingest_revit_element(kg: ProjectKG, doc: Any, element: Any) -> str:
    """Detect the Revit element's class, dispatch to the right converter,
    add the resulting node to the KG, bind the ElementId, and add edges.

    The cornerstone of generic transformation tools (translate, rotate,
    mirror, array_linear, …): after Revit produces new elements via
    `ElementTransformUtils.CopyElements`, the caller doesn't need to
    know which type each new element is — `ingest_revit_element`
    figures it out and updates the KG accordingly.

    Supported in V0: Wall, Column (architectural + structural, via
    FamilyInstance + category), ModelCurve, DetailCurve. Anything
    else raises ValueError — the caller (typically a bulk transform
    tool) collects the failure and aborts the batch atomically.

    Returns the new node's llm_id.

    Also mirrors the resulting llm_id onto the element's
    `claude-in-revit:llm_id` shared parameter so the user sees it in
    Revit's Properties panel. Silent failure if the parameter isn't
    bound (e.g. user did transforms before any Refresh KG); the KG
    remains the authoritative mapping.
    """
    from Autodesk.Revit.DB import (
        DetailCurve,
        FamilyInstance,
        ModelCurve,
        Wall,
    )

    if isinstance(element, Wall):
        level_ref = llm_id_of(kg, element.LevelId)
        wall_type_ref = llm_id_of(kg, element.WallType)
        if level_ref is None or wall_type_ref is None:
            raise ValueError(
                "Cannot ingest Wall {}: level or wall_type missing from "
                "KG. Run Refresh KG first.".format(
                    _extract_revit_id(element.Id),
                )
            )
        attrs = _wall_to_attrs(
            element, level_ref=level_ref, wall_type_ref=wall_type_ref,
        )
        nid = kg.add_node("Wall", attrs)
        bind(kg, nid, element)
        _stamp_param_silent(element, nid)
        kg.add_edge(nid, level_ref, "at_level")
        kg.add_edge(nid, wall_type_ref, "is_type")
        return nid

    if isinstance(element, FamilyInstance):
        cat = getattr(element, "Category", None)
        if cat is not None and getattr(cat, "Id", None) is not None:
            cat_int = _extract_revit_id(cat.Id)
            if cat_int in (
                _OST_COLUMNS_CATEGORY_INT,
                _OST_STRUCTURAL_COLUMNS_CATEGORY_INT,
            ):
                level_ref = llm_id_of(kg, element.LevelId)
                type_ref = llm_id_of(kg, element.Symbol)
                if level_ref is None or type_ref is None:
                    raise ValueError(
                        "Cannot ingest Column {}: level or type missing "
                        "from KG. Run Refresh KG first.".format(
                            _extract_revit_id(element.Id),
                        )
                    )
                attrs = _column_to_attrs(
                    element, level_ref=level_ref, type_ref=type_ref,
                )
                nid = kg.add_node("Column", attrs)
                bind(kg, nid, element)
                _stamp_param_silent(element, nid)
                kg.add_edge(nid, level_ref, "at_level")
                kg.add_edge(nid, type_ref, "is_type")
                return nid

            if cat_int in (
                _OST_DOORS_CATEGORY_INT,
                _OST_WINDOWS_CATEGORY_INT,
            ):
                node_type = (
                    "Door" if cat_int == _OST_DOORS_CATEGORY_INT else "Window"
                )
                host_wall_ref = llm_id_of(kg, element.Host)
                type_ref = llm_id_of(kg, element.Symbol)
                if host_wall_ref is None or type_ref is None:
                    raise ValueError(
                        "Cannot ingest {} {}: host wall or type missing "
                        "from KG. Run Refresh KG first.".format(
                            node_type, _extract_revit_id(element.Id),
                        )
                    )
                # Derive level from host wall — Door.LevelId may differ
                # from the wall's level on certain hosted families;
                # we treat the host wall as authoritative for `at_level`.
                host_attrs = kg.get_node(host_wall_ref)
                level_ref = host_attrs.get("level_ref")
                attrs = _opening_to_attrs(
                    element, type_ref=type_ref, host_wall_ref=host_wall_ref,
                )
                nid = kg.add_node(node_type, attrs)
                bind(kg, nid, element)
                _stamp_param_silent(element, nid)
                kg.add_edge(host_wall_ref, nid, "hosts")
                kg.add_edge(nid, type_ref, "is_type")
                if level_ref is not None:
                    kg.add_edge(nid, level_ref, "at_level")
                return nid

    if isinstance(element, ModelCurve):
        nid = kg.add_node("ModelLine", _curve_element_to_attrs(element))
        bind(kg, nid, element)
        _stamp_param_silent(element, nid)
        return nid

    if isinstance(element, DetailCurve):
        nid = kg.add_node("DetailLine", _curve_element_to_attrs(element))
        bind(kg, nid, element)
        _stamp_param_silent(element, nid)
        return nid

    raise ValueError(
        "Don't know how to ingest element of class {}. Supported in V0: "
        "Wall, Column (arch/struct FamilyInstance), Door / Window "
        "(hosted FamilyInstance), ModelCurve, DetailCurve.".format(
            type(element).__name__
        )
    )


# ----- @kg_synced decorator --------------------------------------------


def kg_synced(name_or_fn: Any) -> Any:
    """Pair a Revit `Transaction` with a `kg.transaction()` so both atomic.

    Two call forms:

        @kg_synced                # uses fn.__name__ as the Revit Tx name
        def create_wall(kg, doc, ...): ...

        @kg_synced("create_wall")
        def create_wall(kg, doc, ...): ...

    Wrapped functions must accept `kg` and `doc` as the first two positional
    parameters (the convention is checked at call time, not at decoration —
    `inspect.signature` is enough to enforce statically and we may relax later).

    **Order of context managers**: KG outer (snapshot), Revit inner (commit).
    On exception inside the body, the inner ctx rolls back Revit first,
    then the outer ctx restores the KG snapshot — symmetric rollback. On
    success, Revit commits, then `kg.persist()` writes to disk. The only
    drift window is a disk failure during persist after Revit committed,
    mitigated by `refresh_kg` (§10).
    """
    if callable(name_or_fn):
        return _wrap(name_or_fn, name_or_fn.__name__)
    return lambda fn: _wrap(fn, name_or_fn)


def _wrap(fn: F, tx_name: str) -> F:
    @functools.wraps(fn)
    def wrapper(kg: ProjectKG, doc: Any, *args: Any, **kwargs: Any) -> Any:
        # Lazy import: only resolved when the decorated function is actually
        # called (so the module stays importable in pytest where there's no
        # Autodesk.Revit.DB on sys.path).
        from . import revit_primitives as rp
        with kg.transaction():
            with rp.transaction(doc, tx_name):
                return fn(kg, doc, *args, **kwargs)
    return wrapper  # type: ignore[return-value]
