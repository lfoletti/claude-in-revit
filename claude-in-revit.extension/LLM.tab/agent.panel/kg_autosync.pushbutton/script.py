#! python3
# -*- coding: utf-8 -*-
"""Toggle the KG auto-sync hook (`hooks/doc-changed.py`).

Wrapper UI mono-clic du file sentinel `~/.config/claude-in-revit/hooks.disabled` :
sa présence coupe le hook (mode manuel) ; son absence active le hook
(mode auto). Le pushbutton lit l'état actuel, montre une confirmation
Yes/No, et toggle si confirmé.

Mode AUTO (sentinel absent) :
- Chaque transaction Revit non-`[LLM] *` ajoute une ligne au buffer
  `pending_diffs.jsonl` du projet.
- Le buffer est consommé paresseusement au début du prochain tour agent
  (`kg.consume_pending_diffs()`).

Mode MANUAL (sentinel présent) :
- Le hook early-return immédiatement à chaque DocumentChanged.
- L'user doit cliquer Refresh KG pour synchroniser ; la drift detection
  des tools reste active comme filet de sécurité.

Pattern UI identique à `reset_dxf.pushbutton` (TaskDialog Yes/No).
Defensive shell `BaseException + traceback` comme tous nos pushbuttons.
"""
__title__ = "KG Auto-sync"
__doc__ = "Toggle l'auto-synchronisation Revit→KG via hook DocumentChanged."

import os
import sys
import traceback

# pyRevit met `<extension>/lib/` sur sys.path mais pas la racine.
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
    try:
        from lib.ui_dialogs import show_selectable_text
        show_selectable_text("claude-in-revit — {}".format(title), body)
    except Exception:  # noqa: BLE001
        _show_error(title, body)


def _confirm_toggle(currently_disabled: bool) -> bool:
    current_state = "désactivé (MANUEL)" if currently_disabled else "activé (AUTO)"
    target_state = "AUTO" if currently_disabled else "MANUEL"

    if currently_disabled:
        future_behavior = (
            "Chaque édition Revit (non issue de l'agent) sera captée par un "
            "hook après commit et propagée au KG au prochain clic Prompt. "
            "Plus besoin de cliquer Refresh KG manuellement entre 2 tours."
        )
    else:
        future_behavior = (
            "Le hook DocumentChanged sera ignoré. Tu devras cliquer Refresh "
            "KG manuellement après toute édition Revit hors-agent pour "
            "garder le KG à jour. La drift detection des tools reste active "
            "comme filet de sécurité."
        )

    dlg = TaskDialog("claude-in-revit — KG Auto-sync")
    dlg.MainInstruction = "État actuel : auto-sync {}".format(current_state)
    dlg.MainContent = (
        "Basculer en mode {} ?\n\n{}\n\n"
        "Le toggle est immédiat et persistant entre sessions Revit "
        "(file sentinel `~/.config/claude-in-revit/hooks.disabled`)."
    ).format(target_state, future_behavior)
    dlg.CommonButtons = TaskDialogCommonButtons.Yes | TaskDialogCommonButtons.No
    dlg.DefaultButton = TaskDialogResult.No
    return dlg.Show() == TaskDialogResult.Yes


def _main():
    from lib import config

    currently_disabled = config.are_hooks_disabled()

    if not _confirm_toggle(currently_disabled):
        return

    new_disabled = config.set_hooks_disabled(not currently_disabled)
    new_state = "MANUEL (hook désactivé)" if new_disabled else "AUTO (hook actif)"
    sentinel = config.hooks_disabled_file()
    _show(
        "KG Auto-sync",
        "Mode basculé : {}.\n\n"
        "Sentinel : {} {}\n\n"
        "Aucun redémarrage Revit nécessaire — le hook lit le sentinel à "
        "chaque déclenchement.".format(
            new_state,
            sentinel,
            "(présent)" if new_disabled else "(absent)",
        ),
    )


try:
    _main()
except BaseException as exc:  # noqa: BLE001
    _show_error(
        "KG Auto-sync failed",
        "{}: {}\n\n{}".format(type(exc).__name__, exc, traceback.format_exc()),
    )
