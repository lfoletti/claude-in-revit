"""Tests UC1 Phase 4 — dwg_section_reader (classify, levels, openings, match).

Fixtures DXF générées en mémoire via ezdxf. Couverture :
- parse_block_id / parse_block_dimensions (regex)
- classify_dxf (heuristique layer)
- read_levels (3 niveaux d'une coupe synthétique)
- read_section_openings (INSERTs A-GLAZ)
- match_openings (par block_id partagé)
- Integration : si DXF Projet4 présents dans le dossier connu, valide
  les counts attendus (skip propre sinon).
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from lib import dwg_reader, dwg_section_reader as dsr
from lib.dwg_section_reader import (
    Level,
    SectionOpening,
    classify_dxf,
    match_openings,
    parse_block_dimensions,
    parse_block_id,
    read_levels,
    read_section_openings,
)


# ----- Fixtures DXF synthétiques ---------------------------------------


def _make_section_dxf(
    tmp_path: Path,
    *,
    level_elevations_m: tuple = (0.0, 3.0, 6.0),
    units_code: int = 4,  # mm — la convention Projet4
    with_labels: bool = True,
    with_glazing: tuple = (),  # tuples (block_name, x_mm, y_mm, rot)
) -> Path:
    """Génère un DXF minimal de coupe : layers A-FLOR-LEVL + A-GLAZ.

    Les `level_elevations_m` génèrent une ligne horizontale à Y = elev*1000
    (en mm puisque units_code=4), avec MTEXT « Niveau N » à Y+321 mm et
    MTEXT valeur à Y+951 mm — réplique de la convention Projet4.

    `with_glazing` insère des INSERTs sur A-GLAZ (block doit être défini
    dans la table avant l'insertion).
    """
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = units_code
    doc.layers.add("A-FLOR-LEVL")
    doc.layers.add("A-GLAZ")
    msp = doc.modelspace()

    # Niveaux : 1 LINE + 2 MTEXT par niveau.
    line_x_min, line_x_max = -16000.0, 15500.0
    for i, elev in enumerate(level_elevations_m):
        y_line = elev * 1000  # mm
        msp.add_line(
            (line_x_min, y_line), (line_x_max, y_line),
            dxfattribs={"layer": "A-FLOR-LEVL"},
        )
        if with_labels:
            label = f"Niveau {i}"
            msp.add_mtext(label, dxfattribs={
                "layer": "A-FLOR-LEVL",
                "insert": (line_x_max + 200, y_line + 321),
                "char_height": 300,
            })
            msp.add_mtext(f"{elev:.2f}".rstrip("0").rstrip("."), dxfattribs={
                "layer": "A-FLOR-LEVL",
                "insert": (line_x_max + 200, y_line + 951),
                "char_height": 300,
            })

    # Ouvertures vitrées.
    for block_name, x_mm, y_mm, rot in with_glazing:
        if block_name not in doc.blocks:
            blk = doc.blocks.new(name=block_name)
            # Carré 1m × 1.4m schématique dans le bloc (suffit pour le test).
            blk.add_line((0, 0), (1000, 0), dxfattribs={"layer": "A-GLAZ"})
            blk.add_line((1000, 0), (1000, 1400), dxfattribs={"layer": "A-GLAZ"})
            blk.add_line((1000, 1400), (0, 1400), dxfattribs={"layer": "A-GLAZ"})
            blk.add_line((0, 1400), (0, 0), dxfattribs={"layer": "A-GLAZ"})
        msp.add_blockref(
            block_name, (x_mm, y_mm),
            dxfattribs={"layer": "A-GLAZ", "rotation": rot},
        )

    path = tmp_path / "section.dxf"
    doc.saveas(str(path))
    return path


def _make_plan_dxf(
    tmp_path: Path,
    *,
    units_code: int = 4,
    rooms: tuple = (("Pièce 1", 100, 200),),
    glazing: tuple = (),
) -> Path:
    """DXF de plan minimal : layers A-AREA-IDEN + A-GLAZ optional."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = units_code
    doc.layers.add("A-AREA-IDEN")
    doc.layers.add("A-GLAZ")
    msp = doc.modelspace()

    for name, x, y in rooms:
        msp.add_mtext(name, dxfattribs={
            "layer": "A-AREA-IDEN", "insert": (x, y), "char_height": 200,
        })

    for block_name, x_mm, y_mm, rot in glazing:
        if block_name not in doc.blocks:
            blk = doc.blocks.new(name=block_name)
            blk.add_line((0, 0), (1000, 0), dxfattribs={"layer": "A-GLAZ"})
        msp.add_blockref(
            block_name, (x_mm, y_mm),
            dxfattribs={"layer": "A-GLAZ", "rotation": rot},
        )

    path = tmp_path / "plan.dxf"
    doc.saveas(str(path))
    return path


# ----- parse_block_id ---------------------------------------------------


def test_parse_block_id_extracts_revit_id():
    name = "1 Vantail - Droit - 2_00 m x 1_40 m - Appui en aluminium-255828-Niveau 0"
    assert parse_block_id(name) == "255828"


def test_parse_block_id_works_for_coupe_suffix():
    name = "1 Vantail - Droit - 2_00 m x 1_40 m - Appui en aluminium-255828-Coupe 1"
    assert parse_block_id(name) == "255828"


def test_parse_block_id_none_for_unrecognized_pattern():
    assert parse_block_id("random block name") is None
    assert parse_block_id("Niveau - Marqueur") is None  # pas d'ID avant Niveau


def test_parse_block_id_accepts_alphanumeric_variant():
    """Projet4 re-exporté 2026-05-13 inclut un bloc avec suffixe `V1`
    au lieu de l'ID numérique habituel — la regex doit l'accepter."""
    name = "1 Vantail - Droit - 1_20 m x 1_40 m - Appui en aluminium-V1-Coupe 2"
    assert parse_block_id(name) == "V1"


# ----- parse_block_dimensions ------------------------------------------


def test_parse_block_dimensions_standard_pattern():
    name = "1 Vantail - Droit - 2_00 m x 1_40 m - Appui en aluminium-255828-Coupe 1"
    assert parse_block_dimensions(name) == (2.0, 1.4)


def test_parse_block_dimensions_smaller_window():
    name = "1 Vantail - Droit - 1_20 m x 1_40 m - Est-255830-Coupe 1"
    assert parse_block_dimensions(name) == (1.2, 1.4)


def test_parse_block_dimensions_none_when_absent():
    assert parse_block_dimensions("Block without dimensions") is None


# ----- classify_dxf -----------------------------------------------------


def test_classify_dxf_section_via_flor_levl(tmp_path):
    path = _make_section_dxf(tmp_path)
    _, meta = dwg_reader.parse(path)
    kind, evidence = classify_dxf(meta["layers"])
    assert kind == "section"
    assert evidence["lines_count"] >= 1


def test_classify_dxf_plan_via_area_iden(tmp_path):
    path = _make_plan_dxf(tmp_path)
    _, meta = dwg_reader.parse(path)
    kind, evidence = classify_dxf(meta["layers"])
    assert kind == "plan"
    assert evidence["mtext_count"] >= 1


def test_classify_dxf_unknown_when_no_signature(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("RANDOM")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 100), dxfattribs={"layer": "RANDOM"})
    path = tmp_path / "u.dxf"
    doc.saveas(str(path))
    _, meta = dwg_reader.parse(path)
    kind, evidence = classify_dxf(meta["layers"])
    assert kind == "unknown"
    assert "RANDOM" in evidence["available_layers"]


# ----- read_levels ------------------------------------------------------


def test_read_levels_three_levels_with_labels(tmp_path):
    path = _make_section_dxf(tmp_path, level_elevations_m=(0.0, 3.0, 6.0))
    entities, _ = dwg_reader.parse(path)
    levels = read_levels(entities)
    assert len(levels) == 3
    assert [l.elevation_m for l in levels] == [0.0, 3.0, 6.0]
    assert [l.name for l in levels] == ["Niveau 0", "Niveau 1", "Niveau 2"]
    assert all(l.source == "mtext_label+value" for l in levels)


def test_read_levels_no_labels_fallback_to_y_inference(tmp_path):
    path = _make_section_dxf(
        tmp_path,
        level_elevations_m=(0.0, 2.5),
        with_labels=False,
    )
    entities, _ = dwg_reader.parse(path)
    levels = read_levels(entities)
    assert len(levels) == 2
    # Sans label, elevation tirée de Y (= elev_m après conversion mm → m).
    assert math.isclose(levels[0].elevation_m, 0.0, abs_tol=1e-6)
    assert math.isclose(levels[1].elevation_m, 2.5, abs_tol=1e-6)
    assert all(l.source == "line_only_inferred" for l in levels)
    assert [l.name for l in levels] == ["Niveau 0", "Niveau 1"]


def test_read_levels_returns_empty_if_no_flor_levl(tmp_path):
    path = _make_plan_dxf(tmp_path)
    entities, _ = dwg_reader.parse(path)
    levels = read_levels(entities)
    assert levels == []


# ----- read_section_openings -------------------------------------------


def test_read_section_openings_collects_glaz_inserts(tmp_path):
    block_name = "1 Vantail - Droit - 2_00 m x 1_40 m - Appui en aluminium-255828-Coupe 1"
    path = _make_section_dxf(
        tmp_path,
        with_glazing=((block_name, 0, 0, 0), (block_name, 0, 3000, 0)),
    )
    entities, _ = dwg_reader.parse(path)
    openings = read_section_openings(entities)
    assert len(openings) == 2
    o0 = openings[0]
    assert o0.block_id == "255828"
    assert o0.width_m == 2.0
    assert o0.height_m == 1.4
    assert math.isclose(o0.x_dxf_m, 0.0, abs_tol=1e-6)
    assert math.isclose(o0.y_dxf_m, 0.0, abs_tol=1e-6)
    assert math.isclose(openings[1].y_dxf_m, 3.0, abs_tol=1e-6)


def test_read_section_openings_unknown_id_ok(tmp_path):
    """Un bloc dont le nom ne suit pas la convention : id=None mais
    l'INSERT reste référencé (pour permettre debug)."""
    name = "random block"
    path = _make_section_dxf(tmp_path, with_glazing=((name, 100, 200, 90),))
    entities, _ = dwg_reader.parse(path)
    openings = read_section_openings(entities)
    assert len(openings) == 1
    assert openings[0].block_id is None
    assert openings[0].width_m is None


# ----- match_openings ---------------------------------------------------


def test_match_openings_by_block_id(tmp_path):
    # Plan : 2 fenêtres avec block_id 255828, 1 avec 255830.
    name_a = "Block-255828-Niveau 0"
    name_b = "Block-255830-Niveau 0"
    plan_path = _make_plan_dxf(
        tmp_path,
        glazing=(
            (name_a, 0, 0, 0),
            (name_a, 2000, 0, 0),
            (name_b, 0, 4000, 90),
        ),
    )
    # Section : 1 instance de 255828, 0 de 255830 (donc unmatched), 1
    # de 255999 (orphelin).
    section_doc_path = tmp_path / "section2.dxf"
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("A-GLAZ")
    doc.layers.add("A-FLOR-LEVL")
    msp = doc.modelspace()
    msp.add_line((-1000, 0), (1000, 0), dxfattribs={"layer": "A-FLOR-LEVL"})
    for name in (
        "Block-255828-Coupe 1",
        "Block-255999-Coupe 1",
    ):
        if name not in doc.blocks:
            doc.blocks.new(name=name).add_line((0, 0), (1, 0), dxfattribs={"layer": "A-GLAZ"})
        msp.add_blockref(name, (0, 0), dxfattribs={"layer": "A-GLAZ"})
    doc.saveas(str(section_doc_path))

    plan_entities, _ = dwg_reader.parse(plan_path)
    sec_entities, _ = dwg_reader.parse(section_doc_path)
    plan_openings = read_section_openings(plan_entities)
    sec_openings = read_section_openings(sec_entities)

    matches, unmatched_sec, unmatched_plan = match_openings(plan_openings, sec_openings)
    assert len(matches) == 1
    m = matches[0]
    assert m.block_id == "255828"
    assert len(m.plan_indices) == 2  # 2 instances de 255828 dans le plan
    assert unmatched_sec == [1]  # 255999 sans pendant
    assert unmatched_plan == [2]  # 255830 sans pendant


# ----- Integration : DXF réels Projet4 (skip si absents) ---------------


PROJET4_DIR = Path(r"C:\Users\lauro\Documents\IT\claude-in-revit-projects\DXF")
PROJET4_PLAN = PROJET4_DIR / "Projet4 - Plan d'étage - Niveau 0.dxf"
PROJET4_COUPE1 = PROJET4_DIR / "Projet4 - Coupe - Coupe 1.dxf"
PROJET4_COUPE2 = PROJET4_DIR / "Projet4 - Coupe - Coupe 2.dxf"

projet4_available = pytest.mark.skipif(
    not PROJET4_COUPE1.exists(),
    reason="Projet4 DXF fixtures not present on this machine",
)


@projet4_available
def test_projet4_coupe1_classified_as_section():
    _, meta = dwg_reader.parse(PROJET4_COUPE1)
    kind, _ = classify_dxf(meta["layers"])
    assert kind == "section"


@projet4_available
def test_projet4_plan_classified_as_plan():
    _, meta = dwg_reader.parse(PROJET4_PLAN)
    kind, _ = classify_dxf(meta["layers"])
    assert kind == "plan"


@projet4_available
def test_projet4_coupe1_three_levels_at_0_3_6():
    entities, _ = dwg_reader.parse(PROJET4_COUPE1)
    levels = read_levels(entities)
    assert len(levels) == 3
    elevs = sorted(round(l.elevation_m, 2) for l in levels)
    assert elevs == [0.0, 3.0, 6.0]
    assert all(l.source == "mtext_label+value" for l in levels)


@projet4_available
def test_projet4_coupe1_has_22_openings():
    entities, _ = dwg_reader.parse(PROJET4_COUPE1)
    openings = read_section_openings(entities)
    assert len(openings) == 22
    # Tous les openings de Projet4 ont un block_id reconnu.
    assert all(o.block_id is not None for o in openings)


@projet4_available
def test_projet4_plan_has_20_openings_all_with_id():
    entities, _ = dwg_reader.parse(PROJET4_PLAN)
    openings = read_section_openings(entities)
    assert len(openings) == 20
    assert all(o.block_id is not None for o in openings)


@projet4_available
def test_projet4_coupe1_matches_plan_via_block_id():
    """Test structural — les IDs Revit changent au re-export du DXF
    (observé 2026-05-13 : passage de 255828/29/30/31 à 258141/258127/
    257925/257855/etc.). On vérifie donc :
    - Au moins 95% des ouvertures de la coupe matchent (≥ 21/22).
    - Au moins 2 IDs distincts → la coupe couvre plusieurs façades.
    - Les unmatched_plan reflètent les ouvertures absentes de la coupe
      (façades non visibles depuis cet angle de coupe).
    """
    plan_e, _ = dwg_reader.parse(PROJET4_PLAN)
    sec_e, _ = dwg_reader.parse(PROJET4_COUPE1)
    plan_o = read_section_openings(plan_e)
    sec_o = read_section_openings(sec_e)
    matches, unmatched_sec, unmatched_plan = match_openings(plan_o, sec_o)
    # Au moins 95% des openings doivent matcher (variants V1 etc.
    # tolérés via regex étendue, mais 1 fail occasionnel OK).
    assert len(matches) >= 21, f"Only {len(matches)}/22 matched"
    distinct_ids = {m.block_id for m in matches}
    assert len(distinct_ids) >= 2, (
        f"Expected ≥ 2 distinct facades, got {len(distinct_ids)}: "
        f"{distinct_ids}"
    )
    # Au moins certaines ouvertures du plan ne sont pas dans cette coupe
    # (la façade Ouest n'est pas visible depuis Coupe 1 par exemple).
    assert len(unmatched_plan) > 0, "All plan openings matched — unexpected"
