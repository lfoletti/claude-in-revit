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
    FilteredElementCollector,
    Level,
    ModelCurve,
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
