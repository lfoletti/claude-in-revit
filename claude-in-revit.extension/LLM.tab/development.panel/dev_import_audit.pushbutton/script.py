#! python3
# -*- coding: utf-8 -*-
"""DEV: invoke `dwg_import_project_audit` manuellement sur un dossier DXF
choisi par l'utilisateur. Stage 2 de la validation (CLAUDE.md "Process de
développement") — entre tests offline et exposition LLM.

Read-only : ne mute ni le KG ni Revit. Sûr à cliquer.
"""
__title__ = "DEV Audit Import"
__doc__ = "Pre-validation manuelle de dwg_import_project_audit."

import os
import sys
import json
import traceback

_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

import clr
clr.AddReference("System.Windows.Forms")

from Autodesk.Revit.UI import TaskDialog
from System.Windows.Forms import FolderBrowserDialog, DialogResult


def _show_error(title, body):
    TaskDialog.Show("claude-in-revit — {}".format(title), body)


def _show(title, body):
    try:
        from lib.ui_dialogs import show_selectable_text
        show_selectable_text("claude-in-revit — {}".format(title), body)
    except Exception:  # noqa: BLE001
        _show_error(title, body)


def _pick_folder() -> str:
    dlg = FolderBrowserDialog()
    dlg.Description = "Choisir le dossier DXF projet à auditer"
    dlg.ShowNewFolderButton = False
    if dlg.ShowDialog() != DialogResult.OK:
        return ""
    return dlg.SelectedPath


def _format_audit(result) -> str:
    lines = ["DEV Audit Import — dwg_import_project_audit"]
    lines.append("")
    lines.append("ok: {}".format(result.get("ok")))
    lines.append("gate_status: {} | severity: {}".format(
        result.get("gate_status"), result.get("severity")
    ))
    lines.append("")
    files = result.get("files") or {}
    lines.append("Fichiers détectés :")
    lines.append("  plans     : {}".format(len(files.get("plans") or [])))
    lines.append("  coupes    : {}".format(len(files.get("coupes") or [])))
    lines.append("  élévations: {}".format(len(files.get("elevations") or [])))
    lines.append("  plan_with_markers: {}".format(files.get("plan_with_markers")))
    lines.append("")
    assignment = result.get("section_assignment") or []
    if assignment:
        lines.append("Section assignment ({}):".format(len(assignment)))
        for sa in assignment:
            lines.append(
                "  {} → marker[{}] view_dir={} drift={:.4f}m ({:.2f}%)".format(
                    sa.get("coupe_name"), sa.get("marker_index"),
                    sa.get("view_dir"),
                    sa.get("drift_m") or 0.0,
                    sa.get("drift_pct") or 0.0,
                )
            )
        lines.append("")
    rec = result.get("level_reconciliation") or {}
    lines.append("Level reconciliation : alignment_complete={}".format(rec.get("alignment_complete")))
    lines.append("  missing/matches/elev_only: {} / {} / {}".format(
        rec.get("missing_count"), rec.get("matches_count"),
        rec.get("elev_only_matches_count"),
    ))
    if rec.get("summary_for_dialog"):
        lines.append("")
        lines.append(rec["summary_for_dialog"])
    actions = result.get("level_actions_proposed") or []
    if actions:
        lines.append("")
        lines.append("Level actions proposed ({}):".format(len(actions)))
        for a in actions:
            lines.append("  {}: {} @ {:.2f}m".format(
                a.get("action"), a.get("name"), float(a.get("elevation_m") or 0.0)
            ))
    lines.append("")
    lines.append("needs_warnings_confirm: {}".format(result.get("needs_warnings_confirm")))
    lines.append("needs_levels_confirm  : {}".format(result.get("needs_levels_confirm")))
    lines.append("")
    lines.append("next_step: {}".format(result.get("next_step")))
    return "\n".join(lines)


def _main():
    from lib import kg_sync
    from lib.tools.dwg_import import import_project_audit

    try:
        uiapp = __revit__  # type: ignore[name-defined]
    except NameError:
        from pyrevit import HOST_APP
        uiapp = HOST_APP.uiapp

    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        _show("DEV Audit Import", "Aucun document Revit actif. Ouvre un projet d'abord.")
        return
    doc = uidoc.Document
    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show("DEV Audit Import", "Sauvegarde le projet Revit d'abord (le KG dépend du path).")
        return

    directory = _pick_folder()
    if not directory:
        return

    kg = kg_sync.open_or_create(doc)
    try:
        result = import_project_audit(kg=kg, directory=directory)
    except Exception as exc:  # noqa: BLE001
        _show("DEV Audit Import — erreur", "{}: {}\n\n{}".format(
            type(exc).__name__, exc, traceback.format_exc()
        ))
        return

    _show("DEV Audit Import — résultat", _format_audit(result))


try:
    _main()
except BaseException as exc:  # noqa: BLE001
    _show_error(
        "DEV Audit Import failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
