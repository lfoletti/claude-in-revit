"""Tests V2 — voting framework + elevation_reader."""
from __future__ import annotations

import math

import pytest

from lib import dwg_voting as vt
from lib.dwg_voting import Vote, aggregate_votes, yes_vote, no_vote, abstain
from lib import dwg_elevation_reader as er
from lib.dwg_elevation_reader import (
    ElevationLine,
    ElevationView,
    parse_elevation,
    project_world_to_elevation,
    vote_wall_visible_in_elevation,
)
from lib.dwg_reader import DwgEntity


# ----- Voting framework ------------------------------------------------


def test_aggregate_all_yes():
    votes = [yes_vote("plan"), yes_vote("coupe"), yes_vote("elev_Sud")]
    d = aggregate_votes(votes)
    assert d.answer is True
    assert d.confidence_score == pytest.approx(1.0)


def test_aggregate_all_no():
    votes = [no_vote("plan"), no_vote("coupe")]
    d = aggregate_votes(votes)
    assert d.answer is False


def test_aggregate_majority_yes():
    votes = [yes_vote("a", 1.0), yes_vote("b", 1.0), no_vote("c", 1.0)]
    d = aggregate_votes(votes)
    assert d.answer is True
    assert d.confidence_score == pytest.approx(2.0 / 3.0, abs=1e-3)


def test_aggregate_threshold_two_thirds():
    # 2 yes + 1 no = ratio 0.66. With threshold 0.67, should be False.
    votes = [yes_vote("a", 1.0), yes_vote("b", 1.0), no_vote("c", 1.0)]
    d = aggregate_votes(votes, threshold=0.67)
    assert d.answer is False


def test_aggregate_abstain_only_returns_none():
    votes = [abstain("a"), abstain("b")]
    d = aggregate_votes(votes)
    assert d.answer is None


def test_aggregate_min_voters_not_met():
    votes = [yes_vote("a")]
    d = aggregate_votes(votes, min_voters=2)
    assert d.answer is None


def test_aggregate_confidence_weighted():
    # 1 yes confiance 0.5 + 1 no confiance 1.0 → no wins.
    votes = [yes_vote("a", 0.5), no_vote("b", 1.0)]
    d = aggregate_votes(votes)
    assert d.answer is False


# ----- Elevation projection -------------------------------------------


def test_project_nord_inverts_x():
    x, y = project_world_to_elevation(5.0, 3.0, 2.0, "Nord")
    assert x == pytest.approx(-5.0)
    assert y == pytest.approx(2.0)


def test_project_sud_keeps_x():
    x, y = project_world_to_elevation(5.0, 3.0, 2.0, "Sud")
    assert x == pytest.approx(5.0)
    assert y == pytest.approx(2.0)


def test_project_est_uses_y():
    x, y = project_world_to_elevation(5.0, 3.0, 2.0, "Est")
    assert x == pytest.approx(3.0)
    assert y == pytest.approx(2.0)


def test_project_ouest_inverts_y():
    x, y = project_world_to_elevation(5.0, 3.0, 2.0, "Ouest")
    assert x == pytest.approx(-3.0)
    assert y == pytest.approx(2.0)


def test_project_invalid_direction_raises():
    with pytest.raises(ValueError):
        project_world_to_elevation(0, 0, 0, "Centre")


# ----- parse_elevation -----------------------------------------------


def _line(x1, y1, x2, y2, layer="A-WALL"):
    return DwgEntity(kind="LINE", layer=layer,
                     coords=[[x1, y1, 0.0], [x2, y2, 0.0]], attrs={})


def test_parse_elevation_extracts_walls_and_levels():
    ents = [
        _line(0, 0, 10, 0),        # horizontal A-WALL
        _line(0, 0, 0, 3),         # vertical A-WALL
        _line(0, 0, 10, 0, layer="A-FLOR-LEVL"),  # level 0
        _line(0, 3, 10, 3, layer="A-FLOR-LEVL"),  # level 1
        _line(0, 0, 10, 0, layer="A-GLAZ"),       # ignored
    ]
    ev = parse_elevation(ents, "Sud")
    assert len(ev.a_wall_lines) == 2
    assert ev.a_wall_bbox == (0.0, 10.0, 0.0, 3.0)
    assert 0.0 in ev.levels_y
    assert 3.0 in ev.levels_y


# ----- vote_wall_visible_in_elevation --------------------------------


def test_vote_wall_outside_bbox_abstains():
    ev = ElevationView(
        direction="Sud",
        a_wall_lines=[ElevationLine((0, 0), (10, 0), is_horizontal=True, is_vertical=False)],
        a_wall_bbox=(0.0, 10.0, 0.0, 3.0),
        levels_y=[0.0],
    )
    # mur projeté à x=-20 → hors bbox.
    v = vote_wall_visible_in_elevation((-20, 0), (-15, 0), 0.0, 3.0, ev)
    assert v.answer is None


def test_vote_wall_visible_with_overlap():
    # Élévation Sud avec linteau horizontal à y=2 de x=0 à x=10.
    ev = ElevationView(
        direction="Sud",
        a_wall_lines=[
            ElevationLine((0, 2), (10, 2), is_horizontal=True, is_vertical=False),
            ElevationLine((0, 0), (0, 3), is_horizontal=False, is_vertical=True),
            ElevationLine((10, 0), (10, 3), is_horizontal=False, is_vertical=True),
        ],
        a_wall_bbox=(0.0, 10.0, 0.0, 3.0),
        levels_y=[0.0, 3.0],
    )
    # mur world (2, y) à (8, y) → x_elev = [2, 8].
    v = vote_wall_visible_in_elevation((2, 0), (8, 0), 0.0, 3.0, ev)
    assert v.answer is True
    assert v.confidence > 0.3


def test_vote_wall_no_overlap_votes_no():
    # Élévation Sud sans aucun A-WALL dans la zone projetée.
    ev = ElevationView(
        direction="Sud",
        a_wall_lines=[
            ElevationLine((20, 2), (30, 2), is_horizontal=True, is_vertical=False),
        ],
        a_wall_bbox=(0.0, 30.0, 0.0, 3.0),
        levels_y=[0.0],
    )
    # mur projeté en x_elev = [2, 8] mais zone vide.
    v = vote_wall_visible_in_elevation((2, 0), (8, 0), 0.0, 3.0, ev)
    assert v.answer is False


# ----- Integration : vote_wall in real P7 elevation if fixtures present


P7_DIR = "C:/Users/lauro/Documents/IT/claude-in-revit-projects/P7"

p7_available = pytest.mark.skipif(
    not __import__("pathlib").Path(P7_DIR).exists(),
    reason="P7 fixtures not present",
)


# ----- Votes coupe (vote_wall_visible_in_section / opening) -----------


def test_vote_wall_visible_in_section_yes_when_thickness_matches():
    from lib.dwg_plan_openings import vote_wall_visible_in_section
    # Trait vertical à x=5, mur horizontal traversant à y=3 → cross at (5, 3).
    # Trait vertical → x_cut = world Y = 3.
    section_line = {
        "plan_p1": [5.0, 0.0], "plan_p2": [5.0, 10.0],
        "coupe_path": "/c.dxf", "name": "C1",
    }
    section_walls = [
        {"x_cut_m": 3.0, "thickness_m": 0.20,
         "y_bottom_m": 0.0, "y_top_m": 3.0},
    ]
    v = vote_wall_visible_in_section(
        (0.0, 3.0), (10.0, 3.0), 0.0, 3.0,
        section_line, section_walls, wall_thickness_m=0.20,
    )
    assert v.answer is True
    assert v.confidence >= 0.85


def test_vote_wall_visible_in_section_abstains_when_no_crossing():
    from lib.dwg_plan_openings import vote_wall_visible_in_section
    section_line = {"plan_p1": [5.0, 0.0], "plan_p2": [5.0, 10.0],
                    "coupe_path": "/c.dxf"}
    v = vote_wall_visible_in_section(
        (0.0, 20.0), (10.0, 20.0), 0.0, 3.0,
        section_line, [],
    )
    assert v.answer is None  # abstain (no crossing)


def test_vote_wall_visible_in_section_no_when_absent():
    from lib.dwg_plan_openings import vote_wall_visible_in_section
    section_line = {"plan_p1": [5.0, 0.0], "plan_p2": [5.0, 10.0],
                    "coupe_path": "/c.dxf"}
    v = vote_wall_visible_in_section(
        (0.0, 3.0), (10.0, 3.0), 0.0, 3.0,
        section_line, [],  # pas de section walls
    )
    assert v.answer is False


def test_vote_opening_visible_in_section_yes_when_block_id_matches():
    from lib.dwg_plan_openings import vote_opening_visible_in_section
    section_line = {"plan_p1": [5.0, 0.0], "plan_p2": [5.0, 10.0],
                    "coupe_path": "/c.dxf", "name": "C1"}
    by_id = {"123": {"sill_m": 0.9, "height_m": 1.5}}
    v = vote_opening_visible_in_section(
        (5.0, 4.0), "123", section_line, by_id,
    )
    assert v.answer is True


def test_vote_opening_visible_in_section_abstains_far_from_trait():
    from lib.dwg_plan_openings import vote_opening_visible_in_section
    section_line = {"plan_p1": [5.0, 0.0], "plan_p2": [5.0, 10.0],
                    "coupe_path": "/c.dxf"}
    v = vote_opening_visible_in_section(
        (10.0, 4.0), "123", section_line, {"123": {"sill_m": 0.9, "height_m": 1.5}},
    )
    assert v.answer is None  # abstain : opening loin du trait


# ----- Votes opening dans élévation -----------------------------------


def test_vote_opening_in_elevation_yes_with_linteau():
    from lib.dwg_elevation_reader import vote_opening_visible_in_elevation
    # Élévation Sud avec linteau à y=2.5 et allège à y=1.0.
    ev = ElevationView(
        direction="Sud",
        a_wall_lines=[
            ElevationLine((2, 2.5), (8, 2.5), is_horizontal=True, is_vertical=False),
            ElevationLine((2, 1.0), (8, 1.0), is_horizontal=True, is_vertical=False),
        ],
        a_wall_bbox=(0.0, 10.0, 0.0, 3.0),
        levels_y=[0.0],
    )
    # opening world (5, y) → x_elev=5 (Sud). sill=1.0, height=1.5 → head=2.5.
    v = vote_opening_visible_in_elevation(
        (5.0, 0.0), 0.0, sill_m=1.0, height_m=1.5, width_m=1.0,
        elevation=ev,
    )
    assert v.answer is True


@p7_available
def test_p7_sud_elevation_votes_yes_for_south_exterior_wall():
    """Sur P7, le mur extérieur Sud (y=-1.17) doit voter yes confiance 1
    depuis l'élévation Sud."""
    from pathlib import Path
    from lib.dwg_reader import parse
    for f in Path(P7_DIR).glob("*Sud*.dxf"):
        ents, _ = parse(f)
        ev = parse_elevation(ents, "Sud")
        v = vote_wall_visible_in_elevation(
            (-9.49, -1.17), (-5.36, -1.17), 0.0, 3.0, ev,
        )
        assert v.answer is True
        assert v.confidence >= 0.9
        return
    pytest.skip("Élévation Sud not found in P7")
