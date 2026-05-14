"""Tests for floor holes detection (cages d'escalier, patios, atria).

Couvre :
- `dwg_section_reader.read_floor_holes_from_plan` : détection layer-based
  des trous + exclusion des projections overhead.
- `floors.create_many` avec param `holes` : Floor KG-side avec attr
  `holes` peuplé + aire nette = outer − Σ holes.
- Round-trip : fixture synthétique → audit ne change rien (read-only),
  execute crée la dalle avec son hole.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib import dwg_reader, dwg_section_reader
from lib.dwg_section_reader import FloorHole, read_floor_holes_from_plan
from lib.project_kg import ProjectKG


_SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic_holes" / "floor_with_holes.dxf"
_P7_PLAN_N1 = Path(__file__).parent / "fixtures" / "P7" / "Projet8-Plan d'étage - Niveau 1.dxf"


# ----- read_floor_holes_from_plan ---------------------------------------


def test_read_floor_holes_returns_2_holes_on_synthetic():
    ents, _ = dwg_reader.parse(_SYNTHETIC)
    holes = read_floor_holes_from_plan(ents)
    # Fixture has 1 stair + 1 patio (opening) + 1 OVHD (excluded by default).
    assert len(holes) == 2
    kinds = {h.kind for h in holes}
    assert kinds == {"stair", "opening"}
    for h in holes:
        assert h.is_overhead is False
        assert len(h.points) >= 3


def test_read_floor_holes_includes_overhead_when_flag_set():
    ents, _ = dwg_reader.parse(_SYNTHETIC)
    holes = read_floor_holes_from_plan(ents, include_overhead=True)
    assert len(holes) == 3
    overheads = [h for h in holes if h.is_overhead]
    assert len(overheads) == 1
    assert overheads[0].layer == "A-FLOR-OVHD"
    assert overheads[0].kind == "overhead"


def test_read_floor_holes_no_false_positives_on_p7():
    """P7 N1 is a simple rectangle with no stair/patio/atrium — must
    return 0 holes despite having entities on layer `A-FLOR` (saillies)."""
    ents, _ = dwg_reader.parse(_P7_PLAN_N1)
    holes = read_floor_holes_from_plan(ents)
    assert holes == []


def test_read_floor_holes_keyword_fallback():
    """Non-AIA layer name with French keyword should still match."""
    from lib.dwg_reader import DwgEntity
    fake_ents = [
        DwgEntity(
            kind="LWPOLYLINE", layer="MON-LAYER-ESCALIER",
            coords=[[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
            attrs={"closed": True},
        ),
        DwgEntity(
            kind="LWPOLYLINE", layer="A-OTHER-NON-HOLE",
            coords=[[5, 5, 0], [6, 5, 0], [6, 6, 0], [5, 6, 0]],
            attrs={"closed": True},
        ),
    ]
    holes = read_floor_holes_from_plan(fake_ents)
    assert len(holes) == 1
    assert holes[0].kind == "stair"
    assert holes[0].layer == "MON-LAYER-ESCALIER"


def test_read_floor_holes_skips_open_polylines():
    """An open polyline on a hole layer should NOT be detected as a hole."""
    from lib.dwg_reader import DwgEntity
    fake_ents = [
        DwgEntity(
            kind="LWPOLYLINE", layer="A-FLOR-STAIR",
            coords=[[0, 0, 0], [1, 0, 0], [1, 1, 0]],
            attrs={"closed": False},  # open !
        ),
    ]
    assert read_floor_holes_from_plan(fake_ents) == []


def test_read_floor_holes_skips_too_few_vertices():
    """Polyline with < 3 vertices skipped."""
    from lib.dwg_reader import DwgEntity
    fake_ents = [
        DwgEntity(
            kind="LWPOLYLINE", layer="A-FLOR-STAIR",
            coords=[[0, 0, 0], [1, 0, 0]],  # only 2 pts
            attrs={"closed": True},
        ),
    ]
    assert read_floor_holes_from_plan(fake_ents) == []


# ----- floors.create_many with holes ------------------------------------


@pytest.fixture
def kg_with_level_and_type():
    kg = ProjectKG(project_id="test-holes")
    level = kg.add_node("Level", {"name": "N0", "elevation": 0.0})
    ft = kg.add_node("FloorType", {"name": "DXF_FLOOR_25cm", "total_thickness": 0.25})
    return kg, level, ft


def test_floor_create_many_with_holes_stores_kg_attr(kg_with_level_and_type):
    from lib.tools.floors import create_many

    kg, level_ref, ft_ref = kg_with_level_and_type
    outer = [[0, 0], [10, 0], [10, 8], [0, 8]]  # 10x8 = 80 m²
    hole_stair = [[2, 4], [4, 4], [4, 7], [2, 7]]  # 2x3 = 6 m²
    hole_patio = [[7, 1], [9, 1], [9, 3], [7, 3]]  # 2x2 = 4 m²

    create_many(
        kg=kg, doc=None,
        items=[{
            "level_ref": level_ref, "floor_type_ref": ft_ref,
            "boundary": outer, "holes": [hole_stair, hole_patio],
        }],
    )

    floors = [kg.get_node(nid) for nid in kg.find_by_type("Floor")]
    assert len(floors) == 1
    f = floors[0]
    assert "holes" in f
    assert len(f["holes"]) == 2
    # Net area = 80 − 6 − 4 = 70 m².
    assert abs(f["area_m2"] - 70.0) < 1e-6


def test_floor_create_many_without_holes_omits_attr(kg_with_level_and_type):
    from lib.tools.floors import create_many

    kg, level_ref, ft_ref = kg_with_level_and_type
    outer = [[0, 0], [10, 0], [10, 8], [0, 8]]

    create_many(
        kg=kg, doc=None,
        items=[{
            "level_ref": level_ref, "floor_type_ref": ft_ref,
            "boundary": outer,
        }],
    )

    f = kg.get_node(kg.find_by_type("Floor")[0])
    assert "holes" not in f
    assert abs(f["area_m2"] - 80.0) < 1e-6


def test_floor_create_many_rejects_invalid_hole(kg_with_level_and_type):
    from lib.tools.floors import create_many

    kg, level_ref, ft_ref = kg_with_level_and_type
    outer = [[0, 0], [10, 0], [10, 8], [0, 8]]
    bad_hole = [[1, 1], [2, 2]]  # < 3 distinct points

    with pytest.raises(ValueError, match=r"holes\[0\].*"):
        create_many(
            kg=kg, doc=None,
            items=[{
                "level_ref": level_ref, "floor_type_ref": ft_ref,
                "boundary": outer, "holes": [bad_hole],
            }],
        )
    # Atomic : pas de floor créé sur exception.
    assert kg.find_by_type("Floor") == []


def test_net_floor_area_subtracts_holes():
    from lib.tools.floors import _net_floor_area

    outer = [[0, 0], [10, 0], [10, 10], [0, 10]]  # 100 m²
    hole = [[2, 2], [4, 2], [4, 4], [2, 4]]  # 4 m²
    assert abs(_net_floor_area(outer, [hole]) - 96.0) < 1e-6
    assert abs(_net_floor_area(outer, None) - 100.0) < 1e-6
    assert abs(_net_floor_area(outer, []) - 100.0) < 1e-6
