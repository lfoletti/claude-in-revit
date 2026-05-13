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
        "openings_create_door",
        "openings_create_window",
        "openings_create_many",
        "openings_set_sill_height",
        "openings_set_head_height",
        "openings_set_type",
        "openings_create_type_variant",
        "openings_delete",
        "catalog_list_door_types",
        "catalog_list_window_types",
        "catalog_list_doors",
        "catalog_list_windows",
        "catalog_list_rooms",
        "rooms_create",
        "rooms_set_name",
        "rooms_recompute_boundaries",
        "rooms_get_area",
        "rooms_delete",
        "rooms_set_name_many",
        "levels_create",
        "levels_create_many",
        "levels_create_floor_plan",
        "levels_reconcile_with_dxf",
        "levels_set_elevation",
        "levels_set_name",
        "walls_set_height_many",
        "walls_move_many",
        "walls_delete_many",
        "openings_delete_many",
        "rooms_delete_many",
        "openings_set_sill_height_many",
        "openings_set_head_height_many",
        "openings_set_type_many",
        "openings_purge_unused_variants",
        "bulk_resolve_filter",
        "bulk_apply_to_filter",
        "dwg_inspect",
        "dwg_classify",
        "dwg_import_walls",
        "dwg_inspect_sections",
        "dwg_find_section_markers",
        "dwg_verify_section_scale",
        "dwg_identify_source",
        "dxf_context_get",
        "dxf_context_register_inspection",
        "dxf_context_register_section_line",
        "floors_create",
        "floors_create_many",
        "floors_delete",
        "floors_delete_many",
        "catalog_list_floors",
        "catalog_list_floor_types",
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


def test_walls_set_height_response_includes_drift_fields(kg_with_wall):
    """Discipline read-back : la réponse de walls_set_height expose
    `requested_height_m` + `drift` + `drift_note` même en KG-only,
    pour que le LLM ait toujours le même shape."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height",
        {"llm_id": wall, "height_m": 3.0},
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["height_m"] == 3.0
    assert payload["requested_height_m"] == 3.0
    assert payload["drift"] is False
    assert payload["drift_note"] is None


def test_walls_move_response_includes_drift_fields(kg_with_wall):
    """Pareil pour walls_move : p1_m / p2_m sont les valeurs effectives,
    requested_p1_m / requested_p2_m exposent la trajectoire demandée."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_move",
        {"llm_id": wall, "dx": 2.0, "dy": 0.0},
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["p1_m"] == [2.0, 0.0]
    assert payload["p2_m"] == [7.0, 0.0]
    assert payload["requested_p1_m"] == [2.0, 0.0]
    assert payload["requested_p2_m"] == [7.0, 0.0]
    assert payload["drift"] is False


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


# ----- openings : create_door / create_window / set_*_height / delete ---


@pytest.fixture
def kg_with_opening_setup(kg_with_wall):
    """`kg_with_wall` enrichi de deux FamilyType (un door, un window)
    et d'un second mur — assez de matière pour exercer create_door,
    create_window, create_many, et les mutations."""
    kg, level, wt, wall = kg_with_wall
    door_type = kg.add_node("FamilyType", {
        "family_name": "Porte simple",
        "type_name": "0915 x 2134 mm",
        "category": "Doors",
    })
    window_type = kg.add_node("FamilyType", {
        "family_name": "Fenêtre fixe",
        "type_name": "1200 x 1500 mm",
        "category": "Windows",
    })
    wall2 = kg.add_node("Wall", {
        "type_ref": wt,
        "level_ref": level,
        "p1": [0.0, 5.0],
        "p2": [5.0, 5.0],
        "length": 5.0,
        "height": 2.7,
    })
    kg.add_edge(wall2, level, "at_level")
    kg.add_edge(wall2, wt, "is_type")
    return kg, level, wt, wall, wall2, door_type, window_type


def test_openings_create_door_kg_only_records_node_and_edges(kg_with_opening_setup):
    kg, level, _, wall, _, door_type, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": wall,
            "family_type_ref": door_type,
            "position": [2.5, 0.0],
            "sill_height": 0.0,
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    nid = payload["llm_id"]
    assert payload["revit_id"] is None
    node = kg.get_node(nid)
    assert node["_type"] == "Door"
    assert node["host_wall_ref"] == wall
    assert node["type_ref"] == door_type
    assert node["position"] == [2.5, 0.0]
    # Edges : wall hosts door, door is_type door_type, door at_level level.
    edge_types = {k for _, _, k in kg._g.edges(nid, keys=True)}  # noqa: SLF001
    edge_types_in = {k for _, _, k in kg._g.in_edges(nid, keys=True)}  # noqa: SLF001
    assert "is_type" in edge_types
    assert "at_level" in edge_types
    assert "hosts" in edge_types_in


def test_openings_create_window_kg_only_uses_window_defaults(kg_with_opening_setup):
    """Sans `sill_height` fourni, le défaut KG-only pour une fenêtre est
    0.9 m (architectes standard d'allège française/suisse)."""
    kg, _, _, _, wall2, _, window_type = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {
            "host_wall_ref": wall2,
            "family_type_ref": window_type,
            "position": [2.5, 5.0],
        },
        "t1",
        kg,
    )
    payload = json.loads(result["content"])
    nid = payload["llm_id"]
    node = kg.get_node(nid)
    assert node["_type"] == "Window"
    assert node["sill_height"] == 0.9
    assert payload["sill_height_m"] == 0.9


def test_openings_create_door_rejects_window_type(kg_with_opening_setup):
    """`openings_create_door` doit refuser un family_type_ref de catégorie
    "Windows" — c'est ce qui empêche le LLM de poser une fenêtre via le
    tool 'porte' par confusion."""
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": wall,
            "family_type_ref": window_type,
            "position": [2.5, 0.0],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "category=Windows" in result["content"]
    assert "expected Doors" in result["content"]
    assert kg.count_by_type("Door") == 0


def test_openings_create_door_rejects_unknown_host_wall(kg_with_opening_setup):
    kg, _, _, _, _, door_type, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": "wall_999",
            "family_type_ref": door_type,
            "position": [0.0, 0.0],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "wall_999" in result["content"]


def test_openings_create_door_rejects_non_wall_host(kg_with_opening_setup):
    """Si on passe un level llm_id comme host_wall_ref, refus net."""
    kg, level, _, _, _, door_type, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": level,
            "family_type_ref": door_type,
            "position": [0.0, 0.0],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "not a Wall" in result["content"]


def test_openings_create_many_kg_only_mixed_door_window(kg_with_opening_setup):
    kg, _, _, wall, wall2, door_type, window_type = kg_with_opening_setup
    items = [
        {"kind": "door", "host_wall_ref": wall, "family_type_ref": door_type, "position": [1.0, 0.0]},
        {"kind": "window", "host_wall_ref": wall2, "family_type_ref": window_type, "position": [1.0, 5.0]},
        {"kind": "window", "host_wall_ref": wall2, "family_type_ref": window_type, "position": [3.0, 5.0]},
    ]
    result = llm_protocol.dispatch_tool_use(
        "openings_create_many", {"items": items}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 3
    # 1 door + 2 windows.
    assert kg.count_by_type("Door") == 1
    assert kg.count_by_type("Window") == 2


def test_openings_create_many_validates_upfront_no_partial_mutation(
    kg_with_opening_setup,
):
    """Si l'item #2 est invalide (mauvaise catégorie), AUCUN item ne doit
    être créé — atomicité par la validation upfront."""
    kg, _, _, wall, _, door_type, window_type = kg_with_opening_setup
    items = [
        {"kind": "door", "host_wall_ref": wall, "family_type_ref": door_type, "position": [1.0, 0.0]},
        # kind=door mais family_type est une window → erreur.
        {"kind": "door", "host_wall_ref": wall, "family_type_ref": window_type, "position": [2.0, 0.0]},
    ]
    result = llm_protocol.dispatch_tool_use(
        "openings_create_many", {"items": items}, "t1", kg,
    )
    assert result["is_error"] is True
    # Le 1er item n'est pas non plus créé (rollback KG sur exception via dispatcher).
    assert kg.count_by_type("Door") == 0


def test_openings_set_sill_height_kg_only_updates_attr(kg_with_opening_setup):
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {
            "host_wall_ref": wall,
            "family_type_ref": window_type,
            "position": [2.5, 0.0],
            "sill_height": 0.9,
        },
        "t1",
        kg,
    )
    nid = json.loads(create["content"])["llm_id"]

    set_result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height",
        {"llm_id": nid, "sill_height_m": 1.2},
        "t2",
        kg,
    )
    payload = json.loads(set_result["content"])
    assert payload["ok"] is True
    assert payload["sill_height_m"] == 1.2
    assert payload["revit_modified"] is False
    assert kg.get_node(nid)["sill_height"] == 1.2


def test_openings_set_sill_height_kg_only_reports_no_drift(kg_with_opening_setup):
    """En KG-only (pas de Revit pour recalculer), le drift est toujours
    False et la réponse expose `requested_sill_height_m` + le
    head_height courant (pour que l'utilisateur voie le couple
    complet)."""
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {
            "host_wall_ref": wall, "family_type_ref": window_type,
            "position": [2.5, 0.0], "sill_height": 0.9,
        },
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]

    set_result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height",
        {"llm_id": nid, "sill_height_m": 1.2},
        "t2", kg,
    )
    payload = json.loads(set_result["content"])
    assert payload["drift"] is False
    assert payload["drift_note"] is None
    assert payload["requested_sill_height_m"] == 1.2
    assert payload["sill_height_m"] == 1.2
    # Head reporté tel qu'il était dans le KG (KG-only ne recalcule pas).
    assert "head_height_m" in payload


def test_openings_set_head_height_kg_only_updates_attr(kg_with_opening_setup):
    kg, _, _, wall, _, door_type, _ = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": wall, "family_type_ref": door_type,
            "position": [2.5, 0.0],
        },
        "t1",
        kg,
    )
    nid = json.loads(create["content"])["llm_id"]

    set_result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": nid, "head_height_m": 2.10},
        "t2",
        kg,
    )
    payload = json.loads(set_result["content"])
    assert payload["head_height_m"] == 2.10
    assert kg.get_node(nid)["head_height"] == 2.10


def test_openings_set_head_height_kg_only_reports_no_drift(kg_with_opening_setup):
    """Symétrique de sill : KG-only, drift=False, requested_head_height_m
    exposé."""
    kg, _, _, wall, _, door_type, _ = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": wall, "family_type_ref": door_type,
            "position": [1.0, 0.0],
        },
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]

    set_result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": nid, "head_height_m": 2.10},
        "t2", kg,
    )
    payload = json.loads(set_result["content"])
    assert payload["drift"] is False
    assert payload["drift_note"] is None
    assert payload["requested_head_height_m"] == 2.10
    assert payload["head_height_m"] == 2.10
    assert "sill_height_m" in payload


def test_drift_note_built_when_committed_diverges():
    """Test direct de l'helper `_drift_note` : si la valeur committée
    diffère de la demandée au-delà du seuil, on a une note explicative
    qui pointe vers openings_set_type / openings_create_type_variant."""
    from lib.tools import openings

    # Demandé sill=0.80, Revit a commit sill=1.45 → drift attendu.
    note = openings._drift_note(  # noqa: SLF001
        "sill_height",
        requested_value=0.80,
        actual_sill=1.45,
        actual_head=2.20,
    )
    assert note is not None
    assert "1.450" in note or "1.45" in note
    assert "0.800" in note or "0.80" in note
    # Le LLM doit y voir le contournement à proposer.
    assert "openings_set_type" in note
    assert "openings_create_type_variant" in note


def test_drift_note_none_when_committed_matches():
    """Pas de drift = pas de note. Tolérance demi-mm pour absorber le
    round-trip pieds↔mètres."""
    from lib.tools import openings

    note = openings._drift_note(  # noqa: SLF001
        "sill_height",
        requested_value=0.80,
        actual_sill=0.80003,  # < epsilon
        actual_head=2.20,
    )
    assert note is None


def test_drift_note_none_when_actual_unreadable():
    """Si on n'a pas pu relire le paramètre depuis Revit (None), pas
    de drift signalé — on ne sait juste pas."""
    from lib.tools import openings

    note = openings._drift_note(  # noqa: SLF001
        "head_height",
        requested_value=2.20,
        actual_sill=0.80,
        actual_head=None,
    )
    assert note is None


def test_openings_delete_kg_only_soft_deletes(kg_with_opening_setup):
    kg, _, _, wall, _, door_type, _ = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": wall, "family_type_ref": door_type,
            "position": [1.0, 0.0],
        },
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]
    assert kg.count_by_type("Door") == 1

    delete = llm_protocol.dispatch_tool_use(
        "openings_delete", {"llm_id": nid}, "t2", kg,
    )
    payload = json.loads(delete["content"])
    assert payload["ok"] is True
    assert payload["revit_deleted"] is False
    # Filtered out of default queries.
    assert kg.count_by_type("Door") == 0
    # Still present with include_deleted=True.
    assert nid in kg.find_by_type("Door", include_deleted=True)


def test_openings_delete_refuses_already_deleted(kg_with_opening_setup):
    kg, _, _, wall, _, door_type, _ = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {
            "host_wall_ref": wall, "family_type_ref": door_type,
            "position": [1.0, 0.0],
        },
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]
    kg.soft_delete(nid)

    second = llm_protocol.dispatch_tool_use(
        "openings_delete", {"llm_id": nid}, "t2", kg,
    )
    assert second["is_error"] is True
    assert "already soft-deleted" in second["content"]


def test_openings_set_sill_height_refuses_non_opening(kg_with_opening_setup):
    """Passer un llm_id de Wall doit échouer net (pas un Door/Window)."""
    kg, _, _, wall, _, _, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height",
        {"llm_id": wall, "sill_height_m": 1.0},
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "not a Door or Window" in result["content"]


# ----- catalogues openings ---------------------------------------------


def test_catalog_list_door_types_filters_by_category(kg_with_opening_setup):
    kg, _, _, _, _, door_type, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_door_types", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    ids = [d["llm_id"] for d in payload["door_types"]]
    assert door_type in ids
    # Pas de window_type qui aurait fuité.
    family_names = [d["family_name"] for d in payload["door_types"]]
    assert "Fenêtre fixe" not in family_names


def test_catalog_list_window_types_filters_by_category(kg_with_opening_setup):
    kg, _, _, _, _, _, window_type = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_window_types", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    ids = [w["llm_id"] for w in payload["window_types"]]
    assert window_type in ids
    family_names = [w["family_name"] for w in payload["window_types"]]
    assert "Porte simple" not in family_names


def test_openings_set_type_kg_only_swaps_type_ref(kg_with_opening_setup):
    """KG-only swap d'un Door vers un autre type Doors : modify_node +
    edge `is_type` rerouted."""
    kg, _, _, wall, _, door_type, _ = kg_with_opening_setup
    # Second door type to swap to.
    door_type_b = kg.add_node("FamilyType", {
        "family_name": "Porte simple",
        "type_name": "0815 x 2050 mm",
        "category": "Doors",
        "dimensions": {"height_m": 2.05, "width_m": 0.815},
    })
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {"host_wall_ref": wall, "family_type_ref": door_type, "position": [1.0, 0.0]},
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]
    assert kg.get_node(nid)["type_ref"] == door_type

    swap = llm_protocol.dispatch_tool_use(
        "openings_set_type",
        {"llm_id": nid, "new_family_type_ref": door_type_b},
        "t2", kg,
    )
    payload = json.loads(swap["content"])
    assert payload["ok"] is True
    assert payload["old_type_ref"] == door_type
    assert payload["new_type_ref"] == door_type_b
    # Attr updated.
    assert kg.get_node(nid)["type_ref"] == door_type_b
    # Old edge gone, new edge present.
    assert not kg._g.has_edge(nid, door_type, key="is_type")  # noqa: SLF001
    assert kg._g.has_edge(nid, door_type_b, key="is_type")  # noqa: SLF001


def test_openings_set_type_refuses_cross_category(kg_with_opening_setup):
    """Une porte ne peut pas adopter un type fenêtre."""
    kg, _, _, wall, _, door_type, window_type = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {"host_wall_ref": wall, "family_type_ref": door_type, "position": [1.0, 0.0]},
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]

    swap = llm_protocol.dispatch_tool_use(
        "openings_set_type",
        {"llm_id": nid, "new_family_type_ref": window_type},
        "t2", kg,
    )
    assert swap["is_error"] is True
    assert "category=Windows" in swap["content"]
    assert "expected Doors" in swap["content"]
    # Type unchanged.
    assert kg.get_node(nid)["type_ref"] == door_type


def test_openings_set_type_refuses_non_family_type(kg_with_opening_setup):
    """Refus net si new_family_type_ref n'est pas un FamilyType."""
    kg, level, _, wall, _, door_type, _ = kg_with_opening_setup
    create = llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {"host_wall_ref": wall, "family_type_ref": door_type, "position": [1.0, 0.0]},
        "t1", kg,
    )
    nid = json.loads(create["content"])["llm_id"]

    swap = llm_protocol.dispatch_tool_use(
        "openings_set_type",
        {"llm_id": nid, "new_family_type_ref": level},
        "t2", kg,
    )
    assert swap["is_error"] is True
    assert "not a FamilyType" in swap["content"]


def test_openings_create_type_variant_kg_only_adds_node(kg_with_opening_setup):
    """KG-only path : ajoute un FamilyType avec dimensions, mirror du
    family_name source, type_name = new_name."""
    kg, _, _, _, _, _, window_type = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_type_variant",
        {
            "source_type_ref": window_type,
            "new_name": "Fenêtre 1200 x 1200 mm",
            "opening_height_m": 1.20,
            "opening_width_m": 1.20,
        },
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    new_nid = payload["llm_id"]
    assert payload["revit_id"] is None
    assert payload["family_name"] == "Fenêtre fixe"
    assert payload["type_name"] == "Fenêtre 1200 x 1200 mm"
    assert payload["category"] == "Windows"
    assert payload["dimensions"]["height_m"] == 1.20
    assert payload["dimensions"]["width_m"] == 1.20

    # KG node mirrors the payload.
    node = kg.get_node(new_nid)
    assert node["_type"] == "FamilyType"
    assert node["category"] == "Windows"
    assert node["dimensions"] == {"height_m": 1.20, "width_m": 1.20}


def test_openings_create_type_variant_height_only_omits_width(
    kg_with_opening_setup,
):
    """Sans opening_width_m, la dimensions garde uniquement height_m."""
    kg, _, _, _, _, _, window_type = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_type_variant",
        {
            "source_type_ref": window_type,
            "new_name": "Fenêtre haute",
            "opening_height_m": 2.40,
        },
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["dimensions"] == {"height_m": 2.40}
    assert "width_m" not in payload["dimensions"]


def test_openings_create_type_variant_refuses_non_family_source(
    kg_with_opening_setup,
):
    kg, level, _, _, _, _, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_type_variant",
        {
            "source_type_ref": level,
            "new_name": "fail",
            "opening_height_m": 1.5,
        },
        "t1", kg,
    )
    assert result["is_error"] is True
    assert "not a FamilyType" in result["content"]


def test_openings_create_type_variant_refuses_empty_name(
    kg_with_opening_setup,
):
    kg, _, _, _, _, _, window_type = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_create_type_variant",
        {
            "source_type_ref": window_type,
            "new_name": "  ",
            "opening_height_m": 1.5,
        },
        "t1", kg,
    )
    assert result["is_error"] is True
    assert "non-empty" in result["content"]


def test_catalog_list_door_types_surfaces_dimensions(kg_with_opening_setup):
    """Si un FamilyType a un attribut dimensions, le catalog le remonte."""
    kg, _, _, _, _, _, _ = kg_with_opening_setup
    kg.add_node("FamilyType", {
        "family_name": "Porte coupe-feu",
        "type_name": "EI60 0900 x 2100",
        "category": "Doors",
        "dimensions": {"height_m": 2.10, "width_m": 0.90},
    })
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_door_types", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    types_with_dims = [d for d in payload["door_types"] if "dimensions" in d]
    assert any(
        d["dimensions"] == {"height_m": 2.10, "width_m": 0.90}
        for d in types_with_dims
    )


def test_catalog_list_doors_and_windows_return_geometry(kg_with_opening_setup):
    kg, _, _, wall, wall2, door_type, window_type = kg_with_opening_setup
    # Crée 1 porte + 2 fenêtres.
    llm_protocol.dispatch_tool_use(
        "openings_create_door",
        {"host_wall_ref": wall, "family_type_ref": door_type, "position": [1.0, 0.0]},
        "t1", kg,
    )
    llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall2, "family_type_ref": window_type, "position": [1.0, 5.0]},
        "t2", kg,
    )
    llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall2, "family_type_ref": window_type, "position": [3.0, 5.0]},
        "t3", kg,
    )

    doors_result = llm_protocol.dispatch_tool_use(
        "catalog_list_doors", {}, "t4", kg,
    )
    windows_result = llm_protocol.dispatch_tool_use(
        "catalog_list_windows", {}, "t5", kg,
    )
    assert len(json.loads(doors_result["content"])["doors"]) == 1
    assert len(json.loads(windows_result["content"])["windows"]) == 2


# ----- Rooms + Levels (V0 Sem.2-3) ------------------------------------------


def test_rooms_create_kg_only_adds_room_node_and_edge(kg_with_seed):
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [2.5, 1.5], "name": "Salon"},
        "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["name"] == "Salon"
    assert payload["area_m2"] == 0.0
    assert payload["revit_id"] is None
    assert payload["note"] is None  # KG-only path : no Revit area check.

    room_id = payload["llm_id"]
    attrs = kg.get_node(room_id)
    assert attrs["_type"] == "Room"
    assert attrs["name"] == "Salon"
    assert attrs["level_ref"] == level
    assert attrs["boundary_walls"] == []

    edges = list(kg._g.out_edges(room_id, keys=True))  # noqa: SLF001
    assert [k for _, _, k in edges] == ["at_level"]


def test_rooms_create_refuses_unknown_level(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": "level_999", "point": [0.0, 0.0]},
        "t1", kg,
    )
    assert result["is_error"] is True
    assert "Unknown level_ref" in result["content"]


def test_rooms_create_refuses_non_level_ref(kg_with_seed):
    kg, _, wt = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": wt, "point": [0.0, 0.0]},
        "t1", kg,
    )
    assert result["is_error"] is True
    assert "not a Level" in result["content"]


def test_rooms_create_defaults_name_to_room(kg_with_seed):
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0]},
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["name"] == "Room"


def test_rooms_set_name_kg_only_updates_attr_no_drift(kg_with_seed):
    kg, level, _ = kg_with_seed
    room_id = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "A"},
        "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_set_name",
        {"llm_id": room_id, "name": "Cuisine"},
        "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["name"] == "Cuisine"
    assert payload["requested_name"] == "Cuisine"
    assert payload["drift"] is False
    assert payload["drift_note"] is None
    assert kg.get_node(room_id)["name"] == "Cuisine"


def test_rooms_set_name_refuses_empty(kg_with_seed):
    kg, level, _ = kg_with_seed
    room_id = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0]},
        "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_set_name",
        {"llm_id": room_id, "name": "   "},
        "t2", kg,
    )
    assert result["is_error"] is True
    assert "non-empty" in result["content"]


def test_rooms_get_area_returns_stale_kg_only(kg_with_seed):
    kg, level, _ = kg_with_seed
    room_id = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "Bureau"},
        "t1", kg,
    )["content"])["llm_id"]
    # Force une aire connue côté KG.
    kg.modify_node(room_id, {"area": 12.34})
    result = llm_protocol.dispatch_tool_use(
        "rooms_get_area", {"llm_id": room_id}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["area_m2"] == 12.34
    assert payload["name"] == "Bureau"
    assert payload["level_ref"] == level
    assert payload["stale"] is True


def test_rooms_recompute_boundaries_kg_only_lists_all_rooms(kg_with_seed):
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "A"}, "t1", kg,
    )["content"])["llm_id"]
    r2 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 5.0], "name": "B"}, "t2", kg,
    )["content"])["llm_id"]
    kg.modify_node(r1, {"area": 10.0})
    kg.modify_node(r2, {"area": 20.0})
    result = llm_protocol.dispatch_tool_use(
        "rooms_recompute_boundaries", {}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["rooms_refreshed"] == 2
    assert payload["revit_regenerated"] is False
    areas = {e["llm_id"]: e["area_m2"] for e in payload["refreshed"]}
    assert areas[r1] == 10.0
    assert areas[r2] == 20.0


def test_rooms_recompute_boundaries_single_room(kg_with_seed):
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    _ = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 5.0]}, "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_recompute_boundaries", {"llm_id": r1}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["rooms_refreshed"] == 1
    assert payload["refreshed"][0]["llm_id"] == r1


def test_rooms_delete_soft_deletes(kg_with_seed):
    kg, level, _ = kg_with_seed
    room_id = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_delete", {"llm_id": room_id}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["revit_deleted"] is False
    assert kg.get_node(room_id)["deleted_at_turn"] is not None


def test_catalog_list_rooms_returns_live_rooms(kg_with_seed):
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "Living"}, "t1", kg,
    )["content"])["llm_id"]
    r2 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 0.0], "name": "Kitchen"}, "t2", kg,
    )["content"])["llm_id"]
    # Soft-delete r2 — il ne doit PAS apparaître dans le catalog (find_by_type
    # filtre les soft-deleted par défaut).
    llm_protocol.dispatch_tool_use(
        "rooms_delete", {"llm_id": r2}, "t3", kg,
    )
    result = llm_protocol.dispatch_tool_use(
        "catalog_list_rooms", {}, "t4", kg,
    )
    payload = json.loads(result["content"])
    ids = [r["llm_id"] for r in payload["rooms"]]
    assert r1 in ids
    assert r2 not in ids


def test_levels_create_kg_only_adds_node(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create",
        {"name": "N02", "elevation_m": 6.0}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["name"] == "N02"
    assert payload["elevation_m"] == 6.0
    assert payload["revit_id"] is None
    # KG-only path : pas de doc → pas de FloorPlan créé, flag à False.
    assert payload["floor_plan_created"] is False
    assert payload["floor_plan_revit_id"] is None

    level_id = payload["llm_id"]
    attrs = kg.get_node(level_id)
    assert attrs["_type"] == "Level"


def test_levels_create_floor_plan_kg_only_no_op(kg_with_seed):
    """En KG-only (doc=None), levels_create_floor_plan no-op avec note explicite."""
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create_floor_plan", {"llm_id": level}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["floor_plan_revit_id"] is None
    assert "doc is None" in (payload.get("note") or "")


def test_levels_create_floor_plan_refuses_unknown(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create_floor_plan", {"llm_id": "level_999"}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "Unknown llm_id" in result["content"]


def test_levels_create_refuses_duplicate_name(kg_with_seed):
    """N00 est déjà seedé. Recréer un Level homonyme doit échouer."""
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create",
        {"name": "N00", "elevation_m": 5.0}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "already exists" in result["content"]


def test_levels_create_refuses_empty_name(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create",
        {"name": "   ", "elevation_m": 5.0}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "non-empty" in result["content"]


def test_levels_create_many_creates_three_levels(kg_with_seed):
    """Use case import projet : 3 niveaux extraits depuis coupes en 1 appel."""
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create_many",
        {"items": [
            {"name": "RDC", "elevation_m": 0.0},
            {"name": "Étage 1", "elevation_m": 3.0},
            {"name": "Étage 2", "elevation_m": 6.0},
        ]},
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 3
    assert len(payload["llm_ids"]) == 3
    # KG-only path → no floor plans, note documente.
    assert payload["floor_plans_created"] == 0
    assert "no Revit views" in payload["floor_plan_note"]
    # Vérifier que chaque level est bien typé Level + a la bonne élévation.
    elevs = sorted(kg.get_node(nid)["elevation"] for nid in payload["llm_ids"])
    assert elevs == [0.0, 3.0, 6.0]


def test_levels_create_many_rolls_back_on_duplicate_name(kg_with_seed):
    """Si un item entre en collision avec un Level pré-existant, aucun
    niveau du batch ne doit être commit."""
    kg, _, _ = kg_with_seed
    # kg_with_seed pose déjà un Level "N00" → collision sur le 2e item.
    initial = kg.count_by_type("Level")
    result = llm_protocol.dispatch_tool_use(
        "levels_create_many",
        {"items": [
            {"name": "RDC", "elevation_m": 0.0},
            {"name": "N00", "elevation_m": 3.0},  # collision
        ]},
        "t1", kg,
    )
    assert result["is_error"] is True
    assert kg.count_by_type("Level") == initial


def test_levels_create_many_rejects_intra_batch_duplicate(kg_with_seed):
    """Deux items avec le même nom dans le batch → erreur explicite."""
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create_many",
        {"items": [
            {"name": "RDC", "elevation_m": 0.0},
            {"name": "RDC", "elevation_m": 3.0},
        ]},
        "t1", kg,
    )
    assert result["is_error"] is True
    assert "duplicate" in result["content"].lower()


def test_levels_create_many_rejects_empty_items(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_create_many", {"items": []}, "t1", kg,
    )
    assert result["is_error"] is True


def test_levels_set_elevation_kg_only_no_drift(kg_with_seed):
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_set_elevation",
        {"llm_id": level, "elevation_m": 3.5}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["elevation_m"] == 3.5
    assert payload["requested_elevation_m"] == 3.5
    assert payload["drift"] is False
    assert payload["drift_note"] is None
    assert kg.get_node(level)["elevation"] == 3.5


def test_levels_set_name_kg_only_no_drift(kg_with_seed):
    kg, level, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "levels_set_name",
        {"llm_id": level, "name": "RDC"}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["name"] == "RDC"
    assert payload["drift"] is False
    assert kg.get_node(level)["name"] == "RDC"


def test_walls_delete_many_kg_only(kg_with_levels_and_walltype):
    """3 murs créés puis supprimés en 1 appel."""
    kg, level, wt = kg_with_levels_and_walltype
    ids = []
    for k in range(3):
        nid = kg.add_node("Wall", {
            "type_ref": wt, "level_ref": level,
            "p1": [0.0, float(k)], "p2": [1.0, float(k)],
            "length": 1.0, "height": 2.7,
        })
        ids.append(nid)
    result = llm_protocol.dispatch_tool_use(
        "walls_delete_many",
        {"items": [{"llm_id": nid} for nid in ids]}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 3
    assert payload["deleted_kg_only"] == 3
    assert payload["revit_modified"] is False
    for nid in ids:
        assert kg.get_node(nid)["deleted_at_turn"] is not None


def test_walls_delete_many_accepts_bare_string_ids(kg_with_wall):
    """Items peut être [string, …] OU [{"llm_id": string}, …]."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_delete_many", {"items": [wall]}, "t1", kg,
    )
    assert result["is_error"] is False
    assert kg.get_node(wall)["deleted_at_turn"] is not None


def test_walls_delete_many_refuses_non_wall(kg_with_wall):
    kg, level, _, _ = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_delete_many", {"items": [{"llm_id": level}]}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "not a Wall" in result["content"]


def test_openings_delete_many_kg_only(kg_with_opening_setup):
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    w2 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [3.0, 0.0]}, "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "openings_delete_many", {"items": [w1, w2]}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert kg.get_node(w1)["deleted_at_turn"] is not None
    assert kg.get_node(w2)["deleted_at_turn"] is not None


def test_rooms_delete_many_kg_only(kg_with_seed):
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    r2 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 0.0]}, "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_delete_many", {"items": [r1, r2]}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert kg.get_node(r1)["deleted_at_turn"] is not None
    assert kg.get_node(r2)["deleted_at_turn"] is not None


def test_levels_set_name_refuses_duplicate(kg_with_seed):
    """N00 existe → créer un autre Level puis tenter de le renommer N00."""
    kg, _, _ = kg_with_seed
    new_id = json.loads(llm_protocol.dispatch_tool_use(
        "levels_create",
        {"name": "N02", "elevation_m": 6.0}, "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "levels_set_name",
        {"llm_id": new_id, "name": "N00"}, "t2", kg,
    )
    assert result["is_error"] is True
    assert "already exists" in result["content"]


# ----- Bulk setters (V0 session 2026-05-12 b — dette setters_many) -----------


def test_bulk_setter_summary_no_drift_shape():
    from lib.tools._helpers import bulk_setter_summary
    out = bulk_setter_summary([], count=5, revit_modified=False)
    assert out == {
        "ok": True, "count": 5, "drifted_count": 0,
        "drifts": [], "revit_modified": False,
    }


def test_bulk_setter_summary_with_drifts_preserves_order():
    from lib.tools._helpers import bulk_setter_summary
    drifts = [
        {"llm_id": "wall_001", "note": "n1"},
        {"llm_id": "wall_003", "note": "n2"},
    ]
    out = bulk_setter_summary(drifts, count=10, revit_modified=True)
    assert out["drifted_count"] == 2
    assert out["drifts"] == drifts
    assert out["count"] == 10
    assert out["revit_modified"] is True


def test_walls_set_height_many_kg_only_updates_all(kg_with_levels_and_walltype):
    kg, level, wt = kg_with_levels_and_walltype
    # Crée 3 murs via create_many
    items_create = [
        {"level_ref": level, "wall_type_ref": wt,
         "p1": [0.0, 0.0], "p2": [3.0, 0.0]},
        {"level_ref": level, "wall_type_ref": wt,
         "p1": [3.0, 0.0], "p2": [3.0, 4.0]},
        {"level_ref": level, "wall_type_ref": wt,
         "p1": [3.0, 4.0], "p2": [0.0, 4.0]},
    ]
    create_result = llm_protocol.dispatch_tool_use(
        "walls_create_many", {"items": items_create}, "t1", kg,
    )
    ids = json.loads(create_result["content"])["llm_ids"]

    set_items = [{"llm_id": lid, "height_m": 3.5} for lid in ids]
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height_many", {"items": set_items}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 3
    assert payload["drifted_count"] == 0
    assert payload["drifts"] == []
    assert payload["revit_modified"] is False
    for lid in ids:
        assert kg.get_node(lid)["height"] == 3.5


def test_walls_set_height_many_atomic_rejects_invalid_item(kg_with_wall):
    """Un item invalide → ValueError remontée, aucune mutation appliquée."""
    kg, _, _, wall = kg_with_wall
    pre = kg.get_node(wall)["height"]
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height_many",
        {"items": [
            {"llm_id": wall, "height_m": 3.5},
            {"llm_id": "wall_999", "height_m": 2.0},   # unknown
        ]}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "items[1]" in result["content"]
    # Atomicité : la première hauteur n'a pas été appliquée non plus.
    assert kg.get_node(wall)["height"] == pre


def test_walls_set_height_many_refuses_empty_items(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height_many", {"items": []}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "non-empty list" in result["content"]


def test_walls_set_height_many_refuses_non_wall(kg_with_wall):
    kg, level, _, _ = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "walls_set_height_many",
        {"items": [{"llm_id": level, "height_m": 3.0}]}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "not a Wall" in result["content"]


def test_walls_move_many_kg_only_translates_each(kg_with_levels_and_walltype):
    kg, level, wt = kg_with_levels_and_walltype
    create_result = llm_protocol.dispatch_tool_use(
        "walls_create_many",
        {"items": [
            {"level_ref": level, "wall_type_ref": wt,
             "p1": [0.0, 0.0], "p2": [3.0, 0.0]},
            {"level_ref": level, "wall_type_ref": wt,
             "p1": [0.0, 5.0], "p2": [3.0, 5.0]},
        ]}, "t1", kg,
    )
    ids = json.loads(create_result["content"])["llm_ids"]

    set_items = [
        {"llm_id": ids[0], "dx": 1.0, "dy": 0.0},
        {"llm_id": ids[1], "dx": 0.0, "dy": 2.0},
    ]
    result = llm_protocol.dispatch_tool_use(
        "walls_move_many", {"items": set_items}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert payload["drifted_count"] == 0
    assert payload["revit_modified"] is False
    n1 = kg.get_node(ids[0])
    n2 = kg.get_node(ids[1])
    assert n1["p1"] == [1.0, 0.0]
    assert n1["p2"] == [4.0, 0.0]
    assert n2["p1"] == [0.0, 7.0]
    assert n2["p2"] == [3.0, 7.0]


def test_walls_move_many_atomic_rollback(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    pre_p1 = list(kg.get_node(wall)["p1"])
    result = llm_protocol.dispatch_tool_use(
        "walls_move_many",
        {"items": [
            {"llm_id": wall, "dx": 1.0, "dy": 0.0},
            {"llm_id": wall, "dx": "bad", "dy": 0.0},  # type error
        ]}, "t1", kg,
    )
    assert result["is_error"] is True
    assert kg.get_node(wall)["p1"] == pre_p1


def test_openings_set_sill_height_many_kg_only(kg_with_opening_setup):
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    # Crée 2 fenêtres.
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0], "sill_height": 0.9},
        "t1", kg,
    )["content"])["llm_id"]
    w2 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [3.0, 0.0], "sill_height": 0.9},
        "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height_many",
        {"items": [
            {"llm_id": w1, "sill_height_m": 0.80},
            {"llm_id": w2, "sill_height_m": 0.80},
        ]}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert payload["drifted_count"] == 0
    assert kg.get_node(w1)["sill_height"] == 0.80
    assert kg.get_node(w2)["sill_height"] == 0.80


def test_openings_set_head_height_many_kg_only(kg_with_opening_setup):
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0]},
        "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height_many",
        {"items": [{"llm_id": w1, "head_height_m": 2.20}]}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 1
    assert payload["drifted_count"] == 0
    assert kg.get_node(w1)["head_height"] == 2.20


def test_openings_set_sill_height_many_refuses_non_opening(kg_with_opening_setup):
    kg, _, _, wall, _, _, _ = kg_with_opening_setup
    result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height_many",
        {"items": [{"llm_id": wall, "sill_height_m": 1.0}]}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "not a Door or Window" in result["content"]


def test_openings_set_type_many_kg_only_swaps_each(kg_with_opening_setup):
    kg, _, _, wall, wall2, door_type, window_type = kg_with_opening_setup
    # 2 fenêtres sur window_type → swap les deux vers une nouvelle variante.
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    w2 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall2, "family_type_ref": window_type,
         "position": [1.0, 5.0]}, "t2", kg,
    )["content"])["llm_id"]
    # Crée une variante (KG-only) — pas besoin de Revit.
    variant = kg.add_node("FamilyType", {
        "family_name": "Fenêtre fixe",
        "type_name": "1200 x 1400 mm",
        "category": "Windows",
        "dimensions": {"height_m": 1.40, "width_m": 1.20},
    })
    result = llm_protocol.dispatch_tool_use(
        "openings_set_type_many",
        {"items": [
            {"llm_id": w1, "new_family_type_ref": variant},
            {"llm_id": w2, "new_family_type_ref": variant},
        ]}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert payload["drifted_count"] == 0  # swap binaire, pas de drift KG-only.
    assert kg.get_node(w1)["type_ref"] == variant
    assert kg.get_node(w2)["type_ref"] == variant


def test_openings_set_type_many_refuses_category_mismatch(kg_with_opening_setup):
    """Une window ne peut pas swap vers un door type."""
    kg, _, _, wall, _, door_type, window_type = kg_with_opening_setup
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "openings_set_type_many",
        {"items": [{"llm_id": w1, "new_family_type_ref": door_type}]},
        "t2", kg,
    )
    assert result["is_error"] is True
    assert "category=Doors" in result["content"]
    # Atomique : w1 n'a pas swap.
    assert kg.get_node(w1)["type_ref"] == window_type


def test_rooms_set_name_many_kg_only(kg_with_seed):
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "A"}, "t1", kg,
    )["content"])["llm_id"]
    r2 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 0.0], "name": "B"}, "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_set_name_many",
        {"items": [
            {"llm_id": r1, "name": "Salon"},
            {"llm_id": r2, "name": "Cuisine"},
        ]}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert payload["drifted_count"] == 0
    assert kg.get_node(r1)["name"] == "Salon"
    assert kg.get_node(r2)["name"] == "Cuisine"


def test_rooms_set_name_many_allows_duplicate_names(kg_with_seed):
    """Revit autorise des Rooms homonymes (c'est Number qui est unique).
    Vérifier que le bulk ne pose pas de pré-check de collision."""
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    r2 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 0.0]}, "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_set_name_many",
        {"items": [
            {"llm_id": r1, "name": "Salon"},
            {"llm_id": r2, "name": "Salon"},  # doublon volontaire.
        ]}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2
    assert kg.get_node(r1)["name"] == "Salon"
    assert kg.get_node(r2)["name"] == "Salon"


def test_rooms_set_name_many_atomic_rollback(kg_with_seed):
    """Un name vide → tout le batch est refusé."""
    kg, level, _ = kg_with_seed
    r1 = json.loads(llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "before"}, "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "rooms_set_name_many",
        {"items": [
            {"llm_id": r1, "name": "after"},
            {"llm_id": r1, "name": "  "},   # invalid
        ]}, "t2", kg,
    )
    assert result["is_error"] is True
    assert "non-empty" in result["content"]
    # Atomic : "after" n'a pas été appliqué.
    assert kg.get_node(r1)["name"] == "before"


# ----- Auto-decouple sill ↔ head (session 2026-05-12 c) ---------------------


@pytest.fixture
def kg_with_window_with_rigid_type(kg_with_wall):
    """KG avec un FamilyType Window qui expose `dimensions.height_m=1.2`
    (= opening_height rigide côté famille) et 2 fenêtres déjà bindées
    dans le mur avec `sill=1.0, head=2.2` (cohérent : head − sill = 1.2)."""
    kg, level, wt, wall = kg_with_wall
    window_type = kg.add_node("FamilyType", {
        "family_name": "Fenêtre fixe",
        "type_name": "1200 x 1200 mm",
        "category": "Windows",
        "dimensions": {"height_m": 1.2, "width_m": 1.2},
    })
    w1 = kg.add_node("Window", {
        "type_ref": window_type,
        "host_wall_ref": wall,
        "position": [1.0, 0.0],
        "sill_height": 1.0,
        "head_height": 2.2,
    })
    kg.add_edge(wall, w1, "hosts")
    kg.add_edge(w1, window_type, "is_type")
    kg.add_edge(w1, level, "at_level")
    w2 = kg.add_node("Window", {
        "type_ref": window_type,
        "host_wall_ref": wall,
        "position": [3.0, 0.0],
        "sill_height": 1.0,
        "head_height": 2.2,
    })
    kg.add_edge(wall, w2, "hosts")
    kg.add_edge(w2, window_type, "is_type")
    kg.add_edge(w2, level, "at_level")
    return kg, level, wall, window_type, w1, w2


def test_set_head_height_auto_decouples_creates_variant(
    kg_with_window_with_rigid_type,
):
    """Setter head=2.0 sur fenêtre avec sill=1.0 et family h=1.2 :
    target_opening = 2.0 − 1.0 = 1.0 ≠ 1.2 → auto-découple :
    variant créé + swap + sill préservé à 1.0."""
    kg, _, _, source_type, w1, _ = kg_with_window_with_rigid_type
    pre_types = kg.count_by_type("FamilyType")
    result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.0}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["decoupled"] is True
    assert payload["auto_variant_created"] is True
    assert payload["new_type_ref"] is not None
    # Variant créé.
    assert kg.count_by_type("FamilyType") == pre_types + 1
    new_type = kg.get_node(payload["new_type_ref"])
    assert new_type["family_name"] == "Fenêtre fixe"
    assert new_type["dimensions"]["height_m"] == 1.0
    assert "(auto h100cm)" in new_type["type_name"]
    # Sill préservé à 1.0, head committé à 2.0, type swap-é.
    assert kg.get_node(w1)["sill_height"] == 1.0
    assert kg.get_node(w1)["head_height"] == 2.0
    assert kg.get_node(w1)["type_ref"] == payload["new_type_ref"]


def test_set_sill_height_auto_decouples_creates_variant(
    kg_with_window_with_rigid_type,
):
    """Symétrique : setter sill=0.6, head=2.2 → target_opening = 1.6 → auto."""
    kg, _, _, _, w1, _ = kg_with_window_with_rigid_type
    result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height",
        {"llm_id": w1, "sill_height_m": 0.6}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["decoupled"] is True
    assert payload["auto_variant_created"] is True
    new_type = kg.get_node(payload["new_type_ref"])
    assert new_type["dimensions"]["height_m"] == 1.6
    # head préservé à 2.2, sill committé à 0.6.
    assert kg.get_node(w1)["head_height"] == 2.2
    assert kg.get_node(w1)["sill_height"] == 0.6


def test_set_head_height_reuses_existing_variant(
    kg_with_window_with_rigid_type,
):
    """Premier appel crée le variant, second appel sur sibling le réutilise."""
    kg, _, _, _, w1, w2 = kg_with_window_with_rigid_type
    # 1er appel sur w1 → crée variant h=1.0
    first = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.0}, "t1", kg,
    )
    variant_id = json.loads(first["content"])["new_type_ref"]
    types_after_first = kg.count_by_type("FamilyType")
    # 2e appel sur w2 (même sill=1.0, même cible head=2.0) → réutilise.
    second = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w2, "head_height_m": 2.0}, "t2", kg,
    )
    payload2 = json.loads(second["content"])
    assert payload2["decoupled"] is True
    assert payload2["auto_variant_created"] is False  # réutilisé !
    assert payload2["new_type_ref"] == variant_id
    # Aucun nouveau FamilyType.
    assert kg.count_by_type("FamilyType") == types_after_first


def test_set_head_height_no_decouple_when_target_matches_family(
    kg_with_window_with_rigid_type,
):
    """sill=1.0, head=2.2, family_h=1.2. Setter head=2.2 (no-op réel) :
    target_opening = 1.2 = family_h → pas de drift → pas de découple."""
    kg, _, _, source_type, w1, _ = kg_with_window_with_rigid_type
    result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.2}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["decoupled"] is False
    assert payload["auto_variant_created"] is False
    assert payload["new_type_ref"] is None
    assert kg.get_node(w1)["type_ref"] == source_type  # type inchangé.


def test_set_head_height_no_decouple_when_family_has_no_dimensions(
    kg_with_opening_setup,
):
    """FamilyType sans `dimensions` → pas de prédiction → pas de découple
    (fallback legacy : Set direct, drift signalé au post-mortem)."""
    kg, _, _, wall, _, _, window_type = kg_with_opening_setup
    # window_type de la fixture base n'a PAS de dimensions.
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.0}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["decoupled"] is False


def test_set_head_height_preserve_sill_false_bypasses(
    kg_with_window_with_rigid_type,
):
    """Escape hatch : preserve_sill=False désactive le pré-flight. Pas
    de variant créé, type inchangé, comportement legacy."""
    kg, _, _, source_type, w1, _ = kg_with_window_with_rigid_type
    pre_types = kg.count_by_type("FamilyType")
    result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.0, "preserve_sill": False}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["decoupled"] is False
    assert payload["auto_variant_created"] is False
    # Aucun variant créé, type identique.
    assert kg.count_by_type("FamilyType") == pre_types
    assert kg.get_node(w1)["type_ref"] == source_type


def test_set_head_height_many_aggregates_decouple_counters(
    kg_with_window_with_rigid_type,
):
    """3 fenêtres mêmes refs, _many doit créer 1 variant et réutiliser
    pour les 2 autres."""
    kg, level, wall, source_type, w1, w2 = kg_with_window_with_rigid_type
    # Ajoute une 3e fenêtre.
    w3 = kg.add_node("Window", {
        "type_ref": source_type,
        "host_wall_ref": wall,
        "position": [4.0, 0.0],
        "sill_height": 1.0,
        "head_height": 2.2,
    })
    kg.add_edge(wall, w3, "hosts")
    kg.add_edge(w3, source_type, "is_type")
    kg.add_edge(w3, level, "at_level")
    pre_types = kg.count_by_type("FamilyType")

    result = llm_protocol.dispatch_tool_use(
        "openings_set_head_height_many",
        {"items": [
            {"llm_id": w1, "head_height_m": 2.0},
            {"llm_id": w2, "head_height_m": 2.0},
            {"llm_id": w3, "head_height_m": 2.0},
        ]}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 3
    assert payload["decoupled_count"] == 3
    # 1 seul variant créé, réutilisé pour les 2 autres.
    assert payload["auto_variants_created"] == 1
    assert kg.count_by_type("FamilyType") == pre_types + 1
    # Toutes les fenêtres pointent vers le même nouveau type.
    new_type = kg.get_node(w1)["type_ref"]
    assert kg.get_node(w2)["type_ref"] == new_type
    assert kg.get_node(w3)["type_ref"] == new_type
    # Sill préservé partout, head à 2.0.
    for w in (w1, w2, w3):
        assert kg.get_node(w)["sill_height"] == 1.0
        assert kg.get_node(w)["head_height"] == 2.0


def test_set_sill_height_many_preserve_head_false(
    kg_with_window_with_rigid_type,
):
    """_many avec escape hatch : decoupled_count = 0."""
    kg, _, _, source_type, w1, w2 = kg_with_window_with_rigid_type
    pre_types = kg.count_by_type("FamilyType")
    result = llm_protocol.dispatch_tool_use(
        "openings_set_sill_height_many",
        {"items": [
            {"llm_id": w1, "sill_height_m": 0.6},
            {"llm_id": w2, "sill_height_m": 0.6},
        ], "preserve_head": False}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["decoupled_count"] == 0
    assert payload["auto_variants_created"] == 0
    assert kg.count_by_type("FamilyType") == pre_types
    # Types inchangés.
    assert kg.get_node(w1)["type_ref"] == source_type
    assert kg.get_node(w2)["type_ref"] == source_type


# ----- Purge unused auto-variants (session 2026-05-12 d) --------------------


def test_purge_unused_variants_drops_orphans_keeps_used(
    kg_with_window_with_rigid_type,
):
    """Crée 2 auto-variants : un utilisé par une fenêtre, un orphelin.
    Purge → seul l'orphelin est supprimé."""
    kg, _, _, _, w1, _ = kg_with_window_with_rigid_type
    # Trigger 1er auto-variant via set_head_height (utilisé par w1).
    used_variant = json.loads(llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.0}, "t1", kg,
    )["content"])["new_type_ref"]
    # Crée manuellement un 2e variant orphelin (porte la marque [auto h140cm]).
    orphan = kg.add_node("FamilyType", {
        "family_name": "Fenêtre fixe",
        "type_name": "1200 x 1200 mm [auto h140cm]",
        "category": "Windows",
        "dimensions": {"height_m": 1.4, "width_m": 1.2},
    })
    pre_types = kg.count_by_type("FamilyType")

    result = llm_protocol.dispatch_tool_use(
        "openings_purge_unused_variants", {}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["scanned"] == 2
    assert payload["purged"] == 1
    assert payload["revit_deleted"] is False
    # used_variant doit apparaître dans kept (reason=in_use).
    kept_ids = [k["llm_id"] for k in payload["kept"]]
    assert used_variant in kept_ids
    assert any(k["reason"] == "in_use" for k in payload["kept"])
    # Orphelin soft-deleted.
    assert kg.get_node(orphan)["deleted_at_turn"] is not None
    # Le compteur baisse de 1 (orphan filtré par find_by_type).
    assert kg.count_by_type("FamilyType") == pre_types - 1


def test_purge_unused_variants_ignores_non_auto_types(
    kg_with_window_with_rigid_type,
):
    """Un FamilyType normal (sans marqueur [auto]) ne doit JAMAIS être
    purgé, même s'il n'est utilisé par rien."""
    kg, _, _, source_type, w1, _ = kg_with_window_with_rigid_type
    # Crée un type normal orphelin.
    normal = kg.add_node("FamilyType", {
        "family_name": "Porte simple",
        "type_name": "Variante manuelle 0900 x 2100 mm",
        "category": "Doors",
        "dimensions": {"height_m": 2.1, "width_m": 0.9},
    })
    result = llm_protocol.dispatch_tool_use(
        "openings_purge_unused_variants", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["scanned"] == 0   # le type normal n'a pas le marqueur.
    assert payload["purged"] == 0
    # Le type normal est toujours vivant.
    assert kg.get_node(normal)["deleted_at_turn"] is None


def test_purge_unused_variants_filter_by_category(
    kg_with_window_with_rigid_type,
):
    """Crée 2 orphans, un Windows + un Doors. Purge avec category=Windows
    ne touche que les Windows."""
    kg, _, _, _, _, _ = kg_with_window_with_rigid_type
    win_orphan = kg.add_node("FamilyType", {
        "family_name": "Fenêtre fixe",
        "type_name": "f [auto h100cm]",
        "category": "Windows",
        "dimensions": {"height_m": 1.0},
    })
    door_orphan = kg.add_node("FamilyType", {
        "family_name": "Porte simple",
        "type_name": "p [auto h200cm]",
        "category": "Doors",
        "dimensions": {"height_m": 2.0},
    })
    result = llm_protocol.dispatch_tool_use(
        "openings_purge_unused_variants",
        {"category": "Windows"}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["scanned"] == 1   # filtré sur Windows.
    assert payload["purged"] == 1
    assert kg.get_node(win_orphan)["deleted_at_turn"] is not None
    # Door orphan préservé (autre catégorie).
    assert kg.get_node(door_orphan)["deleted_at_turn"] is None


def test_purge_unused_variants_treats_soft_deleted_openings_as_unused(
    kg_with_window_with_rigid_type,
):
    """Variant dont la seule fenêtre référente est soft-deleted = unused."""
    kg, _, _, _, w1, _ = kg_with_window_with_rigid_type
    variant = json.loads(llm_protocol.dispatch_tool_use(
        "openings_set_head_height",
        {"llm_id": w1, "head_height_m": 2.0}, "t1", kg,
    )["content"])["new_type_ref"]
    # Soft-delete la fenêtre.
    llm_protocol.dispatch_tool_use(
        "openings_delete", {"llm_id": w1}, "t2", kg,
    )
    result = llm_protocol.dispatch_tool_use(
        "openings_purge_unused_variants", {}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["purged"] == 1
    assert kg.get_node(variant)["deleted_at_turn"] is not None


def test_purge_unused_variants_no_auto_types_present(kg_with_seed):
    """Aucun auto-variant dans le projet → scanned=0, purged=0."""
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "openings_purge_unused_variants", {}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["scanned"] == 0
    assert payload["purged"] == 0
    assert payload["kept"] == []


def test_purge_unused_variants_refuses_invalid_category(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "openings_purge_unused_variants",
        {"category": "Walls"}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "Doors" in result["content"]


# ----- Bulk filter-based (session 2026-05-12 e — UC7) -----------------------


def test_bulk_resolve_filter_by_type(kg_with_levels_and_walltype):
    """Filter type=Wall sur un KG vide de murs → count=0."""
    kg, _, _ = kg_with_levels_and_walltype
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Wall"}}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 0


def test_bulk_resolve_filter_by_type_and_level_ref(kg_with_levels_and_walltype):
    """Crée 3 murs au N00 + 2 au N01, filter sur N01 → 2 hits."""
    kg, level_n00, wt = kg_with_levels_and_walltype
    # level_n01 ajouté par la fixture.
    level_n01 = [
        nid for nid in kg.find_by_type("Level")
        if kg.get_node(nid)["name"] == "N01"
    ][0]
    # 3 murs N00.
    for k in range(3):
        kg.add_node("Wall", {
            "type_ref": wt, "level_ref": level_n00,
            "p1": [0.0, float(k)], "p2": [1.0, float(k)],
            "length": 1.0, "height": 2.7,
        })
    # 2 murs N01.
    for k in range(2):
        kg.add_node("Wall", {
            "type_ref": wt, "level_ref": level_n01,
            "p1": [0.0, float(k)], "p2": [1.0, float(k)],
            "length": 1.0, "height": 2.7,
        })
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Wall", "level_ref": level_n01}}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2


def test_bulk_resolve_filter_ignores_soft_deleted(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    llm_protocol.dispatch_tool_use("walls_delete", {"llm_id": wall}, "t1", kg)
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Wall"}}, "t2", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 0


def test_bulk_resolve_filter_name_contains(kg_with_seed):
    kg, level, _ = kg_with_seed
    llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [0.0, 0.0], "name": "Salon principal"},
        "t1", kg,
    )
    llm_protocol.dispatch_tool_use(
        "rooms_create",
        {"level_ref": level, "point": [5.0, 0.0], "name": "Cuisine"},
        "t2", kg,
    )
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Room", "name_contains": "salon"}}, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 1


def test_bulk_resolve_filter_name_regex(kg_with_seed):
    kg, level, _ = kg_with_seed
    for name in ("Chambre 1", "Chambre 2", "Cuisine", "Salon"):
        llm_protocol.dispatch_tool_use(
            "rooms_create",
            {"level_ref": level, "point": [0.0, 0.0], "name": name},
            "t" + name, kg,
        )
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Room", "name_regex": "^Chambre"}}, "tx", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 2


def test_bulk_resolve_filter_refuses_unknown_key(kg_with_seed):
    """Faute de frappe LLM (levle_ref au lieu de level_ref) → erreur,
    pas un match silencieux à zéro."""
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Wall", "levle_ref": "level_001"}}, "t1", kg,
    )
    assert result["is_error"] is True
    assert "Unknown filter keys" in result["content"]


def test_bulk_resolve_filter_truncates_large_match(kg_with_levels_and_walltype):
    """> preview_limit → tronque + first/last/note."""
    kg, level, wt = kg_with_levels_and_walltype
    for k in range(15):
        kg.add_node("Wall", {
            "type_ref": wt, "level_ref": level,
            "p1": [0.0, float(k)], "p2": [1.0, float(k)],
            "length": 1.0, "height": 2.7,
        })
    result = llm_protocol.dispatch_tool_use(
        "bulk_resolve_filter",
        {"filter": {"type": "Wall"}, "preview_limit": 5}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["count"] == 15
    assert len(payload["llm_ids"]) == 5
    assert "first_llm_id" in payload
    assert "last_llm_id" in payload
    assert "Truncated" in payload["note"]


def test_bulk_apply_to_filter_no_match_is_noop(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_set_height_many",
            "tool_args": {"height_m": 3.0},
        }, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["matched_count"] == 0
    assert payload["inner"] is None


def test_bulk_apply_to_filter_roundtrip_walls_set_height(
    kg_with_levels_and_walltype,
):
    """3 murs créés → apply set_height_many=3.5 via filter → KG mis à jour."""
    kg, level, wt = kg_with_levels_and_walltype
    ids = []
    for k in range(3):
        nid = kg.add_node("Wall", {
            "type_ref": wt, "level_ref": level,
            "p1": [0.0, float(k)], "p2": [1.0, float(k)],
            "length": 1.0, "height": 2.7,
        })
        ids.append(nid)
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_set_height_many",
            "tool_args": {"height_m": 3.5},
        }, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["matched_count"] == 3
    assert payload["target_tool"] == "walls_set_height_many"
    assert payload["inner"]["count"] == 3
    assert payload["inner"]["drifted_count"] == 0
    for nid in ids:
        assert kg.get_node(nid)["height"] == 3.5


def test_bulk_apply_to_filter_refuses_unknown_target(kg_with_seed):
    kg, _, _ = kg_with_seed
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_set_height_many",  # OK
            "tool_args": {"height_m": 3.0},
        }, "t1", kg,
    )
    # Pas d'erreur (match=0 → no-op), juste vérifie que le path tient.
    assert result["is_error"] is False

    # Maintenant un tool inconnu — doit lever.
    result2 = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_does_not_exist",
            "tool_args": {"height_m": 3.0},
        }, "t2", kg,
    )
    # Note: si filter matched 0, on n'atteint pas la résolution du tool.
    # Donc il faut un wall pour exercer la branche d'erreur.
    kg.add_node("Wall", {
        "type_ref": "walltype_001", "level_ref": "level_001",
        "p1": [0.0, 0.0], "p2": [1.0, 0.0], "length": 1.0, "height": 2.7,
    })
    result3 = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_does_not_exist",
            "tool_args": {"height_m": 3.0},
        }, "t3", kg,
    )
    assert result3["is_error"] is True
    assert "Unknown target_tool" in result3["content"]


def test_bulk_apply_to_filter_refuses_non_many_tool(kg_with_wall):
    """target_tool sans param items → refus."""
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_set_height",  # solo, pas _many
            "tool_args": {"height_m": 3.0},
        }, "t1", kg,
    )
    assert result["is_error"] is True
    assert "doesn't accept `items`" in result["content"]


def test_bulk_apply_to_filter_refuses_llm_id_in_tool_args(kg_with_wall):
    kg, _, _, wall = kg_with_wall
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_set_height_many",
            "tool_args": {"height_m": 3.0, "llm_id": wall},
        }, "t1", kg,
    )
    assert result["is_error"] is True
    assert "llm_id" in result["content"]


def test_bulk_apply_to_filter_with_openings(kg_with_opening_setup):
    """Cas du soir 2026-05-11 simulé : bulk sill sur toutes les fenêtres."""
    kg, _, _, wall, wall2, _, window_type = kg_with_opening_setup
    w1 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall, "family_type_ref": window_type,
         "position": [1.0, 0.0]}, "t1", kg,
    )["content"])["llm_id"]
    w2 = json.loads(llm_protocol.dispatch_tool_use(
        "openings_create_window",
        {"host_wall_ref": wall2, "family_type_ref": window_type,
         "position": [1.0, 5.0]}, "t2", kg,
    )["content"])["llm_id"]
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Window"},
            "target_tool": "openings_set_sill_height_many",
            "tool_args": {"sill_height_m": 0.80},
        }, "t3", kg,
    )
    payload = json.loads(result["content"])
    assert payload["matched_count"] == 2
    assert payload["inner"]["count"] == 2
    assert kg.get_node(w1)["sill_height"] == 0.80
    assert kg.get_node(w2)["sill_height"] == 0.80


def test_bulk_apply_to_filter_atomic_rollback_on_inner_failure(
    kg_with_levels_and_walltype,
):
    """Si le tool cible lève (ex : valeur invalide pour un item),
    l'outer kg.transaction rollback tout le batch."""
    kg, level, wt = kg_with_levels_and_walltype
    ids = []
    for k in range(2):
        nid = kg.add_node("Wall", {
            "type_ref": wt, "level_ref": level,
            "p1": [0.0, float(k)], "p2": [1.0, float(k)],
            "length": 1.0, "height": 2.7,
        })
        ids.append(nid)
    # height_m négative → _validate_set_height_item lève.
    result = llm_protocol.dispatch_tool_use(
        "bulk_apply_to_filter",
        {
            "filter": {"type": "Wall"},
            "target_tool": "walls_set_height_many",
            "tool_args": {"height_m": -1.0},
        }, "t1", kg,
    )
    assert result["is_error"] is True
    # Atomicité : heights inchangées.
    for nid in ids:
        assert kg.get_node(nid)["height"] == 2.7
