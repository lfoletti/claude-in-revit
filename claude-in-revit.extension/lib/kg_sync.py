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
    # OST_Levels (-2000240) intentionally NOT included: Levels are picked
    # up via `OfClass(Level)` (their Category is unreliable on 2024+, see
    # revit_primitives.levels() rationale), and selecting a Level via the
    # UI is uncommon enough that a Refresh KG suggestion isn't useful.
}


_OST_COLUMNS_CATEGORY_INT = -2000100
_OST_STRUCTURAL_COLUMNS_CATEGORY_INT = -2001330


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


def _level_to_attrs(level: Any) -> Dict[str, Any]:
    from . import revit_primitives as rp
    return {
        "name": level.Name,
        "elevation": rp.internal_to_meters(level.Elevation),
    }


def _wall_type_to_attrs(wall_type: Any) -> Dict[str, Any]:
    from . import revit_primitives as rp
    return {
        "name": wall_type.Name,
        "total_thickness": rp.internal_to_meters(wall_type.Width),
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
        rp.internal_to_meters(pt.X),
        rp.internal_to_meters(pt.Y),
    ]
    height_param = column.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
    height_m = rp.internal_to_meters(height_param.AsDouble()) if height_param else 0.0
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
        rp.internal_to_meters(a.X),
        rp.internal_to_meters(a.Y),
        rp.internal_to_meters(a.Z),
    ]
    p2 = [
        rp.internal_to_meters(b.X),
        rp.internal_to_meters(b.Y),
        rp.internal_to_meters(b.Z),
    ]
    length = math.sqrt(sum((p2[i] - p1[i]) ** 2 for i in range(3)))
    return {"p1": p1, "p2": p2, "length": length}


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
    p1 = [rp.internal_to_meters(a.X), rp.internal_to_meters(a.Y)]
    p2 = [rp.internal_to_meters(b.X), rp.internal_to_meters(b.Y)]
    length = math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)

    height_param = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    height_m = rp.internal_to_meters(height_param.AsDouble()) if height_param else 0.0

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

    Hybrid reset: nodes/edges/counters are wiped; `turn` and `action_log`
    are preserved (decision 2026-05-11). A single `rescan` entry is appended
    to the action log so the timeline reflects the boundary.

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
    }

    with kg.transaction():
        kg._clear_topology()  # noqa: SLF001 — intentional cross-module API.

        # 1. Levels — no inbound refs.
        for lvl in rp.levels(doc):
            try:
                nid = kg.add_node("Level", _level_to_attrs(lvl))
                bind(kg, nid, lvl)
            except Exception:  # noqa: BLE001 — converter or Revit attr access.
                skipped["levels"] += 1

        # 2. WallTypes — no inbound refs. WallKind.Curtain / Stacked may
        # have a `.Width` that's 0 or raises; the try/except absorbs both.
        for wt in rp.wall_types(doc):
            try:
                nid = kg.add_node("WallType", _wall_type_to_attrs(wt))
                bind(kg, nid, wt)
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
                nid = kg.add_node("Wall", attrs)
                bind(kg, nid, w)
                kg.add_edge(nid, level_ref, "at_level")
                kg.add_edge(nid, wall_type_ref, "is_type")
            except Exception:  # noqa: BLE001
                skipped["walls"] += 1

        # 4. ModelLines — 3D anchor curves drawn by the user.
        # Arcs / splines fall in the skipped bucket (Line geometry only V0).
        for ml in rp.model_lines(doc):
            try:
                nid = kg.add_node("ModelLine", _curve_element_to_attrs(ml))
                bind(kg, nid, ml)
            except Exception:  # noqa: BLE001
                skipped["model_lines"] += 1

        # 5. DetailLines — view-bound; we drop the view link in V0.
        for dl in rp.detail_lines(doc):
            try:
                nid = kg.add_node("DetailLine", _curve_element_to_attrs(dl))
                bind(kg, nid, dl)
            except Exception:  # noqa: BLE001
                skipped["detail_lines"] += 1

        # 6. ColumnTypes — FamilySymbols of column families. Scanned
        # before column instances so columns can resolve their type_ref.
        for ct in rp.column_types(doc):
            try:
                nid = kg.add_node("ColumnType", _column_type_to_attrs(ct))
                bind(kg, nid, ct)
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
                nid = kg.add_node("Column", attrs)
                bind(kg, nid, col)
                kg.add_edge(nid, level_ref, "at_level")
                kg.add_edge(nid, type_ref, "is_type")
            except Exception:  # noqa: BLE001
                skipped["columns"] += 1

        summary = {
            "levels": kg.count_by_type("Level"),
            "wall_types": kg.count_by_type("WallType"),
            "walls": kg.count_by_type("Wall"),
            "model_lines": kg.count_by_type("ModelLine"),
            "detail_lines": kg.count_by_type("DetailLine"),
            "column_types": kg.count_by_type("ColumnType"),
            "columns": kg.count_by_type("Column"),
            "skipped": dict(skipped),
        }
        kg._log("rescan", target="", summary=dict(summary))  # noqa: SLF001

    return summary


# ----- Generic Revit element → KG dispatcher ----------------------------


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
                kg.add_edge(nid, level_ref, "at_level")
                kg.add_edge(nid, type_ref, "is_type")
                return nid

    if isinstance(element, ModelCurve):
        nid = kg.add_node("ModelLine", _curve_element_to_attrs(element))
        bind(kg, nid, element)
        return nid

    if isinstance(element, DetailCurve):
        nid = kg.add_node("DetailLine", _curve_element_to_attrs(element))
        bind(kg, nid, element)
        return nid

    raise ValueError(
        "Don't know how to ingest element of class {}. Supported in V0: "
        "Wall, Column (arch/struct FamilyInstance), ModelCurve, "
        "DetailCurve.".format(type(element).__name__)
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
