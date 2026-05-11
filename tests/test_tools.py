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


@pytest.fixture
def kg_with_wall(kg_with_seed):
    """`kg_with_seed` + a single Wall (KG-only, no Revit binding)."""
    kg, level, wt = kg_with_seed
    wall = kg.add_node("Wall", {
        "type_ref": wt,
        "level_ref": level,
        "p1": [0.0, 0.0],
        "p2": [5.0, 0.0],
        "length": 5.0,
        "height": 2.7,
    })
    kg.add_edge(wall, level, "at_level")
    kg.add_edge(wall, wt, "is_type")
    return kg, level, wt, wall


def test_canonical_registry_has_expected_tier1_tools(kg_with_seed):
    registry = llm_protocol.get_registry()
    expected = {
        "catalog_list_levels",
        "catalog_list_wall_types",
        "catalog_list_walls",
        "catalog_list_lines",
        "catalog_list_columns",
        "catalog_list_column_types",
        "walls_create",
        "walls_create_many",
        "walls_create_polyline",
        "walls_create_from_lines",
        "walls_delete",
        "walls_move",
        "walls_set_height",
        "elements_translate",
        "elements_rotate",
        "elements_mirror",
        "elements_copy",
        "elements_array_linear",
        "elements_array_rotational",
        "elements_array_parametric",
        "columns_create",
        "columns_create_many",
        "columns_create_grid",
        "columns_create_grid_irregular",
        "query_find_by_name",
        "query_get_node",
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


def test_walls_create_revit_path_requires_revit_binding(kg_with_seed):
    """When the dispatcher hands a `doc`, walls_create enters the Revit
    branch and requires that Level + WallType nodes have a `_revit_id`
    binding (set by kg_sync.full_rescan at session start). Without it,
    the tool fails fast rather than calling Wall.Create with no level."""
    kg, level, wt = kg_with_seed
    # `doc` is just a sentinel — we don't need a real Revit Document because
    # the tool errors *before* the Wall.Create call (binding check first).
    result = llm_protocol.dispatch_tool_use(
        "walls_create",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "p1": [0.0, 0.0],
            "p2": [1.0, 0.0],
            "height": 2.7,
        },
        "t1",
        kg,
        doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]
    # KG unchanged.
    assert kg.count_by_type("Wall") == 0


def test_walls_create_kg_only_path_returns_revit_id_none(kg_with_seed):
    """When dispatched without a `doc` (CLI / pytest happy path), the
    payload reports `revit_id: None` to make the absence explicit."""
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
    assert payload["revit_id"] is None
    assert payload["length_m"] == 5.0


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


# ----- walls_delete / walls_move / walls_set_height ------------------------


def test_walls_delete_kg_only_soft_deletes_node(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_delete", {"llm_id": wall}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["revit_deleted"] is False
    # KG soft-delete: node still in graph but excluded from default queries.
    assert kg.find_by_type("Wall") == []
    assert kg.find_by_type("Wall", include_deleted=True) == [wall]


def test_walls_delete_rejects_already_deleted_wall(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    kg.soft_delete(wall)
    result = llm_protocol.dispatch_tool_use(
        "walls_delete", {"llm_id": wall}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "already soft-deleted" in result["content"]


def test_walls_delete_revit_path_requires_binding(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_delete", {"llm_id": wall}, "t1", kg, doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]
    # KG untouched.
    assert kg.find_by_type("Wall") == [wall]


def test_walls_move_kg_only_translates_p1_p2(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_move",
        {"llm_id": wall, "dx": 1.5, "dy": -0.5},
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["p1_m"] == [1.5, -0.5]
    assert payload["p2_m"] == [6.5, -0.5]
    node = kg.get_node(wall)
    assert node["p1"] == [1.5, -0.5]
    assert node["p2"] == [6.5, -0.5]
    # Length not recomputed by move — it shouldn't change anyway.
    assert node["length"] == 5.0


def test_walls_move_revit_path_requires_binding(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_move",
        {"llm_id": wall, "dx": 1.0, "dy": 0.0},
        "t1",
        kg,
        doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]
    # KG untouched — pre-move p1/p2 still in place.
    assert kg.get_node(wall)["p1"] == [0.0, 0.0]


def test_walls_set_height_kg_only_updates_attr(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height",
        {"llm_id": wall, "height_m": 3.5},
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["height_m"] == 3.5
    assert kg.get_node(wall)["height"] == 3.5


def test_walls_set_height_revit_path_requires_binding(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height",
        {"llm_id": wall, "height_m": 3.5},
        "t1",
        kg,
        doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]
    assert kg.get_node(wall)["height"] == 2.7  # unchanged


def test_walls_mutation_tools_reject_non_wall_llm_id(kg_with_wall):
    """Soft-protection: pointing one of the wall tools at a Level errors out."""
    kg, level, _, _ = kg_with_wall
    for tool_name, extra_input in (
        ("walls_delete", {}),
        ("walls_move", {"dx": 1.0, "dy": 0.0}),
        ("walls_set_height", {"height_m": 3.0}),
    ):
        result = llm_protocol.dispatch_tool_use(
            tool_name, {"llm_id": level, **extra_input}, "t1", kg,
        )
        assert result["is_error"] is True, "expected error for {}".format(tool_name)
        assert "not a Wall" in result["content"]


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


# ----- query_get_node ---------------------------------------------------


def test_query_get_node_returns_full_attrs(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "query_get_node", {"llm_id": wall}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["llm_id"] == wall
    assert payload["_type"] == "Wall"
    assert payload["p1"] == [0.0, 0.0]
    assert payload["p2"] == [5.0, 0.0]
    assert payload["height"] == 2.7
    assert payload["length"] == 5.0


def test_query_get_node_unknown_raises(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "query_get_node", {"llm_id": "ghost_001"}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "Unknown llm_id" in result["content"]


# ----- catalog_list_walls / catalog_list_lines --------------------------


def test_catalog_list_walls_returns_geometry(kg_with_wall):
    kg, level, wt, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_walls", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert len(payload["walls"]) == 1
    item = payload["walls"][0]
    assert item["llm_id"] == wall
    assert item["level_ref"] == level
    assert item["type_ref"] == wt
    assert item["p1"] == [0.0, 0.0]
    assert item["p2"] == [5.0, 0.0]
    assert item["length"] == 5.0
    assert item["height"] == 2.7


def test_catalog_list_lines_combines_model_and_detail(kg_with_seed):
    kg, _, _ = kg_with_seed
    ml = kg.add_node("ModelLine", {
        "p1": [0.0, 0.0, 0.0], "p2": [3.0, 0.0, 0.0], "length": 3.0,
    })
    dl = kg.add_node("DetailLine", {
        "p1": [0.0, 1.0, 0.0], "p2": [2.0, 1.0, 0.0], "length": 2.0,
    })
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_lines", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    by_id = {item["llm_id"]: item for item in payload["lines"]}
    assert by_id[ml]["kind"] == "ModelLine"
    assert by_id[ml]["p1"] == [0.0, 0.0, 0.0]
    assert by_id[ml]["length"] == 3.0
    assert by_id[dl]["kind"] == "DetailLine"
    assert by_id[dl]["length"] == 2.0


def test_catalog_list_lines_excludes_soft_deleted(kg_with_seed):
    kg, _, _ = kg_with_seed
    keep = kg.add_node("ModelLine", {
        "p1": [0.0, 0.0, 0.0], "p2": [1.0, 0.0, 0.0], "length": 1.0,
    })
    gone = kg.add_node("DetailLine", {
        "p1": [0.0, 1.0, 0.0], "p2": [2.0, 1.0, 0.0], "length": 2.0,
    })
    kg.soft_delete(gone)
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_lines", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    llm_ids = {item["llm_id"] for item in payload["lines"]}
    assert llm_ids == {keep}


# ----- generic transformations ------------------------------------------


def test_elements_translate_kg_only_shifts_wall_geometry(kg_with_wall):
    """doc=None branch: shift p1/p2 of a wall in the KG directly."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_translate",
        {"llm_ids": [wall], "vector": [2.0, 1.0]},
        "t1",
        kg,
    )
    assert result["is_error"] is False
    node = kg.get_node(wall)
    assert node["p1"] == [2.0, 1.0]
    assert node["p2"] == [7.0, 1.0]


def test_elements_translate_rejects_empty_llm_ids(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "elements_translate",
        {"llm_ids": [], "vector": [1.0, 0.0]},
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "non-empty list" in result["content"]


def test_elements_translate_rejects_soft_deleted(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    kg.soft_delete(wall)
    result = llm_protocol.dispatch_tool_use(
        "elements_translate",
        {"llm_ids": [wall], "vector": [1.0, 0.0]},
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "soft-deleted" in result["content"]


def test_elements_translate_revit_path_requires_bindings(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_translate",
        {"llm_ids": [wall], "vector": [1.0, 0.0]},
        "t1",
        kg,
        doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]


def test_elements_rotate_requires_doc_in_v0(kg_with_wall):
    """KG-only rotation is deferred — needs Revit's transform engine
    or replicating its 2D maths, which is out of V0 scope."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_rotate",
        {"llm_ids": [wall], "center": [0.0, 0.0], "angle_deg": 90},
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "requires a live Revit document" in result["content"]


def test_elements_array_linear_validates_count(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_array_linear",
        {"llm_ids": [wall], "vector": [1.0, 0.0], "count": 1},
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "count must be an integer ≥ 2" in result["content"]


def test_elements_mirror_rejects_zero_normal(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_mirror",
        {
            "llm_ids": [wall],
            "plane_origin": [0.0, 0.0],
            "plane_normal": [0.0, 0.0],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "non-zero" in result["content"]


def test_elements_copy_requires_rotation_center_when_angle_nonzero(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_copy",
        {
            "llm_ids": [wall],
            "translation": [1.0, 0.0],
            "rotation_angle_deg": 45.0,
            # rotation_center omitted
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "rotation_center required" in result["content"]


def test_elements_array_parametric_validates_count(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_array_parametric",
        {
            "src_llm_ids": [wall],
            "count": 1,  # invalid
            "per_step_translation": [5.0, 0.0],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "count must be an integer ≥ 2" in result["content"]


def test_elements_array_parametric_validates_rotation_center_mode(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_array_parametric",
        {
            "src_llm_ids": [wall],
            "count": 3,
            "per_step_rotation_deg": 9.0,
            "rotation_center_mode": "weird",
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "rotation_center_mode" in result["content"]


def test_elements_array_parametric_fixed_center_requires_explicit_point(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_array_parametric",
        {
            "src_llm_ids": [wall],
            "count": 3,
            "per_step_rotation_deg": 9.0,
            "rotation_center_mode": "fixed",
            # rotation_center omitted intentionally
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "rotation_center required" in result["content"]


def test_elements_array_parametric_negative_shortening_rejected(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_array_parametric",
        {
            "src_llm_ids": [wall],
            "count": 3,
            "per_step_shortening_m": -0.1,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "non-negative" in result["content"]


def test_elements_array_parametric_requires_doc(kg_with_wall):
    """Parametric arrays use Revit's transform engine — doc must be live."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "elements_array_parametric",
        {
            "src_llm_ids": [wall],
            "count": 3,
            "per_step_translation": [5.0, 0.0],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "requires a live Revit document" in result["content"]


def test_kg_centroid_helper_averages_wall_midpoints(kg_with_seed):
    """`_kg_centroid` averages anchor points of all source elements
    (wall midpoint, line midpoint, column position)."""
    from lib.tools.transforms import _kg_centroid
    kg, level, wt = kg_with_seed
    # Two walls forming a + cross.
    w1 = kg.add_node("Wall", {
        "type_ref": wt, "level_ref": level,
        "p1": [-2.0, 0.0], "p2": [2.0, 0.0],
        "length": 4.0, "height": 3.0,
    })
    w2 = kg.add_node("Wall", {
        "type_ref": wt, "level_ref": level,
        "p1": [0.0, -2.0], "p2": [0.0, 2.0],
        "length": 4.0, "height": 3.0,
    })
    # Midpoint of each is (0, 0) ; centroid of midpoints is (0, 0).
    assert _kg_centroid(kg, [w1, w2]) == [0.0, 0.0]


# ----- bulk_summary helper ----------------------------------------------


def test_bulk_summary_small_batch_inlines_ids():
    from lib.tools._helpers import bulk_summary
    out = bulk_summary(["wall_001", "wall_002", "wall_003"])
    assert out == {"ok": True, "count": 3, "llm_ids": ["wall_001", "wall_002", "wall_003"]}


def test_bulk_summary_large_contiguous_uses_range():
    from lib.tools._helpers import bulk_summary
    ids = ["wall_{:03d}".format(i) for i in range(1, 21)]  # 20 ids, contiguous
    out = bulk_summary(ids)
    assert out["count"] == 20
    assert out["contiguous"] is True
    assert out["first_llm_id"] == "wall_001"
    assert out["last_llm_id"] == "wall_020"
    # No `llm_ids` list — that's the win, no enumeration.
    assert "llm_ids" not in out


def test_bulk_summary_large_non_contiguous_falls_back_to_explicit_list():
    from lib.tools._helpers import bulk_summary
    ids = ["wall_001", "wall_003", "wall_005", "wall_007", "wall_009",
           "wall_011", "wall_013", "wall_015", "wall_017"]
    out = bulk_summary(ids)
    assert out["count"] == 9
    assert out.get("contiguous") is not True
    assert out["llm_ids"] == ids


def test_bulk_summary_empty_batch():
    from lib.tools._helpers import bulk_summary
    out = bulk_summary([])
    assert out == {"ok": True, "count": 0, "llm_ids": []}


# ----- walls: bulk + patterns -------------------------------------------


@pytest.fixture
def kg_with_levels_and_walltype(kg_with_seed):
    """`kg_with_seed` + a second level above N00 so story-height defaults work."""
    kg, level, wt = kg_with_seed
    kg.add_node("Level", {"name": "N01", "elevation": 2.85})
    return kg, level, wt


def test_walls_create_many_kg_only_batch(kg_with_levels_and_walltype):
    kg, level, wt = kg_with_levels_and_walltype
    items = [
        {"level_ref": level, "wall_type_ref": wt,
         "p1": [0.0, 0.0], "p2": [3.0, 0.0]},
        {"level_ref": level, "wall_type_ref": wt,
         "p1": [3.0, 0.0], "p2": [3.0, 4.0], "height": 4.0},
    ]
    result = llm_protocol.dispatch_tool_use(
        "walls_create_many", {"items": items}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    # Compact response inlines the ids for small batches (≤ 8).
    assert "llm_ids" in payload and len(payload["llm_ids"]) == 2
    # Verify attrs via the KG (the bulk response no longer enumerates them
    # per item — saves tokens; details fetched on demand if the LLM needs).
    walls = [kg.get_node(nid) for nid in payload["llm_ids"]]
    assert walls[0]["height"] == 2.85   # story height default
    assert walls[1]["height"] == 4.0    # explicit
    assert walls[0]["length"] == 3.0
    assert walls[1]["length"] == 4.0


def test_walls_create_many_rejects_invalid_item_atomic(kg_with_levels_and_walltype):
    kg, level, wt = kg_with_levels_and_walltype
    pre = kg.count_by_type("Wall")
    items = [
        {"level_ref": level, "wall_type_ref": wt,
         "p1": [0.0, 0.0], "p2": [1.0, 0.0], "height": 2.7},
        {"level_ref": level, "wall_type_ref": "ghost_wt",  # bad
         "p1": [0.0, 0.0], "p2": [1.0, 0.0], "height": 2.7},
    ]
    result = llm_protocol.dispatch_tool_use(
        "walls_create_many", {"items": items}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "items[1]" in result["content"]
    # Atomic: nothing landed.
    assert kg.count_by_type("Wall") == pre


def test_walls_create_polyline_chained(kg_with_levels_and_walltype):
    """4 vertices, closed=False → 3 walls (V→V chain). Open polyline."""
    kg, level, wt = kg_with_levels_and_walltype
    vertices = [[0.0, 0.0], [3.0, 0.0], [3.0, 4.0], [0.0, 4.0]]
    result = llm_protocol.dispatch_tool_use(
        "walls_create_polyline",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "vertices": vertices,
            "height": 2.7,
            "closed": False,
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 3
    expected_pairs = [
        ([0.0, 0.0], [3.0, 0.0]),
        ([3.0, 0.0], [3.0, 4.0]),
        ([3.0, 4.0], [0.0, 4.0]),
    ]
    actual_pairs = [
        (kg.get_node(nid)["p1"], kg.get_node(nid)["p2"])
        for nid in payload["llm_ids"]
    ]
    assert actual_pairs == expected_pairs


def test_walls_create_polyline_closed_adds_closing_wall(kg_with_levels_and_walltype):
    """closed=True → N walls instead of N-1 (closing edge added)."""
    kg, level, wt = kg_with_levels_and_walltype
    vertices = [[0.0, 0.0], [3.0, 0.0], [3.0, 4.0], [0.0, 4.0]]
    result = llm_protocol.dispatch_tool_use(
        "walls_create_polyline",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "vertices": vertices,
            "height": 2.7,
            "closed": True,
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 4
    # Closing wall: last vertex → first.
    last = kg.get_node(payload["llm_ids"][-1])
    assert last["p1"] == [0.0, 4.0]
    assert last["p2"] == [0.0, 0.0]


def test_walls_create_polyline_rejects_single_vertex(kg_with_levels_and_walltype):
    kg, level, wt = kg_with_levels_and_walltype
    result = llm_protocol.dispatch_tool_use(
        "walls_create_polyline",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "vertices": [[0.0, 0.0]],
            "height": 2.7,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "at least 2 points" in result["content"]


def test_walls_create_from_lines_drops_z_and_creates_walls(kg_with_levels_and_walltype):
    """Source lines are 3D ([x, y, z]); the wall path is 2D — z must be dropped."""
    kg, level, wt = kg_with_levels_and_walltype
    ml = kg.add_node("ModelLine", {
        "p1": [0.0, 0.0, 0.0], "p2": [5.0, 0.0, 0.0], "length": 5.0,
    })
    dl = kg.add_node("DetailLine", {
        "p1": [5.0, 0.0, 0.0], "p2": [5.0, 3.0, 0.0], "length": 3.0,
    })
    result = llm_protocol.dispatch_tool_use(
        "walls_create_from_lines",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "line_llm_ids": [ml, dl],
            "height": 2.7,
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    walls = [kg.get_node(nid) for nid in payload["llm_ids"]]
    assert walls[0]["p1"] == [0.0, 0.0]
    assert walls[0]["p2"] == [5.0, 0.0]
    assert walls[1]["p1"] == [5.0, 0.0]
    assert walls[1]["p2"] == [5.0, 3.0]


def test_walls_create_from_lines_rejects_non_line_llm_id(kg_with_levels_and_walltype):
    """Pointing at a Level or Wall instead of a Line should fail clean."""
    kg, level, wt = kg_with_levels_and_walltype
    result = llm_protocol.dispatch_tool_use(
        "walls_create_from_lines",
        {
            "level_ref": level,
            "wall_type_ref": wt,
            "line_llm_ids": [level],  # Level, not a line
            "height": 2.7,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "not a line" in result["content"]
    assert kg.count_by_type("Wall") == 0


# ----- columns: catalog + create ----------------------------------------


@pytest.fixture
def kg_with_column_type(kg_with_seed):
    """`kg_with_seed` + a structural ColumnType ready for columns_create."""
    kg, level, wt = kg_with_seed
    ct = kg.add_node("ColumnType", {
        "family_name": "Generic Column",
        "type_name": "200x200",
        "kind": "structural",
    })
    return kg, level, wt, ct


def test_catalog_list_column_types_returns_kind(kg_with_column_type):
    kg, _, _, ct = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_column_types", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert len(payload["column_types"]) == 1
    item = payload["column_types"][0]
    assert item["llm_id"] == ct
    assert item["family_name"] == "Generic Column"
    assert item["type_name"] == "200x200"
    assert item["kind"] == "structural"


def test_columns_create_kg_only_records_kind_from_type(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "position": [1.0, 2.0],
            "height": 3.5,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["revit_id"] is None
    assert payload["kind"] == "structural"
    col_id = payload["llm_id"]
    node = kg.get_node(col_id)
    assert node["position"] == [1.0, 2.0]
    assert node["height"] == 3.5
    assert node["kind"] == "structural"


def test_catalog_list_columns_returns_full_geometry(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "position": [4.0, 5.0],
            "height": 3.0,
        },
        "t1",
        kg,
    )
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_columns", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert len(payload["columns"]) == 1
    item = payload["columns"][0]
    assert item["level_ref"] == level
    assert item["type_ref"] == ct
    assert item["position"] == [4.0, 5.0]
    assert item["height"] == 3.0
    assert item["kind"] == "structural"


def test_columns_create_rejects_non_column_type_ref(kg_with_column_type):
    """Pointing column_type_ref at a WallType should fail with a clean
    error rather than producing a malformed Column."""
    kg, level, wt, _ = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": wt,  # a WallType, not a ColumnType
            "position": [0.0, 0.0],
            "height": 3.0,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "not a ColumnType" in result["content"]


def test_columns_create_revit_path_requires_binding(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "position": [0.0, 0.0],
            "height": 3.0,
        },
        "t1",
        kg,
        doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]


def test_columns_create_default_height_uses_story_height(kg_with_column_type):
    """Height omitted → defaults to (next_level.elevation - base.elevation)."""
    kg, level, _, ct = kg_with_column_type  # level N00 @ 0.0 m
    # Add a level above so a story height is computable.
    kg.add_node("Level", {"name": "N01", "elevation": 2.85})

    result = llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "position": [0.0, 0.0],
            # height omitted on purpose.
        },
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["height_m"] == 2.85
    assert payload["height_default"] is True
    assert kg.get_node(payload["llm_id"])["height"] == 2.85


def test_columns_create_explicit_height_overrides_default(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 2.85})

    result = llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "position": [0.0, 0.0],
            "height": 4.2,
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["height_m"] == 4.2
    assert payload["height_default"] is False


def test_columns_create_many_kg_only_batch(kg_with_column_type):
    """Bulk creates all items in a single KG mutation; returned llm_ids
    are distinct and ordered."""
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})  # for default height.

    items = [
        {"level_ref": level, "column_type_ref": ct, "position": [x, 0.0]}
        for x in (0.0, 5.0, 10.0)
    ]
    result = llm_protocol.dispatch_tool_use(
        "columns_create_many", {"items": items}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 3
    llm_ids = payload["llm_ids"]
    assert len(set(llm_ids)) == 3  # all distinct
    # Each landed in the KG with the default height (story height = 3.0).
    for nid, x in zip(llm_ids, (0.0, 5.0, 10.0)):
        node = kg.get_node(nid)
        assert node["position"] == [x, 0.0]
        assert node["height"] == 3.0


def test_columns_create_many_mixes_explicit_and_default_heights(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 2.85})

    items = [
        {"level_ref": level, "column_type_ref": ct, "position": [0.0, 0.0]},  # default 2.85
        {"level_ref": level, "column_type_ref": ct, "position": [1.0, 0.0], "height": 4.2},
    ]
    result = llm_protocol.dispatch_tool_use(
        "columns_create_many", {"items": items}, "t1", kg,
    )
    payload = json.loads(result["content"])
    cols = [kg.get_node(nid) for nid in payload["llm_ids"]]
    assert cols[0]["height"] == 2.85
    assert cols[1]["height"] == 4.2


def test_columns_create_many_rejects_invalid_item_with_no_partial_mutation(kg_with_column_type):
    """Validation is upfront — a bad item aborts before any KG mutation."""
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})
    pre_columns = kg.count_by_type("Column")

    items = [
        {"level_ref": level, "column_type_ref": ct, "position": [0.0, 0.0]},
        {"level_ref": "ghost_001", "column_type_ref": ct, "position": [1.0, 0.0]},  # bad
        {"level_ref": level, "column_type_ref": ct, "position": [2.0, 0.0]},
    ]
    result = llm_protocol.dispatch_tool_use(
        "columns_create_many", {"items": items}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "items[1]" in result["content"]
    assert "invalid level_ref" in result["content"]
    # KG untouched — no partial batch.
    assert kg.count_by_type("Column") == pre_columns


def test_columns_create_many_empty_list_raises(kg_with_column_type):
    kg, _, _, _ = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "columns_create_many", {"items": []}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "non-empty list" in result["content"]


def test_columns_create_grid_lays_out_count_x_times_count_y(kg_with_column_type):
    """Pattern tool builds positions locally — LLM passes 7 args, gets
    count_x*count_y columns back. Order: i varies slowest, j fastest."""
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})

    result = llm_protocol.dispatch_tool_use(
        "columns_create_grid",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "origin": [10.0, 20.0],
            "step_x": 6.0,
            "step_y": 4.0,
            "count_x": 3,
            "count_y": 2,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["count"] == 6
    # 6 ≤ 8 → llm_ids inlined in the response. Check via KG.
    positions = [tuple(kg.get_node(nid)["position"]) for nid in payload["llm_ids"]]
    assert positions == [
        (10.0, 20.0), (10.0, 24.0),
        (16.0, 20.0), (16.0, 24.0),
        (22.0, 20.0), (22.0, 24.0),
    ]
    # Default height = story height = 3.0.
    assert all(kg.get_node(nid)["height"] == 3.0 for nid in payload["llm_ids"])


def test_columns_create_grid_single_row_or_column(kg_with_column_type):
    """count_y=1 → single row of count_x columns."""
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})

    result = llm_protocol.dispatch_tool_use(
        "columns_create_grid",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "origin": [0.0, 0.0],
            "step_x": 5.0,
            "step_y": 0.0,
            "count_x": 4,
            "count_y": 1,
            "height": 3.5,
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 4
    nodes = [kg.get_node(nid) for nid in payload["llm_ids"]]
    xs = [n["position"][0] for n in nodes]
    ys = [n["position"][1] for n in nodes]
    assert xs == [0.0, 5.0, 10.0, 15.0]
    assert ys == [0.0, 0.0, 0.0, 0.0]


def test_columns_create_grid_irregular_cumulative_sum(kg_with_column_type):
    """User-provided example: 8-8-8-5-8-8-8-5-8-8-8 in X (12 cols)
    × 5-5-5-3-5-5-5-3-5-5-5-3 in Y (13 cols) = 156 columns total."""
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})

    x_spacings = [8, 8, 8, 5, 8, 8, 8, 5, 8, 8, 8]      # 11 → 12 cols
    y_spacings = [5, 5, 5, 3, 5, 5, 5, 3, 5, 5, 5, 3]   # 12 → 13 cols

    result = llm_protocol.dispatch_tool_use(
        "columns_create_grid_irregular",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "origin": [0.0, 0.0],
            "x_spacings": x_spacings,
            "y_spacings": y_spacings,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["count"] == 12 * 13  # 156
    # Large batch (> 8) → compact response with contiguous range. Verify
    # the geometry via the KG itself (the response no longer enumerates
    # each position to save ~10 KB of input tokens on the next turn).
    assert payload.get("contiguous") is True
    positions = [tuple(kg.get_node(nid)["position"])
                 for nid in kg.find_by_type("Column")]
    assert (24.0, 18.0) in positions     # i=3, j=4 ⇒ sums (24, 18)
    assert (82.0, 54.0) in positions     # corner: sum(x)=82, sum(y)=54
    assert (0.0, 0.0) in positions       # origin


def test_columns_create_grid_irregular_empty_spacings_yields_single_column(kg_with_column_type):
    """Empty lists ⇒ a 1×1 grid (just the origin)."""
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})

    result = llm_protocol.dispatch_tool_use(
        "columns_create_grid_irregular",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "origin": [5.0, 6.0],
            "x_spacings": [],
            "y_spacings": [],
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 1
    only = kg.get_node(payload["llm_ids"][0])
    assert only["position"] == [5.0, 6.0]


def test_columns_create_grid_irregular_rejects_non_numeric_spacing(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "columns_create_grid_irregular",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "origin": [0.0, 0.0],
            "x_spacings": [5, "boom", 5],
            "y_spacings": [3.0],
            "height": 3.0,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "x_spacings[1]" in result["content"]


def test_columns_create_grid_rejects_zero_count(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    result = llm_protocol.dispatch_tool_use(
        "columns_create_grid",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "origin": [0.0, 0.0],
            "step_x": 5.0,
            "step_y": 5.0,
            "count_x": 0,  # invalid
            "count_y": 3,
            "height": 3.0,
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "count_x must be a positive integer" in result["content"]


def test_columns_create_many_revit_path_requires_bindings(kg_with_column_type):
    kg, level, _, ct = kg_with_column_type
    kg.add_node("Level", {"name": "N01", "elevation": 3.0})

    items = [
        {"level_ref": level, "column_type_ref": ct, "position": [0.0, 0.0]},
    ]
    result = llm_protocol.dispatch_tool_use(
        "columns_create_many", {"items": items}, "t1", kg, doc=object(),
    )
    assert result["is_error"] is True
    assert "no Revit binding" in result["content"]
    assert kg.count_by_type("Column") == 0


def test_columns_create_top_level_errors_when_height_omitted(kg_with_column_type):
    """Si pas de niveau au-dessus, le défaut n'est pas calculable → erreur."""
    kg, level, _, ct = kg_with_column_type  # single level N00.

    result = llm_protocol.dispatch_tool_use(
        "columns_create",
        {
            "level_ref": level,
            "column_type_ref": ct,
            "position": [0.0, 0.0],
            # height omitted on purpose.
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "No level above" in result["content"]
    # KG untouched.
    assert kg.count_by_type("Column") == 0
