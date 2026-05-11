#! python3
# -*- coding: utf-8 -*-
"""Project-level globals editor (V1+).

Planned: edit `ProjectContext` (singleton in the project KG) — affectation,
jurisdiction, default Wall Types, etc. — with provenance tracked per field.
See DESIGN.md §4.5 and §8.
"""
__title__ = "Globals"
__doc__ = "Configure project-level variables (affectation, jurisdiction, defaults)."

# `pyrevit.forms` is IronPython-only — use the Revit API's TaskDialog under CPython.
from Autodesk.Revit.UI import TaskDialog


TaskDialog.Show(
    "claude-in-revit",
    "Globals editor is not implemented yet.\n\n"
    "Planned for V1+ once the KG `ProjectContext` schema and `config.json` land.\n"
    "See DESIGN.md §4.5 and §8.",
)
