"""Smoke tests for the canonical tool registry — confirms each shipped tool
imports and dispatches without surprises."""
from __future__ import annotations

import json

import pytest

from lib import llm_protocol
from lib.project_kg import ProjectKG


@pytest.fixture
def kg_with_seed():
    """A KG carrying one Level + one WallType so walls_create has refs to use."""
    llm_protocol.reset_registry()
    # Trigger auto-import of lib.tools — populates the registry.
    llm_protocol.get_registry()

    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})
    return kg, level, wt


def test_canonical_registry_has_expected_tier1_tools(kg_with_seed):
    registry = llm_protocol.get_registry()
    expected = {
        "catalog_list_levels",
        "catalog_list_wall_types",
        "walls_create",
        "query_find_by_name",
        "aggregations_count",
    }
    assert expected.issubset(set(registry.keys()))


def test_catalog_list_levels(kg_with_seed):
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_levels", {}, "t1", kg
    )
    payload = json.loads(result["content"])
    assert any(l["llm_id"] == level for l in payload["levels"])


def test_walls_create_links_level_and_wall_type(kg_with_seed):
    kg, level, wt = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "walls_create",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "p1": [0.0, 0.0],
            "p2": [3.0, 4.0],
            "height": 2.7,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["length_m"] == 5.0  # 3-4-5 triangle

    # Walls were linked at_level + is_type
    wall_id = payload["llm_id"]
    edges = list(kg._g.out_edges(wall_id, keys=True))  # noqa: SLF001
    edge_types = sorted(k for _, _, k in edges)
    assert edge_types == ["at_level", "is_type"]


def test_walls_create_with_unknown_level_returns_error(kg_with_seed):
    kg, _, wt = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "walls_create",
        {
            "level_ref": "level_999",  # doesn't exist
            "wall_type_ref": wt,
            "p1": [0.0, 0.0],
            "p2": [1.0, 0.0],
            "height": 2.7,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "Unknown level_ref" in result["content"]
    # KG unchanged — no wall was created
    assert kg.count_by_type("Wall") == 0


def test_aggregations_count(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "aggregations_count", {"node_type": "Level"}, "t1", kg
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 1


def test_query_find_by_name(kg_with_seed):
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "query_find_by_name",
        {"name": "N00", "node_type": "Level"},
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["llm_id"] == level
