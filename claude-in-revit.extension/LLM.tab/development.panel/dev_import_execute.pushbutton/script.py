#! python3
# -*- coding: utf-8 -*-
"""DEV: invoke `dwg_import_project_audit` + `dwg_import_project_execute`
manuellement sur un dossier DXF choisi par l'user. Stage 2 de la validation
(CLAUDE.md "Process de développement").

Mutate Revit + KG. Cliquer après avoir lu `dev_import_audit` pour comprendre
le plan. Undo (Ctrl+Z) annule les créations après inspection.
"""
__title__ = "DEV Execute Import"
__doc__ = "Pre-validation manuelle de dwg_import_project_execute."

import os
import sys
import traceback

_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

import clr
clr.AddReference("System.Windows.Forms")

from Autodesk.Revit.UI import (
    TaskDialog,
    TaskDialogCommonButtons,
    TaskDialogResult,
)
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
    dlg.Description = "Choisir le dossier DXF projet à importer"
    dlg.ShowNewFolderButton = False
    if dlg.ShowDialog() != DialogResult.OK:
        return ""
    return dlg.SelectedPath


def _confirm_plan(audit) -> bool:
    """TaskDialog Yes/No récapitulant ce qui va être créé."""
    rec = audit.get("level_reconciliation") or {}
    files = audit.get("files") or {}
    actions = audit.get("level_actions_proposed") or []
    creates = [a for a in actions if a.get("action") == "create"]

    lines = [
        "Plan d'import :",
        "  Plans     : {}".format(len(files.get("plans") or [])),
        "  Coupes    : {}".format(len(files.get("coupes") or [])),
        "  Élévations: {}".format(len(files.get("elevations") or [])),
        "",
        "Niveaux à créer ({}):".format(len(creates)),
    ]
    for a in creates:
        lines.append("  + {} @ {:.2f}m".format(a.get("name"), float(a.get("elevation_m") or 0.0)))

    lines.append("")
    lines.append("Gate status: {} ({})".format(
        audit.get("gate_status"), audit.get("severity")
    ))

    dlg = TaskDialog("DEV Execute Import — confirmer")
    dlg.MainInstruction = "Lancer l'import complet ?"
    dlg.MainContent = "\n".join(lines)
    dlg.CommonButtons = TaskDialogCommonButtons.Yes | TaskDialogCommonButtons.No
    dlg.DefaultButton = TaskDialogResult.No
    return dlg.Show() == TaskDialogResult.Yes


def _format_result(result) -> str:
    lines = ["DEV Execute Import — résultat"]
    lines.append("")
    if not result.get("ok"):
        lines.append("KO — {}".format(result.get("reason")))
        return "\n".join(lines)
    p1 = result.get("phase1_setup", {})
    lines.append("Phase 1 setup :")
    lines.append("  levels_created           : {}".format(p1.get("levels_created")))
    lines.append("  sections_created         : {}".format(p1.get("sections_created")))
    lines.append("  section_lines_registered : {}".format(p1.get("section_lines_registered")))
    lines.append("  linked_views_count       : {}".format(p1.get("linked_views_count")))
    lines.append("  skipped_unmatched        : {}".format(p1.get("skipped_unmatched")))
    # Diagnostic : convention X axis appliquée + verdict Revit (P2 mirror fix).
    sect_orient = p1.get("section_orientations") or []
    if sect_orient:
        lines.append("  section_orientations (basis_x check) :")
        for so in sect_orient:
            match = so.get("basis_x_match")
            marker = "✓" if match else ("✗" if match is False else "?")
            lines.append("    {} {}  view_dir={}  conv={}  intended={}  actual={}".format(
                marker,
                so.get("name") or so.get("coupe_path", "?")[-20:],
                so.get("view_dir"), so.get("x_axis_convention"),
                so.get("intended_basis_x"),
                so.get("actual_right_direction"),
            ))
    p2a = result.get("phase2a_walls", {})
    lines.append("")
    lines.append("Phase 2a walls :")
    lines.append("  walls_imported_total : {}".format(p2a.get("walls_imported_total")))
    lines.append("  fusion_events        : {}".format(p2a.get("fusion_events")))
    suspects = p2a.get("walls_suspect_low_3d_consensus") or []
    if suspects:
        lines.append("  walls_suspect ({}): {}".format(
            len(suspects), ", ".join(s.get("llm_id", "?") for s in suspects[:10])
        ))
    p2b = result.get("phase2b_openings", {})
    lines.append("")
    lines.append("Phase 2b openings :")
    lines.append("  plan_openings_detected   : {}".format(p2b.get("plan_openings_detected")))
    lines.append("  windows_created          : {}".format(p2b.get("openings_windows_created")))
    lines.append("  doors_created            : {}".format(p2b.get("openings_doors_created")))
    lines.append("  oversize_for_wall        : {}".format(p2b.get("openings_oversize_for_wall")))
    lines.append("  orphan                   : {}".format(p2b.get("openings_orphan")))
    p2c = result.get("phase2c_floors", {})
    lines.append("")
    lines.append("Phase 2c floors :")
    lines.append("  floors_created_count : {}".format(p2c.get("floors_created_count")))
    lines.append("  per_level            : {}".format(p2c.get("floors_per_level")))
    holes = p2c.get("holes_count_by_kind") or {}
    if holes:
        lines.append("  trous détectés       : {}".format(
            ", ".join("{}={}".format(k, v) for k, v in sorted(holes.items()))
        ))
    p2d = result.get("phase2d_columns", {})
    lines.append("")
    lines.append("Phase 2d columns :")
    lines.append("  columns_created_count : {}".format(p2d.get("columns_created_count")))
    lines.append("  candidates_total      : {}".format(p2d.get("candidates_total")))
    lines.append("  aggregated_count      : {}".format(p2d.get("aggregated_count")))
    lines.append("  types_created/reused  : {} / {}".format(
        p2d.get("types_created"), p2d.get("types_reused"),
    ))
    cpl = p2d.get("columns_per_level") or {}
    if cpl:
        lines.append("  per_level             : {}".format(cpl))
    if p2d.get("skipped_reason"):
        lines.append("  ⚠ skipped_reason      : {}".format(p2d.get("skipped_reason")))
    lines.append("")
    lines.append("view_3d_opened : {}".format(result.get("view_3d_opened")))
    lines.append("")
    lines.append("note: {}".format(result.get("note")))
    return "\n".join(lines)


def _main():
    from lib import kg_sync
    from lib.tools.dwg_import import (
        import_project_audit, import_project_execute,
    )

    try:
        uiapp = __revit__  # type: ignore[name-defined]
    except NameError:
        from pyrevit import HOST_APP
        uiapp = HOST_APP.uiapp

    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        _show("DEV Execute Import", "Aucun document Revit actif.")
        return
    doc = uidoc.Document
    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show("DEV Execute Import", "Sauvegarde le projet Revit d'abord.")
        return

    directory = _pick_folder()
    if not directory:
        return

    kg = kg_sync.open_or_create(doc)

    # Step 1 : run audit, surface plan.
    try:
        audit = import_project_audit(kg=kg, directory=directory)
    except Exception as exc:  # noqa: BLE001
        _show("DEV Execute Import — audit failed",
              "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()))
        return

    if audit.get("gate_status") == "abort":
        errs = audit.get("integrity_audit", {}).get("errors") or []
        _show(
            "DEV Execute Import — abort",
            "Gate status abort. Errors :\n\n" + "\n".join(
                "- {}".format(e) for e in errs[:20]
            ),
        )
        return

    # Step 2 : confirm with user.
    if not _confirm_plan(audit):
        return

    # Step 3 : execute.
    try:
        result = import_project_execute(
            kg=kg, doc=doc, directory=directory,
            level_actions=audit.get("level_actions_proposed"),
            proceed_on_warnings=True,
        )
    except Exception as exc:  # noqa: BLE001
        _show("DEV Execute Import — execute failed",
              "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()))
        return

    _show("DEV Execute Import — résultat", _format_result(result))


try:
    _main()
except BaseException as exc:  # noqa: BLE001
    _show_error(
        "DEV Execute Import failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
