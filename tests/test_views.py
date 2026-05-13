"""Tests tools/views.py — KG-only path (pas de Revit dans la harness).

Couvre :
- views_create_section : retour avec geom computed + revit_id=None hors-Revit.
- views_link_cad : validation inputs + retour avec link_revit_id=None.
- dxf_context_register_linked_view : append au context.

Les branches Revit (`doc is not None`) sont exercées en runtime via
prompt.pushbutton, pas testées ici.
"""
from __future__ import annotations

import json
from pathlib import Path

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


# ----- views_create_section --------------------------------------------


def test_create_section_kg_only_returns_geom(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "views_create_section",
        {
            "name": "Coupe 1",
            "p1_m": [-1.8, -6.0],
            "p2_m": [11.9, -6.0],
            "view_dir": "down",
        },
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["name"] == "Coupe 1"
    assert payload["revit_id"] is None  # hors-Revit
    assert payload["view_dir"] == "down"
    # Section length = ||p2 - p1|| = 11.9 - (-1.8) = 13.7m.
    assert payload["section_length_m"] == pytest.approx(13.7, abs=0.01)
    assert "doc is None" in payload["note"]


def test_create_section_rejects_empty_name(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "views_create_section",
        {"name": "   ", "p1_m": [0, 0], "p2_m": [10, 0], "view_dir": "up"},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True
    assert "non-empty" in result["content"]


def test_create_section_rejects_bad_view_dir(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "views_create_section",
        {"name": "X", "p1_m": [0, 0], "p2_m": [10, 0], "view_dir": "northwest"},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


def test_create_section_rejects_zero_length(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "views_create_section",
        {"name": "X", "p1_m": [5, 5], "p2_m": [5, 5], "view_dir": "down"},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True
    assert "zero length" in result["content"].lower()


# ----- views_link_cad --------------------------------------------------


def test_link_cad_kg_only_returns_placeholder(kg_fresh, tmp_path):
    dxf = tmp_path / "fake.dxf"
    dxf.write_text("0\nSECTION\n2\nENDSEC\n0\nEOF\n")
    result = llm_protocol.dispatch_tool_use(
        "views_link_cad",
        {"file_path": str(dxf), "view_revit_id": 42},
        "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["link_revit_id"] is None
    assert payload["view_revit_id"] == 42
    assert "doc is None" in payload["note"]


def test_link_cad_rejects_missing_file(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "views_link_cad",
        {"file_path": "/nonexistent.dxf", "view_revit_id": 42},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True
    assert "not found" in result["content"].lower()


def test_link_cad_rejects_bad_placement(kg_fresh, tmp_path):
    dxf = tmp_path / "x.dxf"
    dxf.write_text("0\nEOF\n")
    result = llm_protocol.dispatch_tool_use(
        "views_link_cad",
        {"file_path": str(dxf), "view_revit_id": 1, "placement": "diagonal"},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


# ----- dxf_context_register_linked_view --------------------------------


def test_register_linked_view_appends(kg_fresh):
    r1 = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view",
        {
            "file_path": "/tmp/plan.dxf",
            "link_revit_id": 100,
            "view_revit_id": 50,
            "view_kind": "plan",
            "view_name": "Niveau 0",
        },
        "t1", kg_fresh,
    )
    p1 = json.loads(r1["content"])
    assert p1["linked_view_index"] == 0
    assert p1["total_linked_views"] == 1

    r2 = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view",
        {
            "file_path": "/tmp/coupe1.dxf",
            "link_revit_id": 200,
            "view_revit_id": 60,
            "view_kind": "section",
        },
        "t2", kg_fresh,
    )
    p2 = json.loads(r2["content"])
    assert p2["total_linked_views"] == 2

    # Get the context — should have 2 linked_views.
    r3 = llm_protocol.dispatch_tool_use("dxf_context_get", {}, "t3", kg_fresh)
    payload = json.loads(r3["content"])
    assert len(payload["linked_views"]) == 2
    assert payload["linked_views"][0]["view_name"] == "Niveau 0"


def test_register_linked_view_rejects_bad_view_kind(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view",
        {
            "file_path": "/tmp/x.dxf",
            "link_revit_id": 1,
            "view_revit_id": 2,
            "view_kind": "elevation",  # invalid
        },
        "t1", kg_fresh,
    )
    assert result["is_error"] is True
    assert "view_kind" in result["content"].lower()


def test_register_linked_view_rejects_bad_ids(kg_fresh):
    """Les revit_ids doivent être > 0."""
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view",
        {
            "file_path": "/tmp/x.dxf",
            "link_revit_id": 0,
            "view_revit_id": 1,
            "view_kind": "plan",
        },
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


def test_register_linked_view_auto_creates_context(kg_fresh):
    """Pas de register_inspection préalable → register_linked_view crée
    le context."""
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view",
        {
            "file_path": "/tmp/x.dxf",
            "link_revit_id": 1,
            "view_revit_id": 2,
            "view_kind": "plan",
        },
        "t1", kg_fresh,
    )
    assert result["is_error"] is False
    assert kg_fresh.count_by_type("DxfImportContext") == 1
