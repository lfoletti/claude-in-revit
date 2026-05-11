#! python3
# -*- coding: utf-8 -*-
"""Force a full re-scan of the Revit model into the project Knowledge Graph.

Mitigation for KG drift (§4.1 of DESIGN.md): the user may edit the model
outside the agent pipeline; this button discards the cached KG and rebuilds
it from the live Revit document via `kg_sync.full_rescan(doc)`.
"""
__title__ = "Refresh KG"
__doc__ = "Force-resync the project Knowledge Graph with the current Revit model."

# `pyrevit.forms` is IronPython-only — use the Revit API's TaskDialog under CPython.
from Autodesk.Revit.UI import TaskDialog


TaskDialog.Show(
    "claude-in-revit",
    "KG refresh is not implemented yet.\n\n"
    "Requires `lib/kg_sync.py` + `lib/revit_primitives.py` — next phase. "
    "See DESIGN.md §4.1.",
)
