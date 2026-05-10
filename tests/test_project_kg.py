"""Tests for lib.project_kg — schema, lifecycle, persistence, transactions."""
from __future__ import annotations

import pytest

from lib.project_kg import (
    CREATED_AT,
    DELETED_AT,
    MODIFIED_AT,
    ProjectKG,
)


def _seed(kg: ProjectKG) -> tuple:
    kg.advance_turn()  # turn 1
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})
    return level, wt


def test_add_node_assigns_llm_id_and_lifecycle_attrs():
    kg = ProjectKG("p")
    kg.advance_turn()
    nid = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    assert nid == "level_001"
    node = kg.get_node(nid)
    assert node[CREATED_AT] == 1
    assert node[MODIFIED_AT] == []
    assert node[DELETED_AT] is None
    assert node["_type"] == "Level"


def test_add_node_rejects_unknown_type():
    kg = ProjectKG("p")
    with pytest.raises(ValueError, match="Unknown node type"):
        kg.add_node("Sofa", {"name": "x"})


def test_add_node_rejects_missing_required_attrs():
    kg = ProjectKG("p")
    with pytest.raises(ValueError, match="Missing required"):
        kg.add_node("Level", {"name": "N00"})  # missing elevation


def test_add_node_rejects_unknown_attrs():
    kg = ProjectKG("p")
    with pytest.raises(ValueError, match="Unknown attrs"):
        kg.add_node("Level", {"name": "N00", "elevation": 0.0, "color": "red"})


def test_modify_node_logs_history_and_marks_modified_at_turn():
    kg = ProjectKG("p")
    _, wt = _seed(kg)
    kg.advance_turn()  # turn 2
    kg.modify_node(wt, {"total_thickness": 0.25})
    node = kg.get_node(wt)
    assert node["total_thickness"] == 0.25
    assert node[MODIFIED_AT] == [2]
    log = kg.action_log
    assert log[-1]["action"] == "modify"
    assert log[-1]["details"]["before"] == {"total_thickness": 0.2}


def test_soft_delete_marks_node_and_excludes_from_default_queries():
    kg = ProjectKG("p")
    level, _ = _seed(kg)
    kg.advance_turn()
    kg.soft_delete(level)
    assert kg.get_node(level)[DELETED_AT] == 2
    assert kg.find_by_type("Level") == []
    assert kg.find_by_type("Level", include_deleted=True) == [level]


def test_add_edge_validates_type_and_endpoints():
    kg = ProjectKG("p")
    level, wt = _seed(kg)
    with pytest.raises(ValueError, match="Unknown edge type"):
        kg.add_edge(level, wt, "is_friends_with")
    with pytest.raises(KeyError):
        kg.add_edge(level, "wall_999", "at_level")


def test_persistence_roundtrip(tmp_path):
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    level, wt = _seed(kg)
    kg.add_edge(level, wt, "at_level")  # edge type doesn't matter for the test
    kg.persist()

    loaded = ProjectKG.load(persist)
    assert loaded.project_id == "p"
    assert loaded.turn == 1
    assert loaded.find_by_type("Level") == [level]
    assert loaded.find_by_type("WallType") == [wt]
    assert loaded.action_log == kg.action_log
    # Edges survived
    assert loaded._g.number_of_edges() == 1  # noqa: SLF001


def test_transaction_persists_on_success(tmp_path):
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    with kg.transaction():
        kg.advance_turn()
        kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    assert persist.exists()
    loaded = ProjectKG.load(persist)
    assert loaded.find_by_type("Level")


def test_transaction_rolls_back_on_exception(tmp_path):
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    _seed(kg)
    kg.persist()
    pre_log = kg.action_log
    pre_levels = kg.find_by_type("Level")

    with pytest.raises(RuntimeError, match="boom"):
        with kg.transaction():
            kg.advance_turn()
            kg.add_node("Level", {"name": "rollback_me", "elevation": 5.0})
            raise RuntimeError("boom")

    # In-memory state restored
    assert kg.action_log == pre_log
    assert kg.find_by_type("Level") == pre_levels
    # Disk state unchanged (still pre-rollback content)
    loaded = ProjectKG.load(persist)
    assert loaded.action_log == pre_log
    assert loaded.find_by_type("Level") == pre_levels


def test_diff_since_returns_actions_at_or_after_turn():
    kg = ProjectKG("p")
    _seed(kg)  # turn 1
    kg.advance_turn()
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})  # turn 2
    diff = kg.diff_since(2)
    assert len(diff) == 1
    assert diff[0]["turn"] == 2
