"""Internal helpers shared across `lib/tools/` modules.

Private (`_` prefix). Auto-importer in `tools/__init__.py` skips files
whose name starts with `_`, so nothing in this module registers itself
as a tool.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence


def _is_contiguous_llm_ids(llm_ids: Sequence[str]) -> bool:
    """Return True iff every llm_id has the same `<type>_` prefix and the
    integer suffixes form a consecutive ascending range.

    KG-allocated llm_ids in a single bulk run ARE contiguous (the counter
    increments without interleaving), so this should be True for any
    fresh batch — useful for compact summaries.
    """
    if not llm_ids:
        return True
    prefix = None
    numbers: List[int] = []
    for lid in llm_ids:
        idx = lid.rfind("_")
        if idx < 0:
            return False
        p, suffix = lid[:idx], lid[idx + 1:]
        try:
            numbers.append(int(suffix))
        except ValueError:
            return False
        if prefix is None:
            prefix = p
        elif p != prefix:
            return False
    return numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def bulk_summary(
    llm_ids: Sequence[str],
    *,
    small_threshold: int = 8,
) -> Dict[str, Any]:
    """Compact `tool_result` payload for any bulk create / copy / transform.

    Goal : keep the response under ~50 tokens regardless of the batch
    size, vs the ~50-70 tokens × N you get if every item is enumerated
    with its full attrs. The LLM still has enough information to:
      - confirm the operation succeeded (`ok` + `count`).
      - reference each created element by `llm_id` (either inline for
        small batches, or via a contiguous range for large ones).
      - pull full details on demand via `catalog_list_<type>` or
        `query_get_node(llm_id)`.

    `revit_id` is deliberately absent: no tool accepts a `revit_id` as
    input, so surfacing it to the LLM is dead weight in the token
    budget.

    Args:
        llm_ids: ordered list of llm_ids created/affected by the bulk
            op. Order should match the user-visible iteration order
            (e.g. for a grid : i varies slowest, j fastest — same as
            ``columns_create_grid``).
        small_threshold: batches strictly smaller than this get their
            ids inlined (`"llm_ids": [...]`). Larger batches get a
            contiguous-range summary when possible.

    Returns:
        - Empty batch: `{"ok": True, "count": 0, "llm_ids": []}`
        - Small batch (`<= small_threshold`):
          `{"ok": True, "count": N, "llm_ids": [...]}`
        - Large contiguous batch:
          `{"ok": True, "count": N, "first_llm_id": ..., "last_llm_id": ...,
            "contiguous": True, "note": "..."}`
        - Large non-contiguous batch (rare):
          `{"ok": True, "count": N, "llm_ids": [...], "note": "..."}`
    """
    ids = list(llm_ids)
    n = len(ids)
    out: Dict[str, Any] = {"ok": True, "count": n}
    if n == 0:
        out["llm_ids"] = []
        return out
    if n <= small_threshold:
        out["llm_ids"] = ids
        return out
    if _is_contiguous_llm_ids(ids):
        out["first_llm_id"] = ids[0]
        out["last_llm_id"] = ids[-1]
        out["contiguous"] = True
        out["note"] = (
            "All {} llm_ids are contiguous from {} to {}. "
            "Use catalog_list_<type> or query_get_node(llm_id) for "
            "details on any specific one."
        ).format(n, ids[0], ids[-1])
        return out
    out["llm_ids"] = ids
    out["note"] = "Non-contiguous batch — explicit list of {} llm_ids.".format(n)
    return out


def stamp_llm_id(element: Any, llm_id: str) -> None:
    """Mirror the KG-side llm_id onto the Revit element's shared parameter.

    The KG remains the source of truth — this stamp is purely a UX
    surface (visible in Revit's Properties panel) and a recovery
    fallback. Silent on failure: if `revit_primitives` isn't on the
    pythonpath (hors-Revit tests) or the param isn't bound on the
    element's category, we no-op so callers don't have to guard.

    Must be invoked inside an open Revit transaction (the parameter
    write requires one). Typically called right after the KG-side
    `kg.set_revit_id(llm_id, revit_id)` inside a `rp.transaction(...)`
    block in the creation tool.
    """
    if element is None or not llm_id:
        return
    try:
        from .. import revit_primitives as rp
    except Exception:  # noqa: BLE001 — pythonpath/import issues hors-Revit.
        return
    fn = getattr(rp, "set_llm_id_on_element", None)
    if fn is None:
        return
    try:
        fn(element, llm_id)
    except Exception:  # noqa: BLE001 — UX surface, never fatal.
        pass
