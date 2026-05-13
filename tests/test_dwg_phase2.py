"""Tests Phase 2 étapes 2-4 — extract épaisseurs + create types + import typed.

Couvre :
- `dwg_extract_wall_thicknesses` : bucketing + distribution.
- `walls_get_or_create_dxf_type` : KG-only create + idempotence.
- `walls_get_or_create_dxf_type_many` : dédup + bulk.
- `dwg_import_walls_typed` : orchestration roundtrip KG-only.

KG-only path (doc=None) — pas de Revit nécessaire.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from lib import llm_protocol
from lib.project_kg import ProjectKG


@pytest.fixture
def kg_fresh():
    llm_protocol.reset_registry()
    llm_protocol.get_registry()
    kg = ProjectKG("p")
    kg.advance_turn()
    return kg


# ----- Fixture DXF plan multi-épaisseurs ----------------------------


def _make_multi_thickness_plan(tmp_path: Path) -> Path:
    """Plan DXF avec 3 murs de 3 épaisseurs différentes (15cm, 20cm, 30cm).

    Layout : 3 paires parallèles à des x différents.
    """
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6  # metres
    doc.layers.add("A-WALL")
    msp = doc.modelspace()

    # Mur 1 : 15cm à x=2.
    msp.add_line((2.0 - 0.075, 0.0), (2.0 - 0.075, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((2.0 + 0.075, 0.0), (2.0 + 0.075, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    # Mur 2 : 20cm à x=5.
    msp.add_line((5.0 - 0.10, 0.0), (5.0 - 0.10, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((5.0 + 0.10, 0.0), (5.0 + 0.10, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    # Mur 3 : 30cm à x=8.
    msp.add_line((8.0 - 0.15, 0.0), (8.0 - 0.15, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((8.0 + 0.15, 0.0), (8.0 + 0.15, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    p = tmp_path / "plan_multi.dxf"
    doc.saveas(str(p))
    return p


# ----- dwg_extract_wall_thicknesses ---------------------------------


def test_extract_thicknesses_groups_3_buckets(tmp_path, kg_fresh):
    plan = _make_multi_thickness_plan(tmp_path)
    result = llm_protocol.dispatch_tool_use(
        "dwg_extract_wall_thicknesses",
        {"file_path": str(plan)},
        "t1",
        kg_fresh,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["walls_count"] == 3
    buckets = payload["thickness_buckets"]
    assert len(buckets) == 3
    cms = sorted(b["cm"] for b in buckets)
    assert cms == [15, 20, 30]
    type_names = sorted(b["type_name"] for b in buckets)
    assert type_names == ["DXF_WALL_15cm", "DXF_WALL_20cm", "DXF_WALL_30cm"]


def test_extract_thicknesses_bucket_5cm_merges(tmp_path, kg_fresh):
    plan = _make_multi_thickness_plan(tmp_path)
    # Avec bucket_cm=5, 15 et 20 restent distincts mais arrondis.
    result = llm_protocol.dispatch_tool_use(
        "dwg_extract_wall_thicknesses",
        {"file_path": str(plan), "bucket_cm": 5},
        "t1",
        kg_fresh,
    )
    payload = json.loads(result["content"])
    assert payload["bucket_cm"] == 5
    # 15 → 15, 20 → 20, 30 → 30 (multiples de 5).
    cms = sorted(b["cm"] for b in payload["thickness_buckets"])
    assert cms == [15, 20, 30]


# ----- walls_get_or_create_dxf_type (KG-only) ----------------------


def test_get_or_create_dxf_type_kg_only_creates(kg_fresh):
    result = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type",
        {"thickness_m": 0.20},
        "t1",
        kg_fresh,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["created"] is True
    assert payload["name"] == "DXF_WALL_20cm"
    assert payload["thickness_m"] == pytest.approx(0.20)
    # Vérifie node en KG.
    node = kg_fresh.get_node(payload["llm_id"])
    assert node["_type"] == "WallType"
    assert node["name"] == "DXF_WALL_20cm"
    assert node["total_thickness"] == pytest.approx(0.20)


def test_get_or_create_dxf_type_idempotent(kg_fresh):
    r1 = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type",
        {"thickness_m": 0.20}, "t1", kg_fresh,
    )
    p1 = json.loads(r1["content"])
    assert p1["created"] is True

    # Deuxième appel : doit retourner le même llm_id, created=False.
    r2 = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type",
        {"thickness_m": 0.20}, "t2", kg_fresh,
    )
    p2 = json.loads(r2["content"])
    assert p2["created"] is False
    assert p2["llm_id"] == p1["llm_id"]


def test_get_or_create_dxf_type_buckets_at_cm(kg_fresh):
    # 20.5cm bucketé au cm près → 20cm (round arithm)... attention,
    # round(0.205 * 100) = round(20.5) = 20 sous banker's rounding,
    # ou 21 sinon. Le test reste robuste en vérifiant un nom DXF_WALL_*.
    r = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type",
        {"thickness_m": 0.21}, "t1", kg_fresh,
    )
    p = json.loads(r["content"])
    assert p["name"] == "DXF_WALL_21cm"


def test_get_or_create_dxf_type_rejects_negative(kg_fresh):
    r = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type",
        {"thickness_m": -0.10}, "t1", kg_fresh,
    )
    assert r.get("is_error") is True
    assert "positive" in r["content"].lower()


# ----- walls_get_or_create_dxf_type_many ---------------------------


def test_get_or_create_many_dedups(kg_fresh):
    # 3 thicknesses dont 2 bucketent à 20cm.
    r = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type_many",
        {"thicknesses_m": [0.15, 0.20, 0.20, 0.30, 0.205], "bucket_cm": 1},
        "t1", kg_fresh,
    )
    assert not r.get("is_error"), r.get("content")
    p = json.loads(r["content"])
    # 0.205 bucketé : round(20.5) → 20 (banker's) ou 21 selon Python.
    # On accepte 3 ou 4 types uniques selon.
    names = sorted(t["name"] for t in p["types"])
    assert "DXF_WALL_15cm" in names
    assert "DXF_WALL_20cm" in names
    assert "DXF_WALL_30cm" in names
    assert p["created_count"] >= 3
    assert p["reused_count"] == 0


def test_get_or_create_many_reuses_existing(kg_fresh):
    # Crée d'abord un type 20cm.
    r1 = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type",
        {"thickness_m": 0.20}, "t1", kg_fresh,
    )
    p1 = json.loads(r1["content"])

    # Bulk avec un nouveau 15cm et un 20cm existant.
    r2 = llm_protocol.dispatch_tool_use(
        "walls_get_or_create_dxf_type_many",
        {"thicknesses_m": [0.15, 0.20]}, "t2", kg_fresh,
    )
    p2 = json.loads(r2["content"])
    assert p2["created_count"] == 1   # 15cm
    assert p2["reused_count"] == 1    # 20cm
    # Le 20cm doit pointer sur le même llm_id qu'avant.
    reused_entry = next(t for t in p2["types"] if t["name"] == "DXF_WALL_20cm")
    assert reused_entry["llm_id"] == p1["llm_id"]


# ----- dwg_import_walls_typed (KG-only roundtrip) ------------------


def test_import_walls_typed_kg_only(tmp_path, kg_fresh):
    # Crée un Level d'abord.
    level_id = kg_fresh.add_node("Level", {"name": "N0", "elevation": 0.0})

    plan = _make_multi_thickness_plan(tmp_path)
    result = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed",
        {
            "file_path": str(plan),
            "level_ref": level_id,
            "height_m": 3.0,
        },
        "t1", kg_fresh,
    )
    assert not result.get("is_error"), result.get("content")
    payload = json.loads(result["content"])
    assert payload["walls_imported"] == 3
    assert payload["types_created"] == 3  # 15, 20, 30
    assert payload["types_reused"] == 0
    # Distribution attendue.
    dist = payload["thickness_distribution"]
    assert dist == {"15cm": 1, "20cm": 1, "30cm": 1}
    # Vérifie que les 3 types existent en KG.
    type_names = {
        kg_fresh.get_node(nid).get("name")
        for nid in kg_fresh.find_by_type("WallType")
    }
    assert {"DXF_WALL_15cm", "DXF_WALL_20cm", "DXF_WALL_30cm"} <= type_names
    # 3 walls créés en KG.
    wall_ids = list(kg_fresh.find_by_type("Wall"))
    assert len(wall_ids) == 3
    # Chaque mur a un type_ref pointant vers un WallType DXF_WALL_*.
    for wid in wall_ids:
        wnode = kg_fresh.get_node(wid)
        type_node = kg_fresh.get_node(wnode["type_ref"])
        assert type_node["name"].startswith("DXF_WALL_")


def test_import_walls_typed_idempotent_types(tmp_path, kg_fresh):
    """Re-import : les types existants sont réutilisés, pas re-créés."""
    level_id = kg_fresh.add_node("Level", {"name": "N0", "elevation": 0.0})
    plan = _make_multi_thickness_plan(tmp_path)

    # Premier import.
    r1 = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed",
        {"file_path": str(plan), "level_ref": level_id, "height_m": 3.0},
        "t1", kg_fresh,
    )
    p1 = json.loads(r1["content"])
    assert p1["types_created"] == 3
    assert p1["types_reused"] == 0

    # Deuxième import (autre tour).
    kg_fresh.advance_turn()
    r2 = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed",
        {"file_path": str(plan), "level_ref": level_id, "height_m": 3.0},
        "t2", kg_fresh,
    )
    p2 = json.loads(r2["content"])
    # Tous les types sont réutilisés.
    assert p2["types_created"] == 0
    assert p2["types_reused"] == 3


def test_import_walls_typed_unknown_level_rejects(tmp_path, kg_fresh):
    plan = _make_multi_thickness_plan(tmp_path)
    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed",
        {"file_path": str(plan), "level_ref": "bogus_id"},
        "t1", kg_fresh,
    )
    assert r.get("is_error") is True
    assert "level_ref" in r["content"].lower()


def _make_second_plan(tmp_path: Path) -> Path:
    """2e plan avec 2 épaisseurs : 20cm (commune avec le 1er) + 25cm (nouvelle)."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-WALL")
    msp = doc.modelspace()
    # 20cm à x=3.
    msp.add_line((3.0 - 0.10, 0.0), (3.0 - 0.10, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((3.0 + 0.10, 0.0), (3.0 + 0.10, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    # 25cm à x=7.
    msp.add_line((7.0 - 0.125, 0.0), (7.0 - 0.125, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    msp.add_line((7.0 + 0.125, 0.0), (7.0 + 0.125, 5.0),
                 dxfattribs={"layer": "A-WALL"})
    p = tmp_path / "plan_n1.dxf"
    doc.saveas(str(p))
    return p


# ----- dwg_extract_wall_thicknesses_many ---------------------------


def test_extract_thicknesses_many_aggregates_global(tmp_path, kg_fresh):
    plan1 = _make_multi_thickness_plan(tmp_path)  # 15, 20, 30
    plan2 = _make_second_plan(tmp_path)           # 20, 25
    r = llm_protocol.dispatch_tool_use(
        "dwg_extract_wall_thicknesses_many",
        {"file_paths": [str(plan1), str(plan2)]},
        "t1", kg_fresh,
    )
    assert not r.get("is_error"), r.get("content")
    p = json.loads(r["content"])
    assert len(p["per_file"]) == 2
    # Global distribution : 4 buckets distincts (15, 20, 25, 30).
    cms = p["distinct_buckets_cm"]
    assert cms == [15, 20, 25, 30]
    # Le bucket 20cm apparaît dans les 2 fichiers.
    bucket_20 = next(b for b in p["global_distribution"] if b["cm"] == 20)
    assert bucket_20["files_count"] == 2
    assert bucket_20["total_count"] == 2  # 1 mur 20cm par plan


def test_extract_thicknesses_many_rejects_empty_list(kg_fresh):
    r = llm_protocol.dispatch_tool_use(
        "dwg_extract_wall_thicknesses_many",
        {"file_paths": []},
        "t1", kg_fresh,
    )
    assert r.get("is_error") is True


# ----- dwg_import_walls_typed_many --------------------------------


def test_import_walls_typed_many_dedups_types_globally(tmp_path, kg_fresh):
    """2 plans avec un bucket 20cm commun → 1 seul type DXF_WALL_20cm créé."""
    n0 = kg_fresh.add_node("Level", {"name": "N0", "elevation": 0.0})
    n1 = kg_fresh.add_node("Level", {"name": "N1", "elevation": 3.0})
    plan1 = _make_multi_thickness_plan(tmp_path)  # 15, 20, 30
    plan2 = _make_second_plan(tmp_path)           # 20, 25

    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed_many",
        {
            "items": [
                {"file_path": str(plan1), "level_ref": n0, "height_m": 3.0},
                {"file_path": str(plan2), "level_ref": n1, "height_m": 3.0},
            ],
        },
        "t1", kg_fresh,
    )
    assert not r.get("is_error"), r.get("content")
    p = json.loads(r["content"])
    assert p["files_count"] == 2
    # 3 murs plan1 + 2 murs plan2 = 5 murs au total.
    assert p["walls_imported_total"] == 5
    # 4 types uniques créés (dédup global) : 15, 20, 25, 30.
    assert p["types_created"] == 4
    assert p["types_reused"] == 0
    # Distribution globale.
    dist = p["thickness_distribution_global"]
    assert dist == {"15cm": 1, "20cm": 2, "25cm": 1, "30cm": 1}
    # walls_per_file détaillé.
    assert p["walls_per_file"][str(plan1)] == 3
    assert p["walls_per_file"][str(plan2)] == 2
    # 4 types en KG, 5 walls en KG.
    type_names = {
        kg_fresh.get_node(nid).get("name")
        for nid in kg_fresh.find_by_type("WallType")
    }
    assert {"DXF_WALL_15cm", "DXF_WALL_20cm", "DXF_WALL_25cm", "DXF_WALL_30cm"} <= type_names
    assert len(list(kg_fresh.find_by_type("Wall"))) == 5


def test_import_walls_typed_many_rejects_unknown_level(tmp_path, kg_fresh):
    plan1 = _make_multi_thickness_plan(tmp_path)
    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed_many",
        {"items": [{"file_path": str(plan1), "level_ref": "bogus"}]},
        "t1", kg_fresh,
    )
    assert r.get("is_error") is True
    assert "level_ref" in r["content"].lower()


def test_import_walls_typed_many_rejects_empty_items(kg_fresh):
    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed_many",
        {"items": []},
        "t1", kg_fresh,
    )
    assert r.get("is_error") is True


# ----- Phase 2.5 : dwg_plan_openings (module pur) ------------------


import math as _math
from lib import dwg_plan_openings
from lib.dwg_classifier import WallCandidate
from lib.dwg_section_reader import SectionOpening


def test_perp_distance_basic():
    d = dwg_plan_openings._perp_distance_point_to_line(
        (1.0, 1.0), (0.0, 0.0), (10.0, 0.0),
    )
    assert d == pytest.approx(1.0)


def test_classify_opening_kind_door():
    # sill <= 0.15 et height >= 1.9 → door.
    assert dwg_plan_openings.classify_opening_kind(0.0, 2.1) == "door"
    assert dwg_plan_openings.classify_opening_kind(0.15, 2.1) == "door"
    # height trop courte → window.
    assert dwg_plan_openings.classify_opening_kind(0.0, 1.89) == "window"
    # sill trop haut → window.
    assert dwg_plan_openings.classify_opening_kind(0.16, 2.1) == "window"


def test_classify_opening_kind_unknown_when_missing():
    assert dwg_plan_openings.classify_opening_kind(None, 2.0) == "unknown"
    assert dwg_plan_openings.classify_opening_kind(0.9, None) == "unknown"


def test_merge_walls_no_openings_keeps_walls():
    walls = [
        WallCandidate(p1=(0.0, 0.0), p2=(10.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
    ]
    merged, openings = dwg_plan_openings.merge_walls_with_openings(walls, [])
    assert len(merged) == 1
    assert merged[0].source_indices == [0]
    assert openings == []


def test_merge_walls_two_fragments_with_aglaz_fuses():
    """2 fragments collinéaires séparés par un INSERT A-GLAZ width=1m →
    fusion + 1 opening hosted."""
    walls = [
        WallCandidate(p1=(0.0, 0.0), p2=(4.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
        WallCandidate(p1=(5.0, 0.0), p2=(10.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
    ]
    op = SectionOpening(
        block_name="Fenêtre - 1_00 m x 1_50 m -123-Niveau 0",
        block_id="123",
        x_dxf_m=4.5, y_dxf_m=0.0, rotation_deg=0.0,
        width_m=1.0, height_m=1.5,
    )
    merged, openings = dwg_plan_openings.merge_walls_with_openings(walls, [op])
    assert len(merged) == 1
    assert sorted(merged[0].source_indices) == [0, 1]
    assert merged[0].p1[0] == pytest.approx(0.0)
    assert merged[0].p2[0] == pytest.approx(10.0)
    assert len(openings) == 1
    ao = openings[0]
    assert ao.host_wall_index == 0
    assert ao.reason == "merged_two_fragments"
    assert ao.position_along_wall_m == pytest.approx(4.5)


def test_merge_no_fusion_if_no_width():
    walls = [
        WallCandidate(p1=(0.0, 0.0), p2=(4.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
        WallCandidate(p1=(5.0, 0.0), p2=(10.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
    ]
    op = SectionOpening(
        block_name="bloc-Niveau 0",
        block_id=None,
        x_dxf_m=4.5, y_dxf_m=0.0, rotation_deg=0.0,
        width_m=None, height_m=None,
    )
    merged, openings = dwg_plan_openings.merge_walls_with_openings(walls, [op])
    # Pas de fusion sans width parsable → 2 murs distincts.
    assert len(merged) == 2
    # L'opening tombe dans le gap entre les 2 murs → orphan.
    assert openings[0].host_wall_index is None
    assert openings[0].reason == "orphaned_no_match"


def test_merge_no_fusion_if_gap_mismatch_width():
    """Width=0.5 mais gap=1.0 → pas de fusion (width_match_tol par défaut 0.15)."""
    walls = [
        WallCandidate(p1=(0.0, 0.0), p2=(4.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
        WallCandidate(p1=(5.0, 0.0), p2=(10.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
    ]
    op = SectionOpening(
        block_name="Window-x-Niveau 0",
        block_id="x",
        x_dxf_m=4.5, y_dxf_m=0.0, rotation_deg=0.0,
        width_m=0.5, height_m=1.5,
    )
    merged, openings = dwg_plan_openings.merge_walls_with_openings(walls, [op])
    assert len(merged) == 2  # pas de fusion


def test_merge_opening_on_intact_wall_assigns_host():
    """Un INSERT A-GLAZ à l'intérieur d'un mur intact (pas de fragments) →
    l'opening est hosted sans fusion."""
    walls = [
        WallCandidate(p1=(0.0, 0.0), p2=(10.0, 0.0),
                      thickness=0.20, layer="A-WALL", confidence=1.0),
    ]
    op = SectionOpening(
        block_name="Window-y-Niveau 0",
        block_id="y",
        x_dxf_m=5.0, y_dxf_m=0.0, rotation_deg=0.0,
        width_m=1.0, height_m=1.5,
    )
    merged, openings = dwg_plan_openings.merge_walls_with_openings(walls, [op])
    assert len(merged) == 1
    assert openings[0].host_wall_index == 0
    assert openings[0].reason == "single_wall_intact"


# ----- Tool smoke : dwg_import_walls_and_openings_typed_many ------


def _make_plan_with_window_dxf(tmp_path: Path) -> Path:
    """Plan DXF : 2 fragments de mur (20cm) séparés par 1 INSERT A-GLAZ
    de largeur 1m. block_id=999, hauteur 1.5m dans le block name.

    Layout :
    - Mur frag 1 : x=0→4 (paire à y=-0.1/+0.1)
    - INSERT A-GLAZ au point (4.5, 0), block_name parsable.
    - Mur frag 2 : x=5→10 (paire à y=-0.1/+0.1)
    """
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-WALL")
    doc.layers.add("A-GLAZ")
    doc.layers.add("A-AREA-IDEN")
    msp = doc.modelspace()
    # Frag 1.
    msp.add_line((0.0, -0.1), (4.0, -0.1), dxfattribs={"layer": "A-WALL"})
    msp.add_line((0.0, 0.1), (4.0, 0.1), dxfattribs={"layer": "A-WALL"})
    # Frag 2.
    msp.add_line((5.0, -0.1), (10.0, -0.1), dxfattribs={"layer": "A-WALL"})
    msp.add_line((5.0, 0.1), (10.0, 0.1), dxfattribs={"layer": "A-WALL"})
    # Bloc fenêtre.
    blk_name = "Fenêtre - 1 Vantail - 1_00 m x 1_50 m - Appui aluminium-999-Niveau 0"
    blk = doc.blocks.new(name=blk_name)
    blk.add_circle((0, 0), 0.1, dxfattribs={"layer": "A-GLAZ"})
    msp.add_blockref(blk_name, (4.5, 0.0), dxfattribs={"layer": "A-GLAZ"})
    # Label plan.
    msp.add_mtext("Pièce 1", dxfattribs={
        "layer": "A-AREA-IDEN", "insert": (1, 1), "char_height": 0.2,
    })
    p = tmp_path / "plan_with_window.dxf"
    doc.saveas(str(p))
    return p


def _make_coupe_with_matching_window(tmp_path: Path) -> Path:
    """Coupe DXF avec un INSERT A-GLAZ de même block_id=999.

    Niveau 0 à y=0 (A-FLOR-LEVL). INSERT A-GLAZ à (x_cut=4.5, y=0.9)
    → sill = 0.9 - 0 = 0.9m, height = 1.5m (depuis block name).
    Donc kind = "window" (sill > 0.15).
    """
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    for ly in ("A-WALL", "A-GLAZ", "A-FLOR-LEVL"):
        doc.layers.add(ly)
    msp = doc.modelspace()
    # Niveau 0 sur A-FLOR-LEVL.
    msp.add_line((0.0, 0.0), (10.0, 0.0), dxfattribs={"layer": "A-FLOR-LEVL"})
    # Bloc fenêtre coupe.
    blk_name = "Fenêtre - 1 Vantail - 1_00 m x 1_50 m - Appui aluminium-999-Coupe 1"
    blk = doc.blocks.new(name=blk_name)
    blk.add_circle((0, 0), 0.1, dxfattribs={"layer": "A-GLAZ"})
    msp.add_blockref(blk_name, (4.5, 0.9), dxfattribs={"layer": "A-GLAZ"})
    p = tmp_path / "coupe_with_window.dxf"
    doc.saveas(str(p))
    return p


def test_import_walls_and_openings_typed_many_fuses_and_creates_window(
    tmp_path, kg_fresh,
):
    """Smoke : 2 fragments + A-GLAZ + coupe match → 1 mur fusionné + 1 fenêtre."""
    plan = _make_plan_with_window_dxf(tmp_path)
    coupe = _make_coupe_with_matching_window(tmp_path)
    level_id = kg_fresh.add_node("Level", {"name": "N0", "elevation": 0.0})
    # FamilyType Window minimum dans le KG.
    fam_window = kg_fresh.add_node("FamilyType", {
        "family_name": "Fenêtre", "type_name": "Std", "category": "Windows",
    })

    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_and_openings_typed_many",
        {
            "items": [{"file_path": str(plan), "level_ref": level_id,
                       "height_m": 3.0}],
            "coupe_paths": [str(coupe)],
        },
        "t1", kg_fresh,
    )
    assert not r.get("is_error"), r.get("content")
    p = json.loads(r["content"])
    assert p["walls_imported_total"] == 1  # fusion 2 → 1
    assert p["walls_merged_count"] == 1
    assert p["openings_windows_created"] == 1
    assert p["openings_doors_created"] == 0
    assert p["openings_unmatched_count"] == 0
    assert p["openings_orphan_count"] == 0
    # KG : 1 Wall + 1 Window.
    walls = list(kg_fresh.find_by_type("Wall"))
    windows = list(kg_fresh.find_by_type("Window"))
    assert len(walls) == 1
    assert len(windows) == 1


def test_import_walls_and_openings_typed_many_unmatched_when_no_coupe(
    tmp_path, kg_fresh,
):
    """Sans coupe match : opening reste unmatched (sill inconnu), mur quand
    même fusionné."""
    plan = _make_plan_with_window_dxf(tmp_path)
    level_id = kg_fresh.add_node("Level", {"name": "N0", "elevation": 0.0})
    fam_window = kg_fresh.add_node("FamilyType", {
        "family_name": "Fenêtre", "type_name": "Std", "category": "Windows",
    })

    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_and_openings_typed_many",
        {
            "items": [{"file_path": str(plan), "level_ref": level_id,
                       "height_m": 3.0}],
            "coupe_paths": [],
        },
        "t1", kg_fresh,
    )
    p = json.loads(r["content"])
    assert p["walls_imported_total"] == 1
    assert p["walls_merged_count"] == 1
    assert p["openings_windows_created"] == 0
    assert p["openings_doors_created"] == 0
    assert p["openings_unmatched_count"] == 1  # pas de match coupe


def test_import_walls_typed_refuses_section_dxf(tmp_path, kg_fresh):
    """Le tool doit refuser un DXF de coupe (pas un plan)."""
    level_id = kg_fresh.add_node("Level", {"name": "N0", "elevation": 0.0})
    # DXF coupe : A-FLOR-LEVL + A-WALL mais pas A-AREA-IDEN.
    c_doc = ezdxf.new("R2018")
    c_doc.header["$INSUNITS"] = 6
    for ly in ("A-WALL", "A-FLOR-LEVL"):
        c_doc.layers.add(ly)
    msp = c_doc.modelspace()
    msp.add_line((4.9, 0.0), (4.9, 3.0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((5.1, 0.0), (5.1, 3.0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((0.0, 0.0), (10.0, 0.0), dxfattribs={"layer": "A-FLOR-LEVL"})
    coupe = tmp_path / "coupe.dxf"
    c_doc.saveas(str(coupe))

    r = llm_protocol.dispatch_tool_use(
        "dwg_import_walls_typed",
        {"file_path": str(coupe), "level_ref": level_id, "height_m": 3.0},
        "t1", kg_fresh,
    )
    assert r.get("is_error") is True
    assert "coupe" in r["content"].lower() or "section" in r["content"].lower()
