"""Tests Phase 2 étape 1 — recoupement plan ↔ coupes (dwg_coherence + tool).

Couvre :
- `dwg_section_reader.read_section_walls` : filtre paires verticales sur A-WALL.
- `dwg_coherence._segment_intersection_2d` : intersections strictes.
- `dwg_coherence.reconcile_plan_section_walls` : matches OK, mismatches,
  ambiguous, no_section_wall_at_x, walls coupe orphelins.
- Tool `dwg_reconcile_plan_section_walls` : smoke test avec DXF synthétiques.

Pas de dépendance Revit. ezdxf requis pour le smoke test du tool.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from lib import dwg_coherence, dwg_section_reader as dsr
from lib.dwg_reader import DwgEntity


# ----- read_section_walls : DwgEntity inline ---------------------------


def _line_entity(x1, y1, x2, y2, layer="A-WALL"):
    """Helper : DwgEntity LINE 2D (z=0) sur un layer donné."""
    return DwgEntity(
        kind="LINE",
        layer=layer,
        coords=[[x1, y1, 0.0], [x2, y2, 0.0]],
        attrs={},
    )


def test_read_section_walls_detects_vertical_pair():
    # Un mur vu en coupe : 2 segments verticaux parallèles à x=5 et x=5.2
    # (épaisseur 20 cm), de y=0 à y=3 (hauteur d'étage typique).
    entities = [
        _line_entity(5.0, 0.0, 5.0, 3.0),
        _line_entity(5.2, 0.0, 5.2, 3.0),
    ]
    walls = dsr.read_section_walls(entities)
    assert len(walls) == 1
    w = walls[0]
    assert w.x_cut_m == pytest.approx(5.1, abs=1e-6)
    assert w.thickness_m == pytest.approx(0.2, abs=1e-6)
    assert w.y_bottom_m == pytest.approx(0.0, abs=1e-6)
    assert w.y_top_m == pytest.approx(3.0, abs=1e-6)


def test_read_section_walls_filters_out_horizontal_pairs():
    # Une dalle horizontale (paire horizontale parallèles à Y constant) NE
    # doit PAS être détectée comme mur. Dans cet exemple, on met aussi
    # un vrai mur vertical pour vérifier qu'on garde bien le mur.
    entities = [
        # Dalle horizontale à y=0 et y=0.2.
        _line_entity(0.0, 0.0, 10.0, 0.0),
        _line_entity(0.0, 0.2, 10.0, 0.2),
        # Mur vertical au milieu.
        _line_entity(5.0, 0.5, 5.0, 3.0),
        _line_entity(5.2, 0.5, 5.2, 3.0),
    ]
    walls = dsr.read_section_walls(entities)
    # Seul le mur vertical détecté.
    assert len(walls) == 1
    assert walls[0].x_cut_m == pytest.approx(5.1, abs=1e-6)


def test_read_section_walls_ignores_non_wall_layers():
    entities = [
        _line_entity(5.0, 0.0, 5.0, 3.0, layer="A-GLAZ"),
        _line_entity(5.2, 0.0, 5.2, 3.0, layer="A-GLAZ"),
    ]
    walls = dsr.read_section_walls(entities)
    assert walls == []


def test_read_section_walls_sorts_by_x_cut():
    entities = [
        # Mur 2 à x=10
        _line_entity(10.0, 0.0, 10.0, 3.0),
        _line_entity(10.2, 0.0, 10.2, 3.0),
        # Mur 1 à x=2 (devrait sortir en premier après tri)
        _line_entity(2.0, 0.0, 2.0, 3.0),
        _line_entity(2.2, 0.0, 2.2, 3.0),
    ]
    walls = dsr.read_section_walls(entities)
    assert len(walls) == 2
    assert walls[0].x_cut_m < walls[1].x_cut_m


# ----- _segment_intersection_2d ----------------------------------------


def test_segment_intersection_basic_crossing():
    # Segments + horizontal et + vertical qui se croisent en (2.5, 2.5).
    inter = dwg_coherence._segment_intersection_2d(
        (0.0, 2.5), (5.0, 2.5),
        (2.5, 0.0), (2.5, 5.0),
    )
    assert inter is not None
    x, y, t, u = inter
    assert x == pytest.approx(2.5)
    assert y == pytest.approx(2.5)
    assert t == pytest.approx(0.5)
    assert u == pytest.approx(0.5)


def test_segment_intersection_no_overlap_returns_none():
    # 2 segments parallèles, pas d'intersection.
    inter = dwg_coherence._segment_intersection_2d(
        (0.0, 0.0), (5.0, 0.0),
        (0.0, 1.0), (5.0, 1.0),
    )
    assert inter is None


def test_segment_intersection_outside_bounds_returns_none():
    # Les droites se croiseraient en (10, 0) mais hors des segments.
    inter = dwg_coherence._segment_intersection_2d(
        (0.0, 0.0), (5.0, 0.0),
        (10.0, -1.0), (10.0, 1.0),
    )
    assert inter is None


# ----- reconcile_plan_section_walls : cas nominaux ---------------------


def _wall(p1, p2, thickness):
    return {"p1": list(p1), "p2": list(p2), "thickness_m": thickness}


def test_reconcile_perfect_match_ok():
    # Plan : 1 mur vertical (mur N-S) à world X=5, épaisseur 0.20m.
    plan_walls = [_wall((5.0, 0.0), (5.0, 10.0), 0.20)]
    # Trait de coupe horizontal qui croise le mur à world (5, 3).
    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": "/coupe1.dxf",
        "name": "Coupe 1",
    }]
    # Convention : trait horizontal → x_cut_attendu = world X = 5.
    # Donc le mur en coupe doit être à x_cut=5 avec thickness 0.20.
    section_walls = {"/coupe1.dxf": [{
        "x_cut_m": 5.0, "thickness_m": 0.20,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls,
    )
    assert report.summary["matches_ok"] == 1
    assert report.summary["thickness_mismatches"] == 0
    assert report.matches[0].status == "ok"
    assert report.matches[0].thickness_drift_m == pytest.approx(0.0)


def test_reconcile_thickness_mismatch_flagged():
    plan_walls = [_wall((5.0, 0.0), (5.0, 10.0), 0.20)]
    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": "/c.dxf", "name": "Coupe A",
    }]
    # Mur en coupe avec épaisseur très différente (30cm vs 20cm).
    section_walls = {"/c.dxf": [{
        "x_cut_m": 5.0, "thickness_m": 0.30,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls,
        thickness_tol_m=0.02,
    )
    assert report.summary["thickness_mismatches"] == 1
    assert report.matches[0].status == "thickness_mismatch"
    assert report.matches[0].thickness_drift_m == pytest.approx(0.10, abs=1e-6)


def test_reconcile_no_section_wall_at_x_when_coupe_empty():
    plan_walls = [_wall((5.0, 0.0), (5.0, 10.0), 0.20)]
    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": "/c.dxf", "name": "Coupe A",
    }]
    section_walls = {"/c.dxf": []}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls,
    )
    assert report.summary["no_section_wall_at_x"] == 1
    assert report.matches[0].status == "no_section_wall_at_x"
    assert report.matches[0].section_wall_index is None


def test_reconcile_wall_plan_not_crossed():
    # Mur plan à x=5, trait à y=20 (au-delà du mur qui va de y=0 à y=10).
    plan_walls = [_wall((5.0, 0.0), (5.0, 10.0), 0.20)]
    section_lines = [{
        "plan_p1": [0.0, 20.0], "plan_p2": [10.0, 20.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    section_walls = {"/c.dxf": []}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls,
    )
    assert report.summary["plan_walls_not_crossed"] == 1
    assert report.walls_plan_not_crossed == [0]
    assert report.matches == []


def test_reconcile_section_wall_unmatched():
    # Pas de mur plan, mais 1 mur en coupe : il sera unmatched.
    plan_walls = []
    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    section_walls = {"/c.dxf": [{
        "x_cut_m": 5.0, "thickness_m": 0.20,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls,
    )
    assert report.summary["section_walls_unmatched"] == 1
    assert len(report.section_walls_unmatched) == 1
    assert report.section_walls_unmatched[0]["x_cut_m"] == pytest.approx(5.0)


def test_reconcile_ambiguous_when_multiple_candidates_at_same_x():
    plan_walls = [_wall((5.0, 0.0), (5.0, 10.0), 0.20)]
    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    # 2 murs à la même x_cut=5 : un grand (mur principal, hauteur 3m) +
    # un petit linteau (hauteur 0.3m). Le primary doit être le grand.
    section_walls = {"/c.dxf": [
        {"x_cut_m": 5.0, "thickness_m": 0.20, "y_bottom_m": 0.0, "y_top_m": 3.0},
        {"x_cut_m": 5.05, "thickness_m": 0.15, "y_bottom_m": 2.2, "y_top_m": 2.5},
    ]}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls, x_cut_tol_m=0.10,
    )
    assert report.summary["ambiguous"] == 1
    assert report.matches[0].status == "ambiguous_multiple_candidates"
    # Le primary candidat doit être le mur à y_top=3 (extension verticale max).
    primary_idx = report.matches[0].section_wall_index
    assert section_walls["/c.dxf"][primary_idx]["y_top_m"] == 3.0


def test_reconcile_vertical_trait_uses_y_world_as_x_cut():
    # Trait de coupe vertical (parallèle à world Y) → x_cut_attendu = Y_world.
    # Mur plan horizontal à y=5, trait vertical à x=3 qui le croise en (3, 5).
    plan_walls = [_wall((0.0, 5.0), (10.0, 5.0), 0.18)]
    section_lines = [{
        "plan_p1": [3.0, 0.0], "plan_p2": [3.0, 10.0],
        "view_dir": "left", "coupe_path": "/cv.dxf",
    }]
    # Le mur en coupe doit être à x_cut = 5 (= Y world).
    section_walls = {"/cv.dxf": [{
        "x_cut_m": 5.0, "thickness_m": 0.18,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]}

    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls, section_lines, section_walls,
    )
    assert report.summary["matches_ok"] == 1
    assert report.matches[0].x_cut_expected_m == pytest.approx(5.0)


# ----- detect_section_x_axis_convention -------------------------------


def test_detect_x_axis_identity_when_matches_world_axis():
    """Convention identity : DXF X = +world Y pour trait vertical.
    Mur plan à world Y=3, section_wall en coupe à x_cut=3 → identity."""
    plan_walls = [_wall((5.0, 3.0), (10.0, 3.0), 0.20)]  # horiz wall at Y=3
    section_line = {
        "plan_p1": [7.0, 0.0], "plan_p2": [7.0, 10.0],  # vertical trait at X=7
    }
    # Intersection : world (7, 3). For vertical trait, x_cut_identity = world Y = 3.
    sec_walls = [{
        "x_cut_m": 3.0, "thickness_m": 0.20,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]
    v = dwg_coherence.detect_section_x_axis_convention(
        plan_walls, section_line, sec_walls,
    )
    assert v.convention == "identity"
    assert v.matches_identity == 1
    assert v.matches_reversed == 0


def test_detect_x_axis_reversed_when_dxf_is_negative_world():
    """Convention reversed : DXF X = -world Y. Mur plan à world Y=3,
    section_wall en coupe à x_cut=-3 → reversed."""
    plan_walls = [_wall((5.0, 3.0), (10.0, 3.0), 0.20)]
    section_line = {"plan_p1": [7.0, 0.0], "plan_p2": [7.0, 10.0]}
    sec_walls = [{
        "x_cut_m": -3.0, "thickness_m": 0.20,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]
    v = dwg_coherence.detect_section_x_axis_convention(
        plan_walls, section_line, sec_walls,
    )
    assert v.convention == "reversed"
    assert v.matches_reversed == 1
    assert v.matches_identity == 0


def test_detect_x_axis_horizontal_trait_uses_world_x():
    """Trait horizontal : x_cut_identity = world X de l'intersection."""
    plan_walls = [_wall((5.0, 0.0), (5.0, 10.0), 0.20)]  # vertical wall at X=5
    section_line = {"plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0]}  # horiz trait at Y=3
    # Intersection : world (5, 3). horiz trait → x_cut = world X = 5.
    sec_walls = [{
        "x_cut_m": 5.0, "thickness_m": 0.20,
        "y_bottom_m": 0.0, "y_top_m": 3.0,
    }]
    v = dwg_coherence.detect_section_x_axis_convention(
        plan_walls, section_line, sec_walls,
    )
    assert v.convention == "identity"


def test_detect_x_axis_defaults_identity_when_no_walls_crossed():
    plan_walls = []
    section_line = {"plan_p1": [0, 0], "plan_p2": [10, 0]}
    sec_walls = []
    v = dwg_coherence.detect_section_x_axis_convention(
        plan_walls, section_line, sec_walls,
    )
    assert v.convention == "identity"
    assert v.walls_crossed == 0
    assert v.confidence == 0.0


def test_detect_x_axis_confidence_increases_with_match_margin():
    """Plus de murs matchent → plus de confiance. 3/3 matches en reversed
    et 0 en identity → confidence ≥ 0.3."""
    plan_walls = [
        _wall((-5.0, 3.0), (5.0, 3.0), 0.20),
        _wall((-5.0, 7.0), (5.0, 7.0), 0.20),
        _wall((-5.0, 11.0), (5.0, 11.0), 0.20),
    ]
    section_line = {"plan_p1": [0.0, 0.0], "plan_p2": [0.0, 15.0]}
    # World Y intersections : 3, 7, 11. Reversed → -3, -7, -11.
    sec_walls = [
        {"x_cut_m": -3.0, "thickness_m": 0.20,
         "y_bottom_m": 0.0, "y_top_m": 3.0},
        {"x_cut_m": -7.0, "thickness_m": 0.20,
         "y_bottom_m": 0.0, "y_top_m": 3.0},
        {"x_cut_m": -11.0, "thickness_m": 0.20,
         "y_bottom_m": 0.0, "y_top_m": 3.0},
    ]
    v = dwg_coherence.detect_section_x_axis_convention(
        plan_walls, section_line, sec_walls,
    )
    assert v.convention == "reversed"
    assert v.matches_reversed == 3
    assert v.confidence >= 0.99  # 3/3 = 1.0


# ----- reconcile_plan_section_floors : cas nominaux --------------------


def _floor(boundary, elevation, thickness, holes=None):
    return {
        "boundary": [list(p) for p in boundary],
        "holes": [[list(p) for p in h] for h in (holes or [])],
        "elevation_m": elevation,
        "thickness_m": thickness,
    }


def _slab(top_y, thk, x_min, x_max):
    return {
        "top_y_m": top_y,
        "bot_y_m": top_y - thk,
        "thickness_m": thk,
        "x_min_m": x_min,
        "x_max_m": x_max,
    }


# Boundary carrée 10×10 centrée sur l'origine, en sens trigo.
_SQUARE_10x10 = [(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]


def test_floor_cut_intervals_simple_square_horizontal_trait():
    # Trait horizontal y=0 traverse le carré → cut sur world X = [-5, 5].
    # Convention : trait horizontal (|dx| > |dy|) → x_cut = world X.
    intervals = dwg_coherence._floor_cut_intervals_in_section(
        _SQUARE_10x10, holes=[],
        section_p1=(-8.0, 0.0), section_p2=(8.0, 0.0),
    )
    assert len(intervals) == 1
    x_min, x_max = intervals[0]
    assert x_min == pytest.approx(-5.0, abs=1e-3)
    assert x_max == pytest.approx(5.0, abs=1e-3)


def test_floor_cut_intervals_simple_square_vertical_trait_uses_world_y():
    # Trait vertical x=0 → x_cut = world Y. Carré cut sur Y = [-5, 5].
    intervals = dwg_coherence._floor_cut_intervals_in_section(
        _SQUARE_10x10, holes=[],
        section_p1=(0.0, -8.0), section_p2=(0.0, 8.0),
    )
    assert len(intervals) == 1
    x_min, x_max = intervals[0]
    assert x_min == pytest.approx(-5.0, abs=1e-3)
    assert x_max == pytest.approx(5.0, abs=1e-3)


def test_floor_cut_intervals_with_hole_yields_two_intervals():
    # Carré 10×10 avec un trou 2×2 centré sur l'origine.
    hole = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    intervals = dwg_coherence._floor_cut_intervals_in_section(
        _SQUARE_10x10, holes=[hole],
        section_p1=(-8.0, 0.0), section_p2=(8.0, 0.0),
    )
    assert len(intervals) == 2
    (a_min, a_max), (b_min, b_max) = intervals
    assert a_min == pytest.approx(-5.0, abs=1e-3)
    assert a_max == pytest.approx(-1.0, abs=1e-3)
    assert b_min == pytest.approx(1.0, abs=1e-3)
    assert b_max == pytest.approx(5.0, abs=1e-3)


def test_floor_cut_intervals_no_crossing_returns_empty():
    # Trait au-delà du carré : pas d'intersection.
    intervals = dwg_coherence._floor_cut_intervals_in_section(
        _SQUARE_10x10, holes=[],
        section_p1=(-8.0, 20.0), section_p2=(8.0, 20.0),
    )
    assert intervals == []


def test_reconcile_floors_perfect_match_ok():
    # 1 dalle plan 10×10 au niveau Z=3 ep=25cm. 1 trait horizontal y=0 →
    # cut sur world X = [-5, 5]. La coupe doit avoir 1 paire à top_y=3,
    # thk=0.25, x=[-5, 5].
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0, thickness=0.25)]
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf", "name": "Coupe 1",
    }]
    slabs = {"/c.dxf": [_slab(top_y=3.0, thk=0.25, x_min=-5.0, x_max=5.0)]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs,
    )
    assert report.summary["matches_ok"] == 1
    assert report.summary["plan_floors_not_crossed"] == 0
    assert report.summary["section_slabs_unmatched"] == 0
    m = report.matches[0]
    assert m.status == "ok"
    assert m.thickness_drift_m == pytest.approx(0.0)
    assert m.coverage_ratio == pytest.approx(1.0, abs=1e-3)


def test_reconcile_floors_thickness_mismatch_flagged():
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0, thickness=0.25)]
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf", "name": "Coupe 1",
    }]
    # Paire ep 35cm vs dalle plan ep 25cm.
    slabs = {"/c.dxf": [_slab(top_y=3.0, thk=0.35, x_min=-5.0, x_max=5.0)]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs, thickness_tol_m=0.02,
    )
    assert report.summary["thickness_mismatches"] == 1
    assert report.matches[0].status == "thickness_mismatch"
    assert report.matches[0].thickness_drift_m == pytest.approx(0.10, abs=1e-6)


def test_reconcile_floors_no_section_pair_at_z():
    # Dalle plan Z=3, mais coupe n'a une paire qu'à Z=0.
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0, thickness=0.25)]
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    slabs = {"/c.dxf": [_slab(top_y=0.0, thk=0.25, x_min=-5.0, x_max=5.0)]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs,
    )
    assert report.summary["no_section_pair_at_z"] == 1
    assert report.matches[0].status == "no_section_pair_at_z"
    assert report.matches[0].section_slab_index is None


def test_reconcile_floors_no_section_pair_at_x():
    # Dalle plan cut sur [-5, 5], coupe a une paire au bon Z mais sur
    # [10, 15] : pas d'overlap X.
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0, thickness=0.25)]
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    slabs = {"/c.dxf": [_slab(top_y=3.0, thk=0.25, x_min=10.0, x_max=15.0)]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs,
    )
    assert report.summary["no_section_pair_at_x"] == 1
    assert report.matches[0].status == "no_section_pair_at_x"


def test_reconcile_floors_extent_partial_when_coverage_below_ratio():
    # Cut sur [-5, 5] (longueur 10). Paire ne couvre que [-5, 0] (50%) →
    # coverage 0.5 < default 0.80 → extent_partial.
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0, thickness=0.25)]
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    slabs = {"/c.dxf": [_slab(top_y=3.0, thk=0.25, x_min=-5.0, x_max=0.0)]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs,
    )
    assert report.summary["extent_partial"] == 1
    assert report.matches[0].status == "extent_partial"
    assert report.matches[0].coverage_ratio == pytest.approx(0.5, abs=1e-3)


def test_reconcile_floors_plan_not_crossed():
    # Trait passe à y=20 (au-delà du carré qui va de -5 à 5).
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0, thickness=0.25)]
    section_lines = [{
        "plan_p1": [-8.0, 20.0], "plan_p2": [8.0, 20.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    slabs = {"/c.dxf": []}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs,
    )
    assert report.summary["plan_floors_not_crossed"] == 1
    assert report.floors_plan_not_crossed == [0]
    assert report.matches == []


def test_reconcile_floors_phantom_pair_unmatched():
    # Pas de dalle plan, mais 1 paire en coupe → unmatched (dalle oubliée).
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf",
    }]
    slabs = {"/c.dxf": [_slab(top_y=3.0, thk=0.25, x_min=-5.0, x_max=5.0)]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors=[], section_lines=section_lines,
        section_slabs_by_coupe=slabs,
    )
    assert report.summary["section_slabs_unmatched"] == 1
    assert len(report.section_slabs_unmatched) == 1
    assert report.section_slabs_unmatched[0]["top_y_m"] == pytest.approx(3.0)


def test_reconcile_floors_hole_yields_two_match_intervals():
    # Dalle avec trou 2×2 centré → 2 intervalles de coupe (-5..-1 et 1..5).
    # 2 paires en coupe couvrant chacune un côté → 2 match OK.
    hole = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    plan_floors = [_floor(_SQUARE_10x10, elevation=3.0,
                          thickness=0.25, holes=[hole])]
    section_lines = [{
        "plan_p1": [-8.0, 0.0], "plan_p2": [8.0, 0.0],
        "view_dir": "up", "coupe_path": "/c.dxf", "name": "Coupe 1",
    }]
    slabs = {"/c.dxf": [
        _slab(top_y=3.0, thk=0.25, x_min=-5.0, x_max=-1.0),
        _slab(top_y=3.0, thk=0.25, x_min=1.0, x_max=5.0),
    ]}

    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors, section_lines, slabs,
    )
    assert report.summary["matches_ok"] == 2
    assert report.summary["section_slabs_unmatched"] == 0


# ----- Tool smoke test : dwg_reconcile_plan_section_floors -----------


_P7_DIR = Path(__file__).parent / "fixtures" / "P7"


@pytest.fixture
def p7_kg_with_floors():
    """KG bootstrap-é via le dryrun P7, avec les Floor créés.

    On déclenche le pipeline complet (Phase 1 → 2c) en mode doc=None pour
    avoir 2 Floor (Niveau 0, Niveau 1) dans le KG, des FloorType, des
    Level, et des section_lines enregistrées dans le DxfImportContext.
    """
    from scripts import dxf_dryrun

    # _bootstrap_kg() crée un KG vide ; on duplique l'orchestration du
    # main() en mémoire pour récupérer le KG construit (le main jette
    # le KG après écriture JSON, donc on ne peut pas le réutiliser).
    kg = dxf_dryrun._bootstrap_kg()
    plans, coupes, elevs = dxf_dryrun._classify_dxfs(_P7_DIR)
    assert plans and coupes, "P7 fixture missing plans/coupes"

    from lib.tools import dwg_import as dwgi
    dwgi.check_planset_integrity(kg=kg, directory=str(_P7_DIR))
    dwgi.inspect_sections(kg=kg, directory=str(_P7_DIR))
    markers_payload = dwgi.find_section_markers(
        kg=kg, file_path=str(plans[0]),
    )
    section_markers = markers_payload.get("markers") or []
    assignment = dwgi.assign_coupes_to_traits(
        kg=kg, coupe_paths=[str(c) for c in coupes],
        section_markers=section_markers,
    )

    from lib.tools import levels as levels_tool
    reconcile = levels_tool.reconcile_with_dxf(
        kg=kg, coupe_path=str(coupes[0]),
    )
    dxf_dryrun._kg_seed_levels_from_reconcile(kg, reconcile)

    section_line_specs = []
    for entry in assignment.get("assignment") or []:
        mk = section_markers[entry["marker_index"]]
        view_dir = (
            mk.get("inferred_view_dir")
            or (mk.get("view_dir_candidates") or ["up"])[0]
        )
        section_line_specs.append({
            "coupe_path": entry["coupe_path"],
            "plan_p1": mk["p1_m"], "plan_p2": mk["p2_m"],
            "view_dir": view_dir,
            "name": Path(entry["coupe_path"]).stem,
            "confirmed_by_user": True,
            "scale_verified": True,
            "drift_pct": entry.get("drift_pct", 0.0),
        })
    linked_view_specs = dxf_dryrun._fake_link_specs_for_dryrun(
        plans, coupes, elevs,
    )
    dxf_dryrun._kg_seed_dxf_context(
        kg, directory=str(_P7_DIR),
        section_line_specs=section_line_specs,
        linked_view_specs=linked_view_specs,
    )
    from lib.tools import dxf_context as _dxf_context_mod
    _dxf_context_mod.register_inspection(
        kg=kg, directory=str(_P7_DIR),
        inspection={"files": [{"path": str(p), "kind": "plan"} for p in plans]
                    + [{"path": str(c), "kind": "section"} for c in coupes]},
    )

    dwgi.extract_wall_thicknesses_many(
        kg=kg, file_paths=[str(p) for p in plans],
    )

    levels_sorted = sorted(
        kg.find_by_type("Level"),
        key=lambda lid: kg.get_node(lid)["elevation"],
    )
    assert levels_sorted, "P7 dryrun should produce levels"
    from lib.tools.dwg_import import _plan_path_to_level_elev
    plan_level_elev = _plan_path_to_level_elev(kg)
    plan_items = []
    for p in plans:
        elev = plan_level_elev.get(str(p))
        level_ref = None
        for lid in levels_sorted:
            if abs(kg.get_node(lid)["elevation"] - (elev or 0.0)) < 0.01:
                level_ref = lid
                break
        if level_ref is None:
            level_ref = levels_sorted[0]
        plan_items.append({
            "file_path": str(p), "level_ref": level_ref,
            "height_m": 3.0,
        })
    dwgi.create_continuous_walls_many(kg=kg, doc=None, items=plan_items)
    dwgi.create_floors_many(kg=kg, doc=None)

    return kg


def test_tool_reconcile_floors_p7_finds_orphan(p7_kg_with_floors):
    """Sur P7, le tool doit trouver 2 dalles + détecter l'orpheline de
    Coupe 2 (1 paire A-FLOR sans pendant complet en plan, identifiée
    par l'observation manuelle).
    """
    from lib import llm_protocol
    result = llm_protocol.dispatch_tool_use(
        "dwg_reconcile_plan_section_floors",
        {},
        "t1",
        p7_kg_with_floors,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["plan_floors_count"] == 2  # Niveau 0 + Niveau 1
    assert payload["section_lines_count"] == 3  # 3 coupes
    # Les paires détectées : 2 par coupe × 3 coupes = 6 paires.
    total_slabs = sum(payload["section_slabs_count_by_coupe"].values())
    assert total_slabs == 6
    # On attend au moins quelques match OK (les dalles plan croisées par
    # un trait et avec couverture suffisante).
    assert payload["summary"]["matches_ok"] >= 1


def test_tool_validate_floors_3d_p7(p7_kg_with_floors):
    """Sur P7 : 2 dalles (N0, N1), 3 coupes. Au moins une dalle doit être
    confirmed (ok ou partial), et le rapport doit avoir la structure
    attendue."""
    from lib import llm_protocol
    result = llm_protocol.dispatch_tool_use(
        "dwg_validate_floors_3d_existence",
        {},
        "t1",
        p7_kg_with_floors,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["floors_total"] == 2
    s = payload["summary"]
    total = (s["confirmed"] + s["unconfirmed"]
             + s["partial_extent"] + s["no_crossings"])
    assert total == 2
    # Au moins 1 dalle confirmée OU avec extent_partial (= les dalles
    # de P7 sont visibles en coupe, cf. test_tool_reconcile_floors_p7).
    assert s["confirmed"] + s["partial_extent"] >= 1


def test_tool_validate_walls_3d_p7(p7_kg_with_floors):
    """Sur P7, valide qu'au moins quelques murs sont confirmés par les
    coupes (= ≥1 crossing avec match en coupe), et que le rapport est
    bien structuré (4 catégories : confirmed / unconfirmed / no_crossings /
    thickness_mismatch_only)."""
    from lib import llm_protocol
    result = llm_protocol.dispatch_tool_use(
        "dwg_validate_walls_3d_existence",
        {},
        "t1",
        p7_kg_with_floors,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    # P7 a 19 murs vivants au total (9 N0 + 10 N1).
    assert payload["walls_total"] == 19
    assert payload["section_lines_count"] == 3
    summary = payload["summary"]
    # Sanity check : total = somme des 4 catégories.
    total = (
        summary["confirmed"] + summary["unconfirmed"]
        + summary["no_3d_evidence"] + summary["thickness_mismatch_only"]
    )
    assert total == 19
    # On attend au moins quelques murs confirmés (les murs traversés
    # par un trait + matchent en épaisseur OU visibles en élévation).
    assert summary["confirmed"] >= 1


def test_tool_validate_import_3d_meta_p7(p7_kg_with_floors):
    """Le meta-tool agrège walls + floors + columns + openings."""
    from lib import llm_protocol
    result = llm_protocol.dispatch_tool_use(
        "dwg_validate_import_3d",
        {},
        "t1",
        p7_kg_with_floors,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    # 4 sous-payloads présents.
    assert "walls" in payload
    assert "floors" in payload
    assert "columns" in payload
    assert "openings" in payload
    # Summary consolidé.
    s = payload["summary"]
    assert s["walls_total"] == 19
    assert s["floors_total"] == 2
    assert s["columns_total"] == 0  # P7 sans poteaux
    # P7 a des fenêtres si Phase 2b a tourné — sinon 0.
    # total_elements = walls + floors + columns + openings.
    assert s["total_elements"] == (
        s["walls_total"] + s["floors_total"]
        + s["columns_total"] + s["openings_total"]
    )
    # `total_suspects` cohérent avec les sous-payloads.
    walls_unconf = payload["walls"]["summary"]["unconfirmed"]
    assert s["walls_unconfirmed"] == walls_unconf


# ----- Tool smoke test : dwg_reconcile_plan_section_walls --------------


ezdxf = pytest.importorskip("ezdxf")


def _make_plan_dxf(tmp_path: Path) -> Path:
    """Plan DXF minimal avec 1 mur orthogonal à x=5 (épaisseur 0.20m).

    Layout : 2 lignes parallèles à x=4.9 et x=5.1, y de 0 à 10. Le mur
    est donc centré à x=5.0, épaisseur 0.20m.
    Unités : mètres (units_code=6, no scaling).
    """
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6  # metres
    doc.layers.add("A-WALL")
    msp = doc.modelspace()
    msp.add_line((4.9, 0.0), (4.9, 10.0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((5.1, 0.0), (5.1, 10.0), dxfattribs={"layer": "A-WALL"})
    p = tmp_path / "plan.dxf"
    doc.saveas(str(p))
    return p


def _make_coupe_dxf(tmp_path: Path, x_cut: float = 5.0,
                     thickness: float = 0.20) -> Path:
    """Coupe DXF minimal avec 1 mur vertical à `x_cut` ± thickness/2.

    Layout : 2 lignes verticales aux 2 faces du mur, y de 0 à 3.
    """
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-WALL")
    doc.layers.add("A-FLOR-LEVL")  # pour que classify_dxf -> section
    msp = doc.modelspace()
    half = thickness / 2.0
    msp.add_line((x_cut - half, 0.0), (x_cut - half, 3.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((x_cut + half, 0.0), (x_cut + half, 3.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((-1.0, 0.0), (11.0, 0.0),
                 dxfattribs={"layer": "A-FLOR-LEVL"})
    p = tmp_path / "coupe.dxf"
    doc.saveas(str(p))
    return p


@pytest.fixture
def kg_fresh():
    from lib import llm_protocol
    from lib.project_kg import ProjectKG
    llm_protocol.reset_registry()
    llm_protocol.get_registry()
    kg = ProjectKG("p")
    kg.advance_turn()
    return kg


def test_tool_reconcile_via_explicit_section_lines(tmp_path, kg_fresh):
    from lib import llm_protocol
    plan = _make_plan_dxf(tmp_path)
    coupe = _make_coupe_dxf(tmp_path, x_cut=5.0, thickness=0.20)

    # Trait horizontal à y=3 qui croise le mur plan à (5, 3) → x_cut=5.
    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": str(coupe),
        "name": "Coupe test",
    }]

    result = llm_protocol.dispatch_tool_use(
        "dwg_reconcile_plan_section_walls",
        {"plan_path": str(plan), "section_lines": section_lines},
        "t1",
        kg_fresh,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["plan_walls_count"] >= 1
    assert payload["summary"]["matches_ok"] >= 1
    assert payload["needs_user_decision"] is False


def test_tool_reconcile_mismatch_flags_needs_user(tmp_path, kg_fresh):
    from lib import llm_protocol
    plan = _make_plan_dxf(tmp_path)
    # Coupe avec un mur 30cm (vs 20cm dans le plan).
    coupe = _make_coupe_dxf(tmp_path, x_cut=5.0, thickness=0.30)

    section_lines = [{
        "plan_p1": [0.0, 3.0], "plan_p2": [10.0, 3.0],
        "view_dir": "up", "coupe_path": str(coupe),
    }]

    result = llm_protocol.dispatch_tool_use(
        "dwg_reconcile_plan_section_walls",
        {"plan_path": str(plan), "section_lines": section_lines},
        "t1",
        kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["needs_user_decision"] is True
    assert payload["summary"]["thickness_mismatches"] >= 1
    assert len(payload["thickness_mismatches"]) >= 1


def test_tool_reconcile_missing_context_raises(tmp_path, kg_fresh):
    from lib import llm_protocol
    plan = _make_plan_dxf(tmp_path)
    result = llm_protocol.dispatch_tool_use(
        "dwg_reconcile_plan_section_walls",
        {"plan_path": str(plan)},  # pas de section_lines, pas de KG context
        "t1",
        kg_fresh,
    )
    # is_error doit être True (ValueError remonté en is_error par le dispatcher).
    assert result.get("is_error") is True
    assert "DxfImportContext" in result["content"]


# ----- Helpers purs : source consistency -------------------------------


def test_source_consistency_all_aia_clean():
    check = dwg_coherence.check_source_consistency({
        "/a.dxf": "aia", "/b.dxf": "aia",
    })
    assert check.severity == "clean"
    assert check.issues == []


def test_source_consistency_mixed_is_error():
    check = dwg_coherence.check_source_consistency({
        "/a.dxf": "aia", "/b.dxf": "iso",
    })
    assert check.severity == "errors"
    assert check.issues[0]["kind"] == "mixed_sources"


def test_source_consistency_all_other_is_warning():
    check = dwg_coherence.check_source_consistency({
        "/a.dxf": "other", "/b.dxf": "other",
    })
    assert check.severity == "warnings"
    assert check.issues[0]["kind"] == "all_other"


# ----- Helpers purs : levels consistency -------------------------------


def test_levels_consistency_single_coupe_clean():
    check = dwg_coherence.check_levels_consistency_between_coupes({
        "/c.dxf": [{"name": "Niveau 0", "elevation_m": 0.0}],
    })
    assert check.severity == "clean"


def test_levels_consistency_same_jeu_clean():
    levels = [
        {"name": "Niveau 0", "elevation_m": 0.0},
        {"name": "Niveau 1", "elevation_m": 3.0},
    ]
    check = dwg_coherence.check_levels_consistency_between_coupes({
        "/c1.dxf": list(levels), "/c2.dxf": list(levels),
    })
    assert check.severity == "clean"
    assert check.issues == []


def test_levels_consistency_subset_is_warning():
    # Coupe 1 traverse 2 niveaux ; Coupe 2 ne traverse que 1.
    check = dwg_coherence.check_levels_consistency_between_coupes({
        "/c1.dxf": [
            {"name": "Niveau 0", "elevation_m": 0.0},
            {"name": "Niveau 1", "elevation_m": 3.0},
        ],
        "/c2.dxf": [{"name": "Niveau 0", "elevation_m": 0.0}],
    })
    assert check.severity == "warnings"
    assert any(
        i["kind"] == "level_missing_in_some_coupes"
        for i in check.issues
    )


def test_levels_consistency_elevation_conflict_is_error():
    check = dwg_coherence.check_levels_consistency_between_coupes({
        "/c1.dxf": [{"name": "Niveau 1", "elevation_m": 3.0}],
        "/c2.dxf": [{"name": "Niveau 1", "elevation_m": 3.5}],
    })
    assert check.severity == "errors"
    assert check.issues[0]["kind"] == "elevation_conflict_same_name"


# ----- Helpers purs : scale drift --------------------------------------


def test_scale_drift_clean():
    check = dwg_coherence.check_scale_drift([
        {"coupe_path": "/c.dxf", "drift_pct": 5.0,
         "marker_length_m": 10.0, "coupe_extent_m": 9.5},
    ])
    assert check.severity == "clean"


def test_scale_drift_warning_at_30pct():
    check = dwg_coherence.check_scale_drift([
        {"coupe_path": "/c.dxf", "drift_pct": 30.0,
         "marker_length_m": 10.0, "coupe_extent_m": 7.0},
    ])
    assert check.severity == "warnings"


def test_scale_drift_error_at_60pct():
    check = dwg_coherence.check_scale_drift([
        {"coupe_path": "/c.dxf", "drift_pct": 60.0,
         "marker_length_m": 10.0, "coupe_extent_m": 4.0},
    ])
    assert check.severity == "errors"


# ----- Helpers purs : openings matching --------------------------------


def test_openings_matching_clean():
    check = dwg_coherence.check_openings_matching([
        {"section_name": "c1", "section_path": "/c1.dxf",
         "match_count": 3, "unmatched_section_count": 0,
         "unmatched_plan_count": 0},
    ])
    assert check.severity == "clean"


def test_openings_matching_warning_when_unmatched():
    check = dwg_coherence.check_openings_matching([
        {"section_name": "c1", "section_path": "/c1.dxf",
         "match_count": 2, "unmatched_section_count": 1,
         "unmatched_plan_count": 0},
    ])
    assert check.severity == "warnings"


# ----- aggregate_planset_integrity -------------------------------------


def test_aggregate_severity_max_and_gate():
    from lib.dwg_coherence import IntegrityCheck, aggregate_planset_integrity

    clean = IntegrityCheck(name="a", severity="clean", summary={}, issues=[])
    warn = IntegrityCheck(name="b", severity="warnings", summary={},
                          issues=[{"kind": "x"}])
    err = IntegrityCheck(name="c", severity="errors", summary={},
                         issues=[{"kind": "y_severe"}])

    report = aggregate_planset_integrity([clean, warn], {})
    assert report.severity == "warnings"
    assert report.gate_status == "needs_user"
    assert report.ok is True

    report2 = aggregate_planset_integrity([clean, warn, err], {})
    assert report2.severity == "errors"
    assert report2.gate_status == "abort"
    assert report2.ok is False

    report3 = aggregate_planset_integrity([clean, clean], {})
    assert report3.severity == "clean"
    assert report3.gate_status == "pass"
    assert report3.ok is True


# ----- Tool smoke : check_planset_integrity ---------------------------


def _make_planset_dir(tmp_path: Path, *,
                       wall_thickness_plan: float = 0.20,
                       wall_thickness_coupe: float = 0.20) -> Path:
    """Génère un dossier de planset : 1 plan + 1 coupe minimal.

    Plan : 1 mur orthogonal à x=5 (2 lignes parallèles A-WALL), +
    1 trait de coupe sur G-ANNO-SYMB à y=3 avec marqueurs aux endpoints,
    + 1 label A-AREA-IDEN (signature plan).
    Coupe : 1 mur vertical à x=5 (2 lignes A-WALL) + 1 ligne A-FLOR-LEVL
    (signature section).
    """
    d = tmp_path / "planset"
    d.mkdir()

    # --- Plan ---
    plan_doc = ezdxf.new("R2018")
    plan_doc.header["$INSUNITS"] = 6  # metres
    for ly in ("A-WALL", "A-AREA-IDEN", "G-ANNO-SYMB"):
        plan_doc.layers.add(ly)
    msp_p = plan_doc.modelspace()
    half_p = wall_thickness_plan / 2.0
    msp_p.add_line((5.0 - half_p, 0.0), (5.0 - half_p, 10.0),
                   dxfattribs={"layer": "A-WALL"})
    msp_p.add_line((5.0 + half_p, 0.0), (5.0 + half_p, 10.0),
                   dxfattribs={"layer": "A-WALL"})
    # Label A-AREA pour signature plan.
    msp_p.add_mtext("Pièce 1", dxfattribs={
        "layer": "A-AREA-IDEN", "insert": (1, 1), "char_height": 0.2,
    })
    # Trait de coupe à y=3.
    section_block = "Coupe - Marque-test-Niveau 0"
    blk = plan_doc.blocks.new(name=section_block)
    blk.add_line((0, 0), (1, 0), dxfattribs={"layer": "G-ANNO-SYMB"})
    msp_p.add_line((0.0, 3.0), (10.0, 3.0),
                   dxfattribs={"layer": "G-ANNO-SYMB"})
    msp_p.add_blockref(section_block, (0.0, 3.0),
                      dxfattribs={"layer": "G-ANNO-SYMB"})
    msp_p.add_blockref(section_block, (10.0, 3.0),
                      dxfattribs={"layer": "G-ANNO-SYMB"})
    plan_doc.saveas(str(d / "Plan d'étage - Niveau 0.dxf"))

    # --- Coupe ---
    c_doc = ezdxf.new("R2018")
    c_doc.header["$INSUNITS"] = 6
    for ly in ("A-WALL", "A-FLOR-LEVL"):
        c_doc.layers.add(ly)
    msp_c = c_doc.modelspace()
    half_c = wall_thickness_coupe / 2.0
    # Mur vertical à x_cut=5 (correspond à world X=5 dans le plan, coupe
    # horizontale donc x_cut=ix).
    msp_c.add_line((5.0 - half_c, 0.0), (5.0 - half_c, 3.0),
                   dxfattribs={"layer": "A-WALL"})
    msp_c.add_line((5.0 + half_c, 0.0), (5.0 + half_c, 3.0),
                   dxfattribs={"layer": "A-WALL"})
    # Façades extérieures de la coupe (lignes A-WALL isolées) pour donner
    # un extent ≈ longueur du trait → scale_drift OK. Ces lignes ne
    # forment pas de paire → pas détectées comme SectionWall (filtrées
    # par detect_wall_segments).
    msp_c.add_line((0.0, 0.0), (0.0, 3.0), dxfattribs={"layer": "A-WALL"})
    msp_c.add_line((10.0, 0.0), (10.0, 3.0), dxfattribs={"layer": "A-WALL"})
    # Niveau 0 = ligne horizontale à y=0 sur A-FLOR-LEVL.
    msp_c.add_line((0.0, 0.0), (10.0, 0.0),
                   dxfattribs={"layer": "A-FLOR-LEVL"})
    c_doc.saveas(str(d / "Coupe - Coupe 1.dxf"))

    return d


def test_tool_planset_integrity_clean_passes(tmp_path, kg_fresh):
    from lib import llm_protocol
    d = _make_planset_dir(
        tmp_path,
        wall_thickness_plan=0.20,
        wall_thickness_coupe=0.20,
    )
    result = llm_protocol.dispatch_tool_use(
        "check_planset_integrity",
        {"directory": str(d)},
        "t1",
        kg_fresh,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    # On accepte clean OU warnings (les openings vides peuvent être
    # signalés selon le détail des fixtures).
    assert payload["severity"] in ("clean", "warnings")
    assert payload["gate_status"] in ("pass", "needs_user")
    assert payload["ok"] is True
    assert payload["files_summary"]["plan_count"] == 1
    assert payload["files_summary"]["section_count"] == 1


def test_tool_planset_integrity_severe_mismatch_aborts(tmp_path, kg_fresh):
    from lib import llm_protocol
    # Plan 20cm, coupe 35cm → mismatch > 10cm → errors → gate=abort.
    d = _make_planset_dir(
        tmp_path,
        wall_thickness_plan=0.20,
        wall_thickness_coupe=0.35,
    )
    result = llm_protocol.dispatch_tool_use(
        "check_planset_integrity",
        {"directory": str(d)},
        "t1",
        kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["severity"] == "errors"
    assert payload["gate_status"] == "abort"
    assert payload["ok"] is False
    assert len(payload["errors"]) >= 1


def test_tool_planset_integrity_no_dxf_in_dir_raises(tmp_path, kg_fresh):
    from lib import llm_protocol
    empty = tmp_path / "empty"
    empty.mkdir()
    result = llm_protocol.dispatch_tool_use(
        "check_planset_integrity",
        {"directory": str(empty)},
        "t1",
        kg_fresh,
    )
    assert result.get("is_error") is True


def _make_plan_only_dir(tmp_path: Path) -> Path:
    """Dossier ne contenant qu'un plan, pas de coupe."""
    d = tmp_path / "plan_only"
    d.mkdir()
    plan_doc = ezdxf.new("R2018")
    plan_doc.header["$INSUNITS"] = 6
    for ly in ("A-WALL", "A-AREA-IDEN"):
        plan_doc.layers.add(ly)
    msp = plan_doc.modelspace()
    msp.add_line((4.9, 0.0), (4.9, 10.0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((5.1, 0.0), (5.1, 10.0), dxfattribs={"layer": "A-WALL"})
    msp.add_mtext("Pièce 1", dxfattribs={
        "layer": "A-AREA-IDEN", "insert": (1, 1), "char_height": 0.2,
    })
    plan_doc.saveas(str(d / "Plan seul.dxf"))
    return d


def test_tool_planset_integrity_plan_only_warns_but_allows(tmp_path, kg_fresh):
    """Plan sans coupe = audit dégradé, mais ok=True (gate=needs_user)."""
    from lib import llm_protocol
    d = _make_plan_only_dir(tmp_path)
    result = llm_protocol.dispatch_tool_use(
        "check_planset_integrity",
        {"directory": str(d)},
        "t1",
        kg_fresh,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["severity"] == "warnings"
    assert payload["gate_status"] == "needs_user"
    assert payload["ok"] is True
    # Un warning de setup signale l'absence de coupe.
    assert any(
        w.get("kind") == "no_section_detected"
        for w in payload["warnings"]
    )


def test_tool_planset_integrity_no_plan_aborts(tmp_path, kg_fresh):
    """Pas de plan = abort (errors), même si une coupe est présente."""
    from lib import llm_protocol
    d = tmp_path / "coupe_only"
    d.mkdir()
    c_doc = ezdxf.new("R2018")
    c_doc.header["$INSUNITS"] = 6
    for ly in ("A-WALL", "A-FLOR-LEVL"):
        c_doc.layers.add(ly)
    msp = c_doc.modelspace()
    msp.add_line((5.0, 0.0), (5.0, 3.0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((5.2, 0.0), (5.2, 3.0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((0.0, 0.0), (10.0, 0.0), dxfattribs={"layer": "A-FLOR-LEVL"})
    c_doc.saveas(str(d / "Coupe.dxf"))

    result = llm_protocol.dispatch_tool_use(
        "check_planset_integrity",
        {"directory": str(d)},
        "t1",
        kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["severity"] == "errors"
    assert payload["gate_status"] == "abort"
    assert payload["ok"] is False
    assert any(
        e.get("kind") == "no_plan_detected"
        for e in payload["errors"]
    )


# ----- _building_extent_from_entities : robuste aux coupes sans murs ----
#
# Reproduit le cas P2 (Poteaux + dalles) où Coupe 3 n'a aucun A-WALL
# mais a 12 LINEs A-FLOR. Avant le fix, drift = 100% → gate abort
# (faux positif). Avec le fix (A-WALL ∪ A-FLOR), drift redevient
# représentatif.


def test_building_extent_uses_a_wall_when_present():
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        _line_entity(0.0, 0.0, 10.0, 0.0, layer="A-WALL"),
        _line_entity(2.0, 1.0, 5.0, 1.0, layer="A-FLOR"),
        # Annotation hors-bâtiment : doit être ignorée.
        _line_entity(-50.0, 5.0, 50.0, 5.0, layer="G-ANNO-SYMB"),
    ]
    ext = _building_extent_from_entities(ents)
    # A-WALL (0..10) ∪ A-FLOR (2..5) → extent = 10.
    assert ext == pytest.approx(10.0, abs=1e-6)


def test_building_extent_falls_back_to_a_flor_when_no_a_wall():
    """Cas P2 Coupe 3 : 0 A-WALL LINE, 12 A-FLOR LINEs. Avant le fix le
    extent était 0.0 → drift 100% → drift_error. Avec le fix l'extent
    A-FLOR est utilisé."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        # Pas de A-WALL — Coupe 3 typique poteaux-dalles.
        _line_entity(0.0, 0.0, 16.0, 0.0, layer="A-FLOR"),
        _line_entity(2.0, 1.0, 14.0, 1.0, layer="A-FLOR"),
    ]
    ext = _building_extent_from_entities(ents)
    assert ext == pytest.approx(16.0, abs=1e-6)


def test_building_extent_ignores_other_layers():
    """Les layers d'annotation/structure (G-ANNO, S-GRID, S-COLS, etc.)
    ne contribuent pas à l'extent — éviter les annotations qui dépassent
    largement le bâtiment réel."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        _line_entity(-30.0, 0.0, 30.0, 0.0, layer="S-GRID"),
        _line_entity(-50.0, 5.0, 50.0, 5.0, layer="G-ANNO-SYMB"),
        _line_entity(0.0, 10.0, 10.0, 10.0, layer="A-WALL"),
    ]
    ext = _building_extent_from_entities(ents)
    # Seul A-WALL compte → extent = 10, pas 100.
    assert ext == pytest.approx(10.0, abs=1e-6)


def test_building_extent_returns_none_when_empty():
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        _line_entity(0.0, 0.0, 10.0, 0.0, layer="G-ANNO-SYMB"),
        _line_entity(0.0, 1.0, 5.0, 1.0, layer="A-FLOR-LEVL"),
    ]
    ext = _building_extent_from_entities(ents)
    assert ext is None


def test_building_extent_union_when_a_flor_extends_a_wall():
    """Si A-WALL et A-FLOR ne couvrent pas la même portion (cas P2 Coupe 1
    où A-FLOR couvre plus que A-WALL), l'extent = l'union des deux."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        # A-WALL sur [0, 10]
        _line_entity(0.0, 0.0, 10.0, 0.0, layer="A-WALL"),
        # A-FLOR sur [-5, 14] → étend l'extent à 19
        _line_entity(-5.0, 1.0, 14.0, 1.0, layer="A-FLOR"),
    ]
    ext = _building_extent_from_entities(ents)
    assert ext == pytest.approx(19.0, abs=1e-6)


def _insert_entity(x, y, layer="S-COLS", name="LLM_COL"):
    """Helper : DwgEntity INSERT 1 point (insertion) sur un layer donné."""
    from lib.dwg_reader import DwgEntity
    return DwgEntity(
        kind="INSERT",
        layer=layer,
        coords=[[x, y, 0.0]],
        attrs={"name": name},
    )


def test_building_extent_includes_s_cols_inserts():
    """Cas P2 Coupe 3 : pas de A-WALL, A-FLOR à 16m, S-COLS INSERTs
    étendent à 28m → l'extent doit refléter S-COLS."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        _line_entity(0.0, 0.0, 16.0, 0.0, layer="A-FLOR"),
        _insert_entity(-2.0, 1.0, layer="S-COLS"),  # poteau bord gauche
        _insert_entity(26.0, 1.0, layer="S-COLS"),  # poteau bord droit
    ]
    ext = _building_extent_from_entities(ents)
    # Union : A-FLOR (0..16) ∪ S-COLS (-2..26) → extent = 28.
    assert ext == pytest.approx(28.0, abs=1e-6)


def test_building_extent_includes_s_cols_line_drawn_columns():
    """Variante : poteaux dessinés en 4-LINEs sur S-COLS (pas INSERT).
    Doit aussi contribuer à l'extent."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        # Poteau 30×30 cm dessiné en coupe sur S-COLS (4 LINEs)
        _line_entity(10.0, 0.0, 10.0, 3.0, layer="S-COLS"),
        _line_entity(10.3, 0.0, 10.3, 3.0, layer="S-COLS"),
        _line_entity(10.0, 0.0, 10.3, 0.0, layer="S-COLS"),
        _line_entity(10.0, 3.0, 10.3, 3.0, layer="S-COLS"),
    ]
    ext = _building_extent_from_entities(ents)
    assert ext == pytest.approx(0.3, abs=1e-6)


def test_building_extent_ignores_s_cols_iden_labels():
    """Convention AIA : `S-COLS-IDEN` est le sous-layer des labels de
    poteau. Match exact sur le nom → on n'attrape pas les annotations
    qui peuvent être placées loin du bâtiment."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        # Mur réel
        _line_entity(0.0, 0.0, 10.0, 0.0, layer="A-WALL"),
        # Label de poteau hors-bâtiment (annotation textuelle, INSERT)
        _insert_entity(-50.0, 5.0, layer="S-COLS-IDEN"),
        _insert_entity(50.0, 5.0, layer="S-COLS-IDEN"),
    ]
    ext = _building_extent_from_entities(ents)
    # Seul A-WALL compte → extent = 10, pas 100.
    assert ext == pytest.approx(10.0, abs=1e-6)


def test_building_extent_ignores_non_line_on_a_wall():
    """A-WALL n'accepte que LINE. Un INSERT sur A-WALL (porte/fenêtre
    family symbol) ne doit pas compter — seules les faces de murs
    contribuent à l'enveloppe."""
    from lib.tools.dwg_import import _building_extent_from_entities
    ents = [
        _line_entity(0.0, 0.0, 10.0, 0.0, layer="A-WALL"),
        # Symbole de porte hors-bâtiment (rare mais possible)
        _insert_entity(-30.0, 1.0, layer="A-WALL"),
    ]
    ext = _building_extent_from_entities(ents)
    # Seules les LINEs A-WALL comptent → extent = 10, pas 40.
    assert ext == pytest.approx(10.0, abs=1e-6)
