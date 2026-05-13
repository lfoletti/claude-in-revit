"""Tests DxfImportContext + tools dxf_context_* (Phase 1 import DXF).

Couvre :
- Schema KG (node DxfImportContext accepte directory + optionnels).
- dxf_context_get : retour vide quand pas de contexte, retour rempli sinon.
- dxf_context_register_inspection : crée le 1er, met à jour le 2e.
- dxf_context_register_section_line : append-only, auto-create si absent.
- Persistance entre tours simulée (kg.advance_turn).
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


# ----- Schema KG -------------------------------------------------------


def test_dxf_import_context_node_minimal_attrs(kg_fresh):
    kg = kg_fresh
    nid = kg.add_node("DxfImportContext", {"directory": "/tmp/proj"})
    node = kg.get_node(nid)
    assert node["_type"] == "DxfImportContext"
    assert node["directory"] == "/tmp/proj"


def test_dxf_import_context_node_accepts_optional_attrs(kg_fresh):
    kg = kg_fresh
    nid = kg.add_node("DxfImportContext", {
        "directory": "/tmp/proj",
        "source": "revit_aia",
        "files": [{"path": "/tmp/x.dxf", "kind": "plan"}],
        "section_lines": [],
        "linked_views": [],
    })
    assert kg.get_node(nid)["source"] == "revit_aia"


# ----- dxf_context_get : empty / live ---------------------------------


def test_get_returns_empty_when_no_context(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_get", {}, "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["exists"] is False
    assert payload["llm_id"] is None
    assert payload["files"] == []
    assert payload["section_lines"] == []


def test_get_returns_live_context(kg_fresh):
    kg = kg_fresh
    llm_protocol.dispatch_tool_use(
        "dxf_context_register_inspection",
        {
            "directory": "/tmp/proj",
            "inspection": {
                "files": [
                    {"path": "/tmp/plan.dxf", "name": "plan.dxf", "kind": "plan"},
                    {"path": "/tmp/coupe1.dxf", "name": "coupe1.dxf", "kind": "section"},
                ],
            },
        },
        "t1", kg,
    )
    result = llm_protocol.dispatch_tool_use("dxf_context_get", {}, "t2", kg)
    payload = json.loads(result["content"])
    assert payload["exists"] is True
    assert payload["directory"] == "/tmp/proj"
    assert len(payload["files"]) == 2
    assert payload["files"][0]["kind"] == "plan"


# ----- dxf_context_register_inspection --------------------------------


def test_register_inspection_creates_context(kg_fresh):
    kg = kg_fresh
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_inspection",
        {
            "directory": "/tmp/proj",
            "inspection": {"files": [{"path": "/tmp/x.dxf", "kind": "plan"}]},
            "source": "revit_aia",
        },
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["files_count"] == 1
    # KG carries the source.
    node = kg.get_node(payload["llm_id"])
    assert node["source"] == "revit_aia"


def test_register_inspection_idempotent_updates(kg_fresh):
    """2e appel met à jour, ne crée pas de doublon."""
    kg = kg_fresh
    r1 = llm_protocol.dispatch_tool_use(
        "dxf_context_register_inspection",
        {"directory": "/tmp/a", "inspection": {"files": [{"path": "/tmp/a/p.dxf"}]}},
        "t1", kg,
    )
    p1 = json.loads(r1["content"])
    r2 = llm_protocol.dispatch_tool_use(
        "dxf_context_register_inspection",
        {
            "directory": "/tmp/b",
            "inspection": {"files": [{"path": "/tmp/b/p.dxf"}, {"path": "/tmp/b/c.dxf"}]},
        },
        "t2", kg,
    )
    p2 = json.loads(r2["content"])
    assert p1["llm_id"] == p2["llm_id"]
    assert p2["created"] is False
    assert p2["files_count"] == 2
    assert kg.count_by_type("DxfImportContext") == 1


def test_register_inspection_rejects_empty_directory(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_inspection",
        {"directory": "", "inspection": {"files": []}},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


# ----- dxf_context_register_section_line ------------------------------


def test_register_section_line_appends(kg_fresh):
    kg = kg_fresh
    r1 = llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line",
        {
            "coupe_path": "/tmp/coupe1.dxf",
            "plan_p1": [0.0, 0.0],
            "plan_p2": [20.0, 0.0],
            "view_dir": "up",
            "name": "Coupe 1",
        },
        "t1", kg,
    )
    p1 = json.loads(r1["content"])
    assert p1["section_line_index"] == 0
    assert p1["total_section_lines"] == 1

    r2 = llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line",
        {
            "coupe_path": "/tmp/coupe2.dxf",
            "plan_p1": [10.0, 0.0],
            "plan_p2": [10.0, 20.0],
            "view_dir": "left",
            "name": "Coupe 2",
        },
        "t2", kg,
    )
    p2 = json.loads(r2["content"])
    assert p2["section_line_index"] == 1
    assert p2["total_section_lines"] == 2

    # Context only created once.
    assert kg.count_by_type("DxfImportContext") == 1


def test_register_section_line_auto_creates_context(kg_fresh):
    """Pas d'inspection préalable → register_section_line crée le context."""
    kg = kg_fresh
    r = llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line",
        {
            "coupe_path": "/tmp/c.dxf",
            "plan_p1": [0.0, 0.0],
            "plan_p2": [10.0, 0.0],
            "view_dir": "down",
        },
        "t1", kg,
    )
    payload = json.loads(r["content"])
    assert payload["ok"] is True
    assert kg.count_by_type("DxfImportContext") == 1


def test_register_section_line_rejects_bad_view_dir(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line",
        {
            "coupe_path": "/tmp/c.dxf",
            "plan_p1": [0.0, 0.0],
            "plan_p2": [10.0, 0.0],
            "view_dir": "northwest",  # invalid
        },
        "t1", kg_fresh,
    )
    assert result["is_error"] is True
    assert "view_dir" in result["content"].lower()


def test_register_section_line_persists_user_confirmation_flag(kg_fresh):
    """Le flag confirmed_by_user doit être stocké tel quel."""
    kg = kg_fresh
    llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line",
        {
            "coupe_path": "/tmp/c.dxf",
            "plan_p1": [0.0, 0.0],
            "plan_p2": [10.0, 0.0],
            "view_dir": "right",
            "confirmed_by_user": True,
            "scale_verified": True,
            "drift_pct": 0.5,
        },
        "t1", kg,
    )
    r = llm_protocol.dispatch_tool_use("dxf_context_get", {}, "t2", kg)
    payload = json.loads(r["content"])
    sl = payload["section_lines"][0]
    assert sl["confirmed_by_user"] is True
    assert sl["scale_verified"] is True
    assert sl["drift_pct"] == 0.5


# ----- Integration : inspection → section_line → get -----------------


def test_register_linked_view_many_bulk(kg_fresh):
    """Bulk register : 4 linked views en 1 appel."""
    entries = [
        {"file_path": f"/tmp/f{i}.dxf", "link_revit_id": 100 + i,
         "view_revit_id": 50 + i, "view_kind": "section",
         "view_name": f"V{i}"}
        for i in range(4)
    ]
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view_many",
        {"entries": entries}, "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 4
    assert payload["total_linked_views"] == 4
    # Verify via get
    r2 = llm_protocol.dispatch_tool_use("dxf_context_get", {}, "t2", kg_fresh)
    p2 = json.loads(r2["content"])
    assert len(p2["linked_views"]) == 4


def test_register_linked_view_many_rejects_bad_view_kind(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_linked_view_many",
        {"entries": [
            {"file_path": "/tmp/f.dxf", "link_revit_id": 1,
             "view_revit_id": 2, "view_kind": "elevation"},  # valid now
            {"file_path": "/tmp/g.dxf", "link_revit_id": 3,
             "view_revit_id": 4, "view_kind": "garbage"},  # invalid
        ]},
        "t1", kg_fresh,
    )
    assert result["is_error"] is True


def test_register_section_line_many_bulk(kg_fresh):
    """Bulk register section lines : 3 traits en 1 appel."""
    sls = [
        {
            "coupe_path": f"/tmp/c{i}.dxf",
            "plan_p1": [0.0, float(i)],
            "plan_p2": [10.0, float(i)],
            "view_dir": "up",
            "name": f"Coupe {i}",
        }
        for i in range(3)
    ]
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line_many",
        {"section_lines": sls}, "t1", kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 3
    assert payload["total_section_lines"] == 3


def test_register_section_line_many_rejects_empty(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line_many",
        {"section_lines": []}, "t1", kg_fresh,
    )
    assert result["is_error"] is True


def test_full_phase1_flow_persists_across_turns(kg_fresh):
    """Simule : tour 1 inspect+register ; tour 2 register section_line ;
    tour 3 get retourne tout. Pattern attendu côté agent runtime."""
    kg = kg_fresh
    # Tour 1
    llm_protocol.dispatch_tool_use(
        "dxf_context_register_inspection",
        {
            "directory": "/tmp/Projet4/DXF",
            "inspection": {"files": [
                {"path": "/tmp/Projet4/DXF/plan.dxf", "kind": "plan", "name": "plan.dxf"},
                {"path": "/tmp/Projet4/DXF/c1.dxf", "kind": "section", "name": "c1.dxf"},
                {"path": "/tmp/Projet4/DXF/c2.dxf", "kind": "section", "name": "c2.dxf"},
            ]},
            "source": "revit_aia",
        },
        "t1", kg,
    )
    kg.advance_turn()
    # Tour 2
    llm_protocol.dispatch_tool_use(
        "dxf_context_register_section_line",
        {
            "coupe_path": "/tmp/Projet4/DXF/c1.dxf",
            "plan_p1": [0.0, 10.0],
            "plan_p2": [20.0, 10.0],
            "view_dir": "down",
            "name": "Coupe 1",
            "confirmed_by_user": True,
        },
        "t2", kg,
    )
    kg.advance_turn()
    # Tour 3 — get full state
    r = llm_protocol.dispatch_tool_use("dxf_context_get", {}, "t3", kg)
    payload = json.loads(r["content"])
    assert payload["exists"] is True
    assert payload["source"] == "revit_aia"
    assert len(payload["files"]) == 3
    assert len(payload["section_lines"]) == 1
    assert payload["section_lines"][0]["name"] == "Coupe 1"
