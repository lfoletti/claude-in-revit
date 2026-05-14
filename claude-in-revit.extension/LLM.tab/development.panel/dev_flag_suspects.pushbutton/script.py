#! python3
# -*- coding: utf-8 -*-
"""DEV: invoke `dwg_flag_3d_suspects_in_view` sur la vue active.

Mutate Revit (overrides graphics) mais pas le KG. Réversible via
`DEV Clear Overrides` ou manuellement (Properties → V/G dans Revit).
"""
__title__ = "DEV Flag 3D Suspects"
__doc__ = "Peint en rouge/jaune les suspects identifiés par validation 3D."

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
    lines = ["DEV Flag 3D Suspects — résultat"]
    lines.append("")
    lines.append("view_revit_id : {}".format(payload.get("view_revit_id")))
    lines.append("rouge (suspect fantôme)        : {}".format(payload.get("red_count")))
    lines.append("jaune (sans évidence 3D)       : {}".format(payload.get("yellow_count")))
    lines.append("total flaggé                   : {}".format(payload.get("total_flagged")))
    lines.append("skipped                        : {}".format(payload.get("skipped_count")))
    lines.append("")
    v = payload.get("validation") or {}
    s = v.get("summary") or {}
    lines.append("Validation summary :")
    lines.append("  total elements : {}".format(s.get("total_elements")))
    lines.append("  total suspects : {}".format(s.get("total_suspects")))
    lines.append("")
    lines.append("Note : {}".format(payload.get("note")))
    return "\n".join(lines)


def _main():
    from lib import kg_sync
    from lib.tools.dwg_import import flag_3d_suspects_in_view

    try:
        uiapp = __revit__  # type: ignore[name-defined]
    except NameError:
        from pyrevit import HOST_APP
        uiapp = HOST_APP.uiapp

    uidoc = uiapp.ActiveUIDocument
    if uidoc is None:
        _show("DEV Flag 3D Suspects", "Aucun document Revit actif.")
        return
    doc = uidoc.Document
    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show("DEV Flag 3D Suspects", "Sauvegarde le projet Revit d'abord.")
        return

    kg = kg_sync.open_or_create(doc)

    try:
        payload = flag_3d_suspects_in_view(kg=kg, doc=doc)
    except FileNotFoundError as exc:
        _show("DEV Flag 3D Suspects — fichier manquant", str(exc))
        return
    except ValueError as exc:
        _show("DEV Flag 3D Suspects — input invalide", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        _show("DEV Flag 3D Suspects — erreur",
              "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()))
        return

    _show("DEV Flag 3D Suspects — résultat", _format_report(payload))


try:
    _main()
except BaseException as exc:  # noqa: BLE001
    _show_error(
        "DEV Flag 3D Suspects failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
