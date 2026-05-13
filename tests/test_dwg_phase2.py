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
