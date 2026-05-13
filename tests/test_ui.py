"""Tests tools/ui.py — branche hors-Revit (doc=None).

La vraie branche Revit (TaskDialog) n'est pas testable depuis pytest —
elle est exercée en runtime via prompt.pushbutton. Ici on vérifie :
- Validation des inputs (title, message, choices)
- Fallback default_choice quand doc=None
- ui_confirm_yes_no wrapping correct sur ui_confirm_choices
"""
from __future__ import annotations

import json

import pytest

from lib import llm_protocol
from lib.project_kg import ProjectKG


@pytest.fixture
def kg_fresh():
    llm_protocol.reset_registry()
    llm_protocol.get_registry()
    kg = ProjectKG("p")
    kg.advance_turn()
    return kg


# ----- ui_confirm_choices ---------------------------------------------


def test_confirm_choices_returns_default_kg_only(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_choices",
        {
            "title": "Direction Coupe 1",
            "message": "Confirmer la direction de regard ?",
            "choices": ["right (Est)", "left (Ouest)"],
            "default_choice": "right (Est)",
        },
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["choice"] == "right (Est)"
    assert payload["cancelled"] is False
    assert payload["ran_in_revit"] is False


def test_confirm_choices_falls_back_to_first_choice(kg_fresh):
    """default_choice non spécifié → 1er choix retourné."""
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_choices",
        {"title": "X", "message": "Y", "choices": ["A", "B", "C"]},
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["choice"] == "A"


def test_confirm_choices_rejects_empty_title(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_choices",
        {"title": "  ", "message": "?", "choices": ["A"]},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


def test_confirm_choices_rejects_empty_choices(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_choices",
        {"title": "X", "message": "Y", "choices": []},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


def test_confirm_choices_rejects_too_many_choices(kg_fresh):
    """TaskDialog max 4 CommandLinks."""
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_choices",
        {
            "title": "X", "message": "Y",
            "choices": ["A", "B", "C", "D", "E"],  # 5 choices
        },
        "t1", kg_fresh,
    )
    assert result["is_error"] is True
    assert "max" in result["content"].lower()


def test_confirm_choices_rejects_empty_choice_string(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_choices",
        {"title": "X", "message": "Y", "choices": ["A", "  "]},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


# ----- ui_confirm_yes_no ----------------------------------------------


def test_confirm_yes_no_default_yes(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_yes_no",
        {"title": "Confirmer ?", "message": "OK ?"},
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["yes"] is True
    assert payload["ran_in_revit"] is False


def test_confirm_yes_no_default_no(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_yes_no",
        {
            "title": "Confirmer ?", "message": "OK ?",
            "default_yes": False,
        },
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["yes"] is False


def test_confirm_yes_no_custom_labels(kg_fresh):
    """Labels custom 'Continuer'/'Annuler' avec default 'Continuer'."""
    result = llm_protocol.dispatch_tool_use(
        "ui_confirm_yes_no",
        {
            "title": "X", "message": "?",
            "yes_label": "Continuer", "no_label": "Annuler",
            "default_yes": True,
        },
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["yes"] is True
