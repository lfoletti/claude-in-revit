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
