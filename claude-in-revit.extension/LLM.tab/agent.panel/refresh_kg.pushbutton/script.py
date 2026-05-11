#! python3
# -*- coding: utf-8 -*-
"""Force a full re-scan of the Revit model into the project Knowledge Graph.

Mitigation for KG drift (§4.1 of DESIGN.md): the user may edit the model
outside the agent pipeline; this button discards the cached KG topology
and rebuilds it from the live Revit document via `kg_sync.full_rescan(doc)`.

The conversational timeline (`turn`, `action_log`) is preserved across the
refresh — hybrid semantics decided 2026-05-11.

**Defensive shell.** Any unhandled error inside the script body, *including*
.NET exceptions from PythonNet (which don't always subclass the Python
`Exception` we'd normally catch), bubbles up to Revit as the generic
"Echec de la commande externe" dialog with no Python context. The
`try / except BaseException` wrapping `_main()` plus the explicit
None-checks on `__revit__` / `ActiveUIDocument` / `Document` surface every
failure mode in a TaskDialog with a full traceback instead.
"""
__title__ = "Refresh KG"
__doc__ = "Force-resync the project Knowledge Graph with the current Revit model."

import os
import sys
import traceback

# pyRevit puts `<extension>/lib/` on sys.path but not the extension root.
# See JOURNAL.md 2026-05-11 (bootstrap phase 6).
_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

# `pyrevit.forms` is IronPython-only — use the Revit API's TaskDialog under CPython.
from Autodesk.Revit.UI import TaskDialog


def _show(title, body):
    TaskDialog.Show("claude-in-revit — {}".format(title), body)


def _main():
    # Imports inside the function so a missing module shows up in the
    # exception dialog instead of as a Revit-level external command failure.
    from lib import kg_sync

    # `__revit__` is injected by pyRevit at script start. Under CPython the
    # name is reachable by bare-name resolution (built-ins / module
    # namespace) but it does NOT show up in `globals()` — that pitfall
    # cost us a runtime trip on 2026-05-11 (PyRevitLoader dialog).
    # Fall back to `pyrevit.HOST_APP.uiapp` if the bare name isn't there.
    try:
        uiapp = __revit__  # type: ignore[name-defined]  # injected by pyRevit
    except NameError:
        try:
            from pyrevit import HOST_APP
            uiapp = HOST_APP.uiapp
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                "__revit__ global not available and pyrevit.HOST_APP "
                "fallback also failed ({}). pyRevit may not have "
                "initialised the script runtime.".format(fallback_exc)
            )

    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        _show(
            "Refresh KG",
            "Aucun document Revit actif. Ouvre un projet, puis recommence.",
        )
        return

    doc = uidoc.Document
    if doc is None:
        raise RuntimeError(
            "ActiveUIDocument has no Document — model may be in a "
            "transient open/close state. Try again."
        )

    # Require a saved file. `project_id_for()` has a Title-based fallback,
    # but using it would orphan the KG at first Save (id migrates from
    # "title:Project1" hash to PathName hash) and risk collisions between
    # unsaved drafts that share the default Title. Design doc §8 makes
    # PathName the canonical identifier.
    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show(
            "Refresh KG",
            "Le projet Revit n'est pas encore sauvegardé.\n\n"
            "Enregistre-le d'abord (Fichier → Enregistrer sous), "
            "puis recommence. L'identifiant du KG est dérivé du "
            "chemin du .rvt (§8 du DESIGN doc) ; un brouillon non "
            "sauvé produirait un KG orphelin à la première "
            "sauvegarde.",
        )
        return

    kg = kg_sync.open_or_create(doc)
    summary = kg_sync.full_rescan(doc, kg)
    skipped = summary.get("skipped", {})
    skipped_line = ""
    if any(skipped.values()):
        skipped_line = (
            "\nSkipped: levels={}, wall_types={}, walls={}, "
            "model_lines={}, detail_lines={}, "
            "column_types={}, columns={}"
        ).format(
            skipped.get("levels", 0),
            skipped.get("wall_types", 0),
            skipped.get("walls", 0),
            skipped.get("model_lines", 0),
            skipped.get("detail_lines", 0),
            skipped.get("column_types", 0),
            skipped.get("columns", 0),
        )
    _show(
        "Refresh KG",
        "KG resynced for project_id={}.\n\n"
        "Levels: {}\n"
        "Wall types: {}\n"
        "Walls: {}\n"
        "Model lines: {}\n"
        "Detail lines: {}\n"
        "Column types: {}\n"
        "Columns: {}{}\n\n"
        "Persisted to:\n{}".format(
            kg.project_id,
            summary["levels"],
            summary["wall_types"],
            summary["walls"],
            summary["model_lines"],
            summary["detail_lines"],
            summary["column_types"],
            summary["columns"],
            skipped_line,
            kg.persist_path,
        ),
    )


try:
    _main()
except BaseException as exc:  # noqa: BLE001 — surface everything to the UI.
    # `BaseException` (not `Exception`) because PythonNet sometimes wraps
    # .NET exceptions in a way that bypasses the Python Exception hierarchy.
    _show(
        "Refresh KG failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
