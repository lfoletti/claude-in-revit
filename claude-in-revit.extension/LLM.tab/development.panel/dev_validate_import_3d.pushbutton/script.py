#! python3
# -*- coding: utf-8 -*-
"""DEV: invoke `dwg_validate_import_3d` sur le KG du projet ouvert.

Stage 2 de la validation (cf. CLAUDE.md). Read-only sur le KG :
agrège walls + floors + columns cross-validation 3D et affiche les
suspects.
"""
__title__ = "DEV Validate 3D"
__doc__ = "Pre-validation manuelle de dwg_validate_import_3d."

import os
import sys
import traceback

_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

from Autodesk.Revit.UI import TaskDialog


def _show_error(title, body):
    TaskDialog.Show("claude-in-revit — {}".format(title), body)


def _show(title, body):
    try:
        from lib.ui_dialogs import show_selectable_text
        show_selectable_text("claude-in-revit — {}".format(title), body)
    except Exception:  # noqa: BLE001
        _show_error(title, body)


def _format_report(payload) -> str:
    lines = ["DEV Validate 3D — dwg_validate_import_3d"]
    lines.append("")
    s = payload.get("summary") or {}
    lines.append("Summary :")
    lines.append("  total elements  : {}".format(s.get("total_elements")))
    lines.append("  total suspects  : {}".format(s.get("total_suspects")))
    lines.append("")
    lines.append("Walls ({}) : {} confirmed / {} unconfirmed / {} no-3d-evidence".format(
        s.get("walls_total"), s.get("walls_confirmed"),
        s.get("walls_unconfirmed"), s.get("walls_no_3d_evidence"),
    ))
    lines.append("Floors ({}) : {} confirmed / {} unconfirmed / {} partial / {} no-crossings".format(
        s.get("floors_total"), s.get("floors_confirmed"),
        s.get("floors_unconfirmed"), s.get("floors_partial_extent"),
        s.get("floors_no_crossings"),
    ))
    lines.append("Columns ({}) : {} confirmed / {} unconfirmed / {} no-crossings".format(
        s.get("columns_total"), s.get("columns_confirmed"),
        s.get("columns_unconfirmed"), s.get("columns_no_crossings"),
    ))
    lines.append("Openings ({}) : {} confirmed / {} unconfirmed / {} no-3d-evidence".format(
        s.get("openings_total"), s.get("openings_confirmed"),
        s.get("openings_unconfirmed"), s.get("openings_no_3d_evidence"),
    ))
    lines.append("")

    walls_unconf = payload.get("walls", {}).get("walls_unconfirmed_in_3d") or []
    if walls_unconf:
        lines.append("⚠ Murs suspects ({}) :".format(len(walls_unconf)))
        for w in walls_unconf[:20]:
            lines.append("  {} L={:.2f}m p1={} p2={} level={}".format(
                w.get("llm_id"),
                ((w["p1"][0]-w["p2"][0])**2 + (w["p1"][1]-w["p2"][1])**2)**0.5,
                ["{:.2f}".format(c) for c in w["p1"]],
                ["{:.2f}".format(c) for c in w["p2"]],
                w.get("level_name"),
            ))
        if len(walls_unconf) > 20:
            lines.append("  ... +{} autres".format(len(walls_unconf) - 20))
        lines.append("")

    floors_unconf = payload.get("floors", {}).get("floors_unconfirmed_in_3d") or []
    floors_partial = payload.get("floors", {}).get("floors_partial_extent") or []
    if floors_unconf or floors_partial:
        lines.append("⚠ Sols suspects :")
        for f in floors_unconf:
            lines.append("  [fantôme] {} Z={:.2f}m ep={:.3f}m level={}".format(
                f.get("llm_id"), f.get("elevation_m") or 0.0,
                f.get("thickness_m") or 0.0, f.get("level_name"),
            ))
        for f in floors_partial:
            lines.append("  [partiel] {} Z={:.2f}m level={}".format(
                f.get("llm_id"), f.get("elevation_m") or 0.0, f.get("level_name"),
            ))
        lines.append("")

    cols_unconf = payload.get("columns", {}).get("columns_unconfirmed_in_3d") or []
    if cols_unconf:
        lines.append("⚠ Poteaux suspects ({}) :".format(len(cols_unconf)))
        for c in cols_unconf[:20]:
            lines.append("  {} pos={} level={}".format(
                c.get("llm_id"),
                ["{:.2f}".format(p) for p in c.get("position", [])],
                c.get("level_name"),
            ))
        if len(cols_unconf) > 20:
            lines.append("  ... +{} autres".format(len(cols_unconf) - 20))
        lines.append("")

    ops_unconf = payload.get("openings", {}).get("openings_unconfirmed_in_3d") or []
    if ops_unconf:
        lines.append("⚠ Ouvertures suspectes ({}) :".format(len(ops_unconf)))
        for o in ops_unconf[:20]:
            lines.append("  [{}] {} pos={} sill={:.2f}m H={:.2f}m level={}".format(
                o.get("category"), o.get("llm_id"),
                ["{:.2f}".format(p) for p in o.get("position", [])],
                o.get("sill_m") or 0.0, o.get("head_m") or 0.0,
                o.get("level_name"),
            ))
        if len(ops_unconf) > 20:
            lines.append("  ... +{} autres".format(len(ops_unconf) - 20))
        lines.append("")

    lines.append("Note : {}".format(payload.get("note") or ""))
    return "\n".join(lines)


def _main():
    from lib import kg_sync
    from lib.tools.dwg_import import validate_import_3d

    try:
        uiapp = __revit__  # type: ignore[name-defined]
    except NameError:
        from pyrevit import HOST_APP
        uiapp = HOST_APP.uiapp

    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        _show("DEV Validate 3D", "Aucun document Revit actif.")
        return
    doc = uidoc.Document
    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show("DEV Validate 3D", "Sauvegarde le projet Revit d'abord.")
        return

    kg = kg_sync.open_or_create(doc)
    try:
        payload = validate_import_3d(kg=kg)
    except FileNotFoundError as exc:
        _show("DEV Validate 3D — coupe DXF introuvable", str(exc))
        return
    except ValueError as exc:
        _show("DEV Validate 3D — KG incomplet", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        _show("DEV Validate 3D — erreur",
              "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()))
        return

    _show("DEV Validate 3D — résultat", _format_report(payload))


try:
    _main()
except BaseException as exc:  # noqa: BLE001
    _show_error(
        "DEV Validate 3D failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
