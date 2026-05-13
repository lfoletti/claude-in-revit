"""tools/ui.py — dialogues modaux Revit pour confirmations user en cours
de tour.

Quand l'agent a besoin d'une décision discrète (confirmation, choix
parmi quelques options), au lieu de prompter par texte conversationnel
(lent à taper), il ouvre un `TaskDialog` Revit avec CommandLinks. UX :
1 clic vs ligne de texte.

**Bloquant tool-side** : le tool attend que l'user clique avant de
retourner. Mais c'est très rapide (UX immédiate). Pour du vraiment
non-bloquant (l'user peut continuer à travailler pendant la décision),
voir Option B dans le journal — pas implémenté V0.

**Branche `doc is None`** : la harness pytest n'a pas d'UI Revit. Le
tool retourne le `default_choice` si fourni, sinon `auto_pick_index=0`.
Permet de tester sans Revit.

Tier-1 : ces dialogs sont des primitives utilisées partout dans le
workflow, pas spécifiques à un domaine.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG


_MAX_CHOICES = 4  # TaskDialog supporte CommandLink1 à CommandLink4


@tool(name="ui_confirm_choices", tier=1)
def confirm_choices(
    kg: ProjectKG,
    doc: Any,
    title: str,
    message: str,
    choices: List[str],
    description: Optional[str] = None,
    cancel_allowed: bool = True,
    default_choice: Optional[str] = None,
) -> Dict[str, Any]:
    """Ouvre un dialogue Revit modal avec jusqu'à 4 boutons larges.

    Retourne le label du choix cliqué par l'user. UX : remplace une
    demande de confirmation conversationnelle par 1 clic. Utiliser
    quand l'agent a besoin d'une décision binaire ou discrète parmi
    quelques options (confirmation de direction de coupe, choix de
    template, validation de plan d'action, etc.).

    Concepts: dialog, confirmation, ui, user, choix, validation,
              non-bloquant, dialogue, modal, taskdialog
    Phrases: "demande à l'utilisateur", "fait choisir", "ask the user",
             "dialog confirm", "confirme avec l'user"
    Similar: ui_confirm_yes_no, ui_show_text

    Args:
        title: titre du dialog.
        message: question/instruction principale (1-2 lignes).
        choices: liste des labels de boutons. Max 4. Le label retourné
            est exactement le string passé (l'agent peut donc encoder
            ce qu'il veut).
        description: texte de détails sous le message principal
            (optionnel, peut servir à expliquer le contexte).
        cancel_allowed: si True (défaut), ajoute un bouton Annuler. Si
            cliqué, le tool retourne `choice=None` et `cancelled=True`.
        default_choice: valeur de retour en mode hors-Revit (doc=None).
            Si None, retourne le 1er choix. Utile pour les tests.

    Returns:
        {"ok": bool, "choice": str | None, "cancelled": bool,
         "ran_in_revit": bool}
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    if not isinstance(choices, list) or not choices:
        raise ValueError("choices must be a non-empty list")
    if len(choices) > _MAX_CHOICES:
        raise ValueError(
            "TaskDialog supporte max {} choix (got {})".format(
                _MAX_CHOICES, len(choices),
            )
        )
    for i, c in enumerate(choices):
        if not isinstance(c, str) or not c.strip():
            raise ValueError(
                "choices[{}] must be a non-empty string".format(i)
            )

    if doc is None:
        # Hors-Revit : retourne le default ou le 1er choix, sans UI.
        chosen = default_choice if default_choice in choices else choices[0]
        return {
            "ok": True,
            "choice": chosen,
            "cancelled": False,
            "ran_in_revit": False,
            "note": "doc is None — UI bypassed, returned default choice.",
        }

    # Revit-side : open TaskDialog with CommandLinks.
    from Autodesk.Revit.UI import (
        TaskDialog, TaskDialogCommandLinkId, TaskDialogCommonButtons,
        TaskDialogResult,
    )

    dialog = TaskDialog(title.strip())
    dialog.MainInstruction = message.strip()
    if description:
        dialog.MainContent = description.strip()

    if cancel_allowed:
        dialog.CommonButtons = TaskDialogCommonButtons.Cancel
    else:
        dialog.CommonButtons = TaskDialogCommonButtons.NoButton

    # Map CommandLink IDs to indices.
    link_ids = [
        TaskDialogCommandLinkId.CommandLink1,
        TaskDialogCommandLinkId.CommandLink2,
        TaskDialogCommandLinkId.CommandLink3,
        TaskDialogCommandLinkId.CommandLink4,
    ]
    for i, choice_label in enumerate(choices):
        dialog.AddCommandLink(link_ids[i], choice_label.strip())

    result = dialog.Show()

    # Map result back to choice.
    cancelled = False
    chosen: Optional[str] = None
    if result == TaskDialogResult.CommandLink1:
        chosen = choices[0]
    elif result == TaskDialogResult.CommandLink2 and len(choices) >= 2:
        chosen = choices[1]
    elif result == TaskDialogResult.CommandLink3 and len(choices) >= 3:
        chosen = choices[2]
    elif result == TaskDialogResult.CommandLink4 and len(choices) >= 4:
        chosen = choices[3]
    elif result == TaskDialogResult.Cancel:
        cancelled = True
    # else: result == TaskDialogResult.None ou autre — fallback
    # (l'user a fermé via X) → cancelled.
    if chosen is None and not cancelled:
        cancelled = True

    return {
        "ok": True,
        "choice": chosen,
        "cancelled": cancelled,
        "ran_in_revit": True,
    }


@tool(name="ui_confirm_yes_no", tier=1)
def confirm_yes_no(
    kg: ProjectKG,
    doc: Any,
    title: str,
    message: str,
    description: Optional[str] = None,
    yes_label: str = "Oui",
    no_label: str = "Non",
    default_yes: bool = True,
) -> Dict[str, Any]:
    """Variante 2-choix `ui_confirm_choices` : Oui / Non, retourne bool.

    Concepts: yes, no, confirmation, dialog, ui, binary, user
    Phrases: "demande oui ou non", "confirm yes no", "valider l'action"
    Similar: ui_confirm_choices, ui_show_text

    Args:
        title: titre du dialog.
        message: question (1-2 lignes).
        description: détails optionnels.
        yes_label / no_label: labels custom (défaut "Oui"/"Non").
        default_yes: réponse hors-Revit si doc=None.

    Returns:
        {"ok": bool, "yes": bool, "cancelled": bool, "ran_in_revit": bool}
    """
    res = confirm_choices(
        kg, doc,
        title=title, message=message,
        choices=[yes_label, no_label],
        description=description,
        cancel_allowed=True,
        default_choice=yes_label if default_yes else no_label,
    )
    if res.get("cancelled"):
        return {
            "ok": True, "yes": False, "cancelled": True,
            "ran_in_revit": res.get("ran_in_revit", False),
        }
    return {
        "ok": True,
        "yes": res["choice"] == yes_label,
        "cancelled": False,
        "ran_in_revit": res.get("ran_in_revit", False),
    }
