"""Tests UC0/UC2 Floor (sol / dalle) — KG-only path.

Couvre :
- Schema KG (Floor + FloorType nodes acceptés, requis/optionnels OK).
- floors_create : single, validation level/floor_type refs, validation
  boundary (≥ 3 sommets, pas de doublons adjacents, dédup trailing).
- floors_create_many : transactionnel, retour bulk_summary.
- floors_delete + floors_delete_many : soft delete.
- catalog_list_floors + catalog_list_floor_types : roundtrip.
- shoelace area : valeurs déterministes connues (carré 5×5 = 25 m²,
  triangle rectangle 3-4-5 = 6 m²).

Pas de tests Revit (pas de Document dans la harness pytest). La branche
`doc is not None` est exercée en runtime via `prompt.pushbutton`.
"""
from __future__ import annotations

import json

import pytest

from lib import llm_protocol
from lib.project_kg import ProjectKG
from lib.tools.floors import _shoelace_area, _validate_boundary


# ----- Fixtures ---------------------------------------------------------


@pytest.fixture
def kg_with_floor_type():
    """KG carrying 1 Level + 1 FloorType (KG-only)."""
    llm_protocol.reset_registry()
    llm_protocol.get_registry()  # trigger auto-import

    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    ft = kg.add_node("FloorType", {"name": "DAL200", "total_thickness": 0.2})
    return kg, level, ft


# ----- KG schema -------------------------------------------------------


def test_floor_node_schema_required_attrs():
    kg = ProjectKG("p")
    kg.advance_turn()
    lvl = kg.add_node("Level", {"name": "N0", "elevation": 0.0})
    ft = kg.add_node("FloorType", {"name": "X", "total_thickness": 0.2})
    nid = kg.add_node("Floor", {
        "type_ref": ft, "level_ref": lvl,
        "boundary": [[0, 0], [5, 0], [5, 5], [0, 5]],
        "area_m2": 25.0,
    })
    node = kg.get_node(nid)
    assert node["_type"] == "Floor"
    assert node["area_m2"] == 25.0


def test_floor_node_schema_missing_attr_rejected():
    kg = ProjectKG("p")
    kg.advance_turn()
    lvl = kg.add_node("Level", {"name": "N0", "elevation": 0.0})
    ft = kg.add_node("FloorType", {"name": "X", "total_thickness": 0.2})
    with pytest.raises(ValueError, match="Missing required attrs"):
        kg.add_node("Floor", {
            "type_ref": ft, "level_ref": lvl,
            "boundary": [[0, 0], [1, 0], [1, 1]],
            # area_m2 missing
        })


# ----- _shoelace_area ----------------------------------------------------


def test_shoelace_square():
    assert _shoelace_area([[0, 0], [5, 0], [5, 5], [0, 5]]) == 25.0


def test_shoelace_triangle_3_4_5():
    # Triangle with legs 3 and 4 → area = 6.
    assert _shoelace_area([[0, 0], [3, 0], [0, 4]]) == 6.0


def test_shoelace_orientation_indifferent():
    """CCW vs CW give the same absolute area."""
    ccw = [[0, 0], [5, 0], [5, 5], [0, 5]]
    cw = list(reversed(ccw))
    assert _shoelace_area(ccw) == _shoelace_area(cw)


def test_shoelace_collapsed_polygon_returns_zero():
    """Moins de 3 sommets → aire 0."""
    assert _shoelace_area([[0, 0], [5, 0]]) == 0.0


# ----- _validate_boundary ----------------------------------------------


def test_validate_boundary_strips_trailing_duplicate():
    cleaned = _validate_boundary([[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]])
    assert cleaned == [[0, 0], [5, 0], [5, 5], [0, 5]]


def test_validate_boundary_rejects_under_three_points():
    with pytest.raises(ValueError, match="≥ 3 distinct points"):
        _validate_boundary([[0, 0], [5, 0]])


def test_validate_boundary_rejects_adjacent_duplicates():
    with pytest.raises(ValueError, match="zero-length segment"):
        _validate_boundary([[0, 0], [5, 0], [5, 0], [0, 5]])


def test_validate_boundary_rejects_non_list():
    with pytest.raises(ValueError, match="must be a list"):
        _validate_boundary("not a list")


# ----- floors_create ----------------------------------------------------


def test_floors_create_single_square(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    result = llm_protocol.dispatch_tool_use(
        "floors_create",
        {
            "level_ref": level,
            "floor_type_ref": ft,
            "boundary": [[0, 0], [5, 0], [5, 5], [0, 5]],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["area_m2"] == 25.0
    assert payload["revit_id"] is None  # hors-Revit
    # Vérifie les edges at_level + is_type.
    fid = payload["llm_id"]
    edges = list(kg._g.out_edges(fid, keys=True))  # noqa: SLF001
    edge_types = sorted(k for _, _, k in edges)
    assert edge_types == ["at_level", "is_type"]


def test_floors_create_rejects_unknown_level(kg_with_floor_type):
    kg, _, ft = kg_with_floor_type
    result = llm_protocol.dispatch_tool_use(
        "floors_create",
        {
            "level_ref": "nonexistent",
            "floor_type_ref": ft,
            "boundary": [[0, 0], [1, 0], [1, 1]],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True
    assert "level_ref" in result["content"].lower()


def test_floors_create_rejects_unknown_floor_type(kg_with_floor_type):
    kg, level, _ = kg_with_floor_type
    result = llm_protocol.dispatch_tool_use(
        "floors_create",
        {
            "level_ref": level,
            "floor_type_ref": "nonexistent",
            "boundary": [[0, 0], [1, 0], [1, 1]],
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True


def test_floors_create_rejects_bad_boundary(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    result = llm_protocol.dispatch_tool_use(
        "floors_create",
        {
            "level_ref": level,
            "floor_type_ref": ft,
            "boundary": [[0, 0]],  # 1 point seulement
        },
        "t1",
        kg,
    )
    assert result["is_error"] is True


# ----- floors_create_many ----------------------------------------------


def test_floors_create_many_creates_n_floors(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    items = [
        {"level_ref": level, "floor_type_ref": ft,
         "boundary": [[0, 0], [3, 0], [3, 3], [0, 3]]},
        {"level_ref": level, "floor_type_ref": ft,
         "boundary": [[10, 0], [13, 0], [13, 3], [10, 3]]},
    ]
    result = llm_protocol.dispatch_tool_use(
        "floors_create_many", {"items": items}, "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["count"] == 2


def test_floors_create_many_rolls_back_on_invalid_item(kg_with_floor_type):
    """Si un item est invalide, aucun floor ne doit avoir été créé."""
    kg, level, ft = kg_with_floor_type
    initial_floor_count = kg.count_by_type("Floor")
    items = [
        {"level_ref": level, "floor_type_ref": ft,
         "boundary": [[0, 0], [3, 0], [3, 3], [0, 3]]},
        {"level_ref": level, "floor_type_ref": "nonexistent",
         "boundary": [[10, 0], [13, 0], [13, 3], [10, 3]]},
    ]
    result = llm_protocol.dispatch_tool_use(
        "floors_create_many", {"items": items}, "t1", kg,
    )
    assert result["is_error"] is True
    assert kg.count_by_type("Floor") == initial_floor_count


# ----- floors_delete ---------------------------------------------------


def test_floors_delete_soft_deletes_node(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    create = llm_protocol.dispatch_tool_use(
        "floors_create",
        {"level_ref": level, "floor_type_ref": ft,
         "boundary": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        "t1", kg,
    )
    fid = json.loads(create["content"])["llm_id"]
    delete = llm_protocol.dispatch_tool_use(
        "floors_delete", {"llm_id": fid}, "t2", kg,
    )
    payload = json.loads(delete["content"])
    assert payload["ok"] is True
    assert payload["revit_deleted"] is False
    assert kg.get_node(fid).get("deleted_at_turn") is not None


def test_floors_delete_many(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    ids: list = []
    for i in range(3):
        r = llm_protocol.dispatch_tool_use(
            "floors_create",
            {"level_ref": level, "floor_type_ref": ft,
             "boundary": [[i * 10, 0], [i * 10 + 1, 0], [i * 10 + 1, 1]]},
            "t1", kg,
        )
        ids.append(json.loads(r["content"])["llm_id"])
    r = llm_protocol.dispatch_tool_use(
        "floors_delete_many", {"llm_ids": ids}, "t2", kg,
    )
    payload = json.loads(r["content"])
    assert payload["count"] == 3
    for fid in ids:
        assert kg.get_node(fid).get("deleted_at_turn") is not None


# ----- catalog_list_* --------------------------------------------------


def test_catalog_list_floor_types(kg_with_floor_type):
    kg, _, ft = kg_with_floor_type
    r = llm_protocol.dispatch_tool_use(
        "catalog_list_floor_types", {}, "t1", kg,
    )
    payload = json.loads(r["content"])
    assert any(item["llm_id"] == ft for item in payload["floor_types"])


def test_catalog_list_floors_returns_live(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    r = llm_protocol.dispatch_tool_use(
        "floors_create",
        {"level_ref": level, "floor_type_ref": ft,
         "boundary": [[0, 0], [5, 0], [5, 5], [0, 5]]},
        "t1", kg,
    )
    fid = json.loads(r["content"])["llm_id"]
    r2 = llm_protocol.dispatch_tool_use("catalog_list_floors", {}, "t2", kg)
    payload = json.loads(r2["content"])
    listed = [f for f in payload["floors"] if f["llm_id"] == fid]
    assert len(listed) == 1
    assert listed[0]["area_m2"] == 25.0
    assert listed[0]["vertex_count"] == 4


def test_catalog_list_floors_excludes_soft_deleted(kg_with_floor_type):
    kg, level, ft = kg_with_floor_type
    r = llm_protocol.dispatch_tool_use(
        "floors_create",
        {"level_ref": level, "floor_type_ref": ft,
         "boundary": [[0, 0], [5, 0], [5, 5], [0, 5]]},
        "t1", kg,
    )
    fid = json.loads(r["content"])["llm_id"]
    llm_protocol.dispatch_tool_use("floors_delete", {"llm_id": fid}, "t2", kg)
    r2 = llm_protocol.dispatch_tool_use("catalog_list_floors", {}, "t3", kg)
    payload = json.loads(r2["content"])
    assert all(f["llm_id"] != fid for f in payload["floors"])


# ----- preprocess auto-scan --------------------------------------------


def test_preprocess_detects_tous_les_sols():
    from lib import preprocess
    detected = preprocess.detect_exhaustive_collections(
        "supprime tous les sols du niveau N0"
    )
    assert detected == [("catalog_list_floors", "floors")]


def test_preprocess_detects_toutes_les_dalles():
    from lib import preprocess
    detected = preprocess.detect_exhaustive_collections(
        "passe toutes les dalles à un autre type"
    )
    assert detected == [("catalog_list_floors", "floors")]


def test_preprocess_detects_types_de_sol():
    from lib import preprocess
    detected = preprocess.detect_exhaustive_collections(
        "liste tous les types de sol disponibles"
    )
    assert detected == [("catalog_list_floor_types", "floor_types")]


def test_preprocess_keeps_niveaux_for_french_etages():
    from lib import preprocess
    detected = preprocess.detect_exhaustive_collections(
        "liste tous les étages du projet"
    )
    assert detected == [("catalog_list_levels", "levels")]
