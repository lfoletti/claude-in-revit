#! python3
# -*- coding: utf-8 -*-
"""Reset DXF imports — soft-delete tous les Wall/Window/Door/Floor + types
custom `DXF_*` du KG, et supprime côté Revit en une seule transaction.

Wrapper UI mono-clic du tool `kg_reset_dxf_imports` (lib/tools/dwg_import.py).
But : court-circuiter le tour LLM nécessaire pour invoquer le tool quand
le développeur itère sur l'import DXF (Phase 2a/2b/2c) et veut repartir
vierge entre 2 essais. ~80-150 tokens + 1 round-trip LLM économisés
par clic, et indépendant de la disponibilité Anthropic.

Flow :
  1. Dry-run pour obtenir l'inventaire.
  2. Confirm dialog (Yes/No) qui liste ce qui sera supprimé.
  3. Si confirmé : reset effectif, dialog de résultat.

Préserve : Levels, Rooms, DxfImportContext, Views. Pour un reset total,
supprimer le `.kg.json` à la main (cf. §4.1 design doc).

Defensive shell (BaseException + traceback) identique à `refresh_kg.pushbutton`.
"""
__title__ = "Reset DXF"
__doc__ = "Soft-delete les imports DXF (Wall/Window/Door/Floor + types DXF_*)."

import os
import sys
import traceback

# pyRevit met `<extension>/lib/` sur sys.path mais pas la racine de
# l'extension — cf. CLAUDE.md gotchas CPython.
_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)

from Autodesk.Revit.UI import (
    TaskDialog,
    TaskDialogCommonButtons,
    TaskDialogResult,
)


def _show_error(title, body):
    TaskDialog.Show("claude-in-revit — {}".format(title), body)


def _show(title, body):
    """Dialog sélectionnable + copiable pour les outputs normaux ; TaskDialog
    nu en fallback si `lib.ui_dialogs` n'est pas importable."""
    try:
        from lib.ui_dialogs import show_selectable_text
        show_selectable_text("claude-in-revit — {}".format(title), body)
    except Exception:  # noqa: BLE001
        _show_error(title, body)


def _confirm_reset(inventory) -> bool:
    """Modal TaskDialog Yes/No listant ce qui sera supprimé."""
    parts = []
    for label, key in (
        ("Murs", "walls"),
        ("Fenêtres", "windows"),
        ("Portes", "doors"),
        ("Sols", "floors"),
        ("WallTypes DXF_WALL_*", "wall_types"),
        ("FloorTypes DXF_FLOOR_*", "floor_types"),
        ("FamilyTypes DXF_WIN_*/DXF_DOOR_*", "family_types"),
    ):
        cnt = inventory[key]["count"]
        if cnt:
            parts.append("  • {} : {}".format(label, cnt))

    dlg = TaskDialog("claude-in-revit — Reset DXF")
    dlg.MainInstruction = "Confirmer la suppression ?"
    dlg.MainContent = (
        "Les éléments suivants seront supprimés côté Revit ET soft-delete "
        "dans le KG :\n\n{}\n\n"
        "Préservés : Levels, Rooms, DxfImportContext, Views.\n\n"
        "L'opération est atomique (rollback symétrique si l'un des deux "
        "côtés échoue). Aucun undo Revit après confirmation — relancer "
        "l'import depuis les DXFs pour rétablir.".format("\n".join(parts))
    )
    dlg.CommonButtons = TaskDialogCommonButtons.Yes | TaskDialogCommonButtons.No
    dlg.DefaultButton = TaskDialogResult.No
    return dlg.Show() == TaskDialogResult.Yes


def _format_result(result) -> str:
    inv = (
        ("Murs", "walls"),
        ("Fenêtres", "windows"),
        ("Portes", "doors"),
        ("Sols", "floors"),
        ("WallTypes", "wall_types"),
        ("FloorTypes", "floor_types"),
        ("FamilyTypes", "family_types"),
    )
    lines = ["Reset DXF terminé."]
    lines.append("")
    lines.append("Inventaire ciblé :")
    for label, key in inv:
        lines.append("  • {} : {}".format(label, result[key]["count"]))
    lines.append("")
    lines.append("Suppression Revit : {}".format(result["revit_deleted"]))
    lines.append("Déjà absents / refusés : {}".format(result["already_gone"]))
    lines.append("Tour KG : {}".format(result["deleted_at_turn"]))
    return "\n".join(lines)


def _main():
    from lib import kg_sync
    from lib.tools import dwg_import

    try:
        uiapp = __revit__  # type: ignore[name-defined]
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
        _show("Reset DXF", "Aucun document Revit actif. Ouvre un projet, puis recommence.")
        return

    doc = uidoc.Document
    if doc is None:
        raise RuntimeError(
            "ActiveUIDocument has no Document — model may be in a "
            "transient open/close state. Try again."
        )

    path_name = (getattr(doc, "PathName", "") or "").strip()
    if not path_name:
        _show(
            "Reset DXF",
            "Le projet Revit n'est pas encore sauvegardé.\n\n"
            "Enregistre-le d'abord (Fichier → Enregistrer sous), puis "
            "recommence — l'identifiant du KG est dérivé du chemin du "
            "`.rvt`.",
        )
        return

    kg = kg_sync.open_or_create(doc)

    # 1. Dry-run pour obtenir l'inventaire ciblé sans rien muter.
    preview = dwg_import.kg_reset_dxf_imports(kg=kg, doc=doc, dry_run=True)
    total = sum(
        preview[k]["count"]
        for k in ("walls", "windows", "doors", "floors",
                  "wall_types", "floor_types", "family_types")
    )
    if total == 0:
        _show(
            "Reset DXF",
            "Rien à reset — le KG n'a aucun import DXF actif "
            "(Wall/Window/Door/Floor + types DXF_*).",
        )
        return

    # 2. Confirmation user.
    if not _confirm_reset(preview):
        return

    # 3. Reset effectif. La Tx KG + Tx Revit sont commités atomiquement
    #    par le tool ; en cas de crash Revit, le KG rollback.
    result = dwg_import.kg_reset_dxf_imports(kg=kg, doc=doc, dry_run=False)
    _show("Reset DXF", _format_result(result))


try:
    _main()
except BaseException as exc:  # noqa: BLE001 — surface .NET exceptions too.
    _show_error(
        "Reset DXF failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
