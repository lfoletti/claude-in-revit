"""Tests for lib.project_kg — schema, lifecycle, persistence, transactions."""
from __future__ import annotations

import pytest

from lib.project_kg import (
    CREATED_AT,
    DELETED_AT,
    MODIFIED_AT,
    REVIT_ID,
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


def test_set_and_get_revit_id_roundtrip():
    kg = ProjectKG("p")
    level, _ = _seed(kg)
    kg.set_revit_id(level, 12345)
    assert kg.get_revit_id(level) == 12345
    # Reserved attr is materialised on the node, not validated against schema.
    assert kg.get_node(level)[REVIT_ID] == 12345


def test_set_revit_id_unknown_node_raises():
    kg = ProjectKG("p")
    with pytest.raises(KeyError):
        kg.set_revit_id("ghost_001", 1)


def test_find_by_revit_id_returns_llm_id_or_none():
    kg = ProjectKG("p")
    level, wt = _seed(kg)
    kg.set_revit_id(level, 100)
    kg.set_revit_id(wt, 200)
    assert kg.find_by_revit_id(100) == level
    assert kg.find_by_revit_id(200) == wt
    assert kg.find_by_revit_id(999) is None


def test_revit_id_survives_persistence_roundtrip(tmp_path):
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    level, _ = _seed(kg)
    kg.set_revit_id(level, 42)
    kg.persist()

    loaded = ProjectKG.load(persist)
    assert loaded.get_revit_id(level) == 42
    assert loaded.find_by_revit_id(42) == level


def test_column_and_column_type_node_types_accepted():
    """Phase 14: columns added to the KG (architectural + structural).
    Schema requires `kind` so the agent can distinguish without lookup."""
    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    ct = kg.add_node("ColumnType", {
        "family_name": "Generic Column",
        "type_name": "200x200",
        "kind": "structural",
    })
    col = kg.add_node("Column", {
        "level_ref": level,
        "type_ref": ct,
        "position": [1.0, 2.0],
        "height": 3.0,
        "kind": "structural",
    })
    assert kg.get_node(ct)["kind"] == "structural"
    assert kg.get_node(col)["position"] == [1.0, 2.0]
    assert kg.get_node(col)["height"] == 3.0


def test_model_line_and_detail_line_node_types_accepted():
    """Phase 13: lines added to the KG so the agent can address them
    (e.g. 'trace des murs sur ces lignes'). Schema accepts p1/p2/length;
    the agent uses 3D [x,y,z] for both kinds (z=0 in detail-line plane)."""
    kg = ProjectKG("p")
    kg.advance_turn()
    ml = kg.add_node("ModelLine", {
        "p1": [0.0, 0.0, 0.0],
        "p2": [3.0, 0.0, 0.0],
        "length": 3.0,
    })
    dl = kg.add_node("DetailLine", {
        "p1": [1.0, 1.0, 0.0],
        "p2": [2.0, 1.0, 0.0],
        "length": 1.0,
    })
    assert kg.get_node(ml)["_type"] == "ModelLine"
    assert kg.get_node(dl)["_type"] == "DetailLine"
    assert kg.find_by_type("ModelLine") == [ml]
    assert kg.find_by_type("DetailLine") == [dl]


def test_clear_topology_resets_graph_but_preserves_turn_and_history():
    kg = ProjectKG("p")
    _seed(kg)  # turn 1, adds 2 nodes + 3 action_log entries (1 advance + 2 creates)
    kg.advance_turn()  # turn 2
    pre_turn = kg.turn
    pre_log = list(kg.action_log)

    kg._clear_topology()  # noqa: SLF001 — exercising the internal API.

    # Topology gone.
    assert kg.find_by_type("Level") == []
    assert kg.find_by_type("WallType") == []
    # Counters reset → first new node of a type starts back at _001.
    new_level = kg.add_node("Level", {"name": "fresh", "elevation": 0.0})
    assert new_level == "level_001"
    # Timeline preserved.
    assert kg.turn == pre_turn
    # Pre-existing log entries are still there (the new add_node appended one).
    assert kg.action_log[: len(pre_log)] == pre_log


def test_clear_topology_preserve_counters_keeps_them():
    """With `preserve_counters=True`, the next allocated id continues
    past the highest pre-clear id (no renumbering)."""
    kg = ProjectKG("p")
    # Allocate 3 walls so counters['Wall'] = 3.
    kg.add_node("Level", {"name": "L1", "elevation": 0.0})
    kg.add_node("WallType", {"name": "T1", "total_thickness": 0.2})
    for _ in range(3):
        kg.add_node("Wall", {
            "type_ref": "walltype_001", "level_ref": "level_001",
            "p1": [0, 0], "p2": [1, 0], "length": 1.0, "height": 2.7,
        })
    assert kg._counters["Wall"] == 3  # noqa: SLF001

    kg._clear_topology(preserve_counters=True)  # noqa: SLF001
    # Counter intact.
    assert kg._counters["Wall"] == 3  # noqa: SLF001
    # Re-add the prerequisites then a wall — next id is wall_004, not wall_001.
    kg.add_node("Level", {"name": "L1", "elevation": 0.0})
    kg.add_node("WallType", {"name": "T1", "total_thickness": 0.2})
    new_wall = kg.add_node("Wall", {
        "type_ref": "walltype_001", "level_ref": "level_001",
        "p1": [0, 0], "p2": [1, 0], "length": 1.0, "height": 2.7,
    })
    assert new_wall == "wall_004"


def test_add_node_emit_log_false_suppresses_create_entry():
    """`_emit_log=False` skips the `create` action_log entry. Used by
    `kg_sync.full_rescan` so the log doesn't fill up with N creates at
    every rescan — only a single `rescan` event remains meaningful."""
    kg = ProjectKG("p")
    kg.advance_turn()
    pre_log_len = len(kg.action_log)

    # Silent: node added, but no log entry appended.
    nid = kg.add_node(
        "Level", {"name": "Silent", "elevation": 0.0}, _emit_log=False,
    )
    assert kg.has_node(nid)
    assert len(kg.action_log) == pre_log_len  # no `create` event.

    # Default: log appended.
    kg.add_node("Level", {"name": "Loud", "elevation": 1.0})
    assert len(kg.action_log) == pre_log_len + 1
    assert kg.action_log[-1]["action"] == "create"


def test_snapshot_revit_id_map_returns_mapping_including_deleted():
    """The snapshot must include soft-deleted nodes so an undo→rescan
    round-trip recovers the original llm_id when the element comes back."""
    kg = ProjectKG("p")
    kg.advance_turn()
    a = kg.add_node("Level", {"name": "A", "elevation": 0.0})
    b = kg.add_node("Level", {"name": "B", "elevation": 1.0})
    kg.set_revit_id(a, 100)
    kg.set_revit_id(b, 200)
    # Soft-delete b — still bound, still findable.
    kg.soft_delete(b)

    mapping = kg.snapshot_revit_id_map()
    assert mapping == {100: a, 200: b}


def test_snapshot_skips_nodes_without_revit_binding():
    """Unbound nodes (created via CLI without `set_revit_id`) don't appear
    in the snapshot — preserving their id at rescan would be meaningless
    since they have no Revit element to match against."""
    kg = ProjectKG("p")
    kg.add_node("Level", {"name": "Unbound", "elevation": 0.0})
    a = kg.add_node("Level", {"name": "Bound", "elevation": 1.0})
    kg.set_revit_id(a, 555)

    mapping = kg.snapshot_revit_id_map()
    assert mapping == {555: a}


def test_family_type_requires_category_attr():
    """`category` is required on FamilyType so the openings catalogs can
    filter without re-resolving the Revit category each time."""
    kg = ProjectKG("p")
    # Valid : with category.
    nid = kg.add_node("FamilyType", {
        "family_name": "Porte simple",
        "type_name": "0915 x 2134 mm",
        "category": "Doors",
    })
    assert kg.get_node(nid)["category"] == "Doors"

    # Missing category → ValueError mentions the missing attr.
    with pytest.raises(ValueError, match="category"):
        kg.add_node("FamilyType", {
            "family_name": "Bare",
            "type_name": "T1",
        })


def test_remove_edge_drops_typed_edge_idempotently():
    """`remove_edge` returns True on first call, False on subsequent
    calls (idempotent). Used by `openings_set_type` to re-route the
    `is_type` edge atomically."""
    kg = ProjectKG("p")
    a = kg.add_node("Level", {"name": "A", "elevation": 0.0})
    b = kg.add_node("WallType", {"name": "T", "total_thickness": 0.2})
    wall = kg.add_node("Wall", {
        "type_ref": b, "level_ref": a,
        "p1": [0, 0], "p2": [1, 0], "length": 1.0, "height": 2.7,
    })
    kg.add_edge(wall, b, "is_type")
    assert kg._g.has_edge(wall, b, key="is_type")  # noqa: SLF001

    assert kg.remove_edge(wall, b, "is_type") is True
    assert not kg._g.has_edge(wall, b, key="is_type")  # noqa: SLF001
    # Idempotent — second call returns False without raising.
    assert kg.remove_edge(wall, b, "is_type") is False


def test_door_window_schema_accepts_required_attrs():
    """Schema sanity check : Door/Window need the 5 required attrs."""
    kg = ProjectKG("p")
    door = kg.add_node("Door", {
        "type_ref": "family_type_001",
        "host_wall_ref": "wall_001",
        "position": [1.0, 0.0],
        "sill_height": 0.0,
        "head_height": 2.1,
    })
    assert kg.get_node(door)["_type"] == "Door"

    win = kg.add_node("Window", {
        "type_ref": "family_type_002",
        "host_wall_ref": "wall_001",
        "position": [3.0, 0.0],
        "sill_height": 0.9,
        "head_height": 2.4,
    })
    assert kg.get_node(win)["_type"] == "Window"

    # Door without `position` is refused.
    with pytest.raises(ValueError, match="position"):
        kg.add_node("Door", {
            "type_ref": "family_type_001",
            "host_wall_ref": "wall_001",
            "sill_height": 0.0,
            "head_height": 2.1,
        })
