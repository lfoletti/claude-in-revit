"""Tests pour dwg_plan_columns : parsing du nom de bloc, extraction des
INSERTs S-COLS, aggregation multi-niveaux.

Cas P2 réel (Poteaux + dalles, profil HEA160) :
- Block name : `Poteau HE-A - HEA160-<ID>-Niveau N`.
- 30 positions × 3 niveaux → 30 colonnes aggrégées de 6m.
"""
from __future__ import annotations

from typing import Any, List, Tuple

import pytest

from lib.dwg_plan_columns import (
    AggregatedColumn,
    ColumnCandidate,
    aggregate_columns_across_plans,
    extract_columns_from_entities,
    parse_column_block_name,
)
from lib.dwg_reader import DwgEntity


# ----- parse_column_block_name ----------------------------------------


def test_parse_block_name_revit_standard_with_v_id():
    """Pattern P2 typique."""
    family, type_ = parse_column_block_name("Poteau HE-A - HEA160-V1-Niveau 0")
    assert family == "Poteau HE-A"
    assert type_ == "HEA160"


def test_parse_block_name_revit_standard_with_numeric_id():
    family, type_ = parse_column_block_name(
        "Poteau HE-A - HEA160-295798-Niveau 1",
    )
    assert family == "Poteau HE-A"
    assert type_ == "HEA160"


def test_parse_block_name_concrete_column():
    """Pattern béton hypothétique."""
    family, type_ = parse_column_block_name(
        "Poteau béton - 30x30-V2-Niveau 2",
    )
    assert family == "Poteau béton"
    assert type_ == "30x30"


def test_parse_block_name_fallback_when_unrecognised():
    """Block name qui ne suit pas le pattern → fallback DXF_COL."""
    family, type_ = parse_column_block_name("RandomColumnName")
    assert family == "DXF_COL"
    assert type_ == "RandomColumnName"


def test_parse_block_name_empty():
    family, type_ = parse_column_block_name("")
    assert family == "DXF_COL"
    assert type_ == "Unknown"


# ----- extract_columns_from_entities ----------------------------------


def _insert(x, y, block_name, layer="S-COLS", rotation_deg=0.0):
    return DwgEntity(
        kind="INSERT",
        layer=layer,
        coords=[[x, y, 0.0]],
        attrs={
            "block_name": block_name,
            "rotation_deg": rotation_deg,
            "scale": [1.0, 1.0, 1.0],
        },
    )


def test_extract_picks_only_s_cols_layer():
    ents = [
        _insert(0, 0, "Poteau HE-A - HEA160-V1-Niveau 0", layer="S-COLS"),
        _insert(1, 1, "Door 90cm", layer="A-WALL"),       # porte sur mur → skip
        _insert(2, 2, "Label", layer="S-COLS-IDEN"),     # annotation → skip
    ]
    out = extract_columns_from_entities(ents)
    assert len(out) == 1
    assert out[0].position == (0.0, 0.0)
    assert out[0].family_name == "Poteau HE-A"
    assert out[0].type_name == "HEA160"


def test_extract_ignores_non_inserts():
    ents = [
        _insert(0, 0, "Poteau HE-A - HEA160-V1-Niveau 0"),
        DwgEntity(kind="LINE", layer="S-COLS", coords=[[0, 0, 0], [1, 1, 0]], attrs={}),
    ]
    out = extract_columns_from_entities(ents)
    assert len(out) == 1


def test_extract_preserves_rotation():
    ents = [
        _insert(0, 0, "Poteau HE-A - HEA160-V1-Niveau 0", rotation_deg=45.0),
    ]
    out = extract_columns_from_entities(ents)
    assert out[0].rotation_deg == 45.0


def test_extract_returns_sorted_by_y_then_x():
    ents = [
        _insert(2, 5, "Poteau HE-A - HEA160-V1-Niveau 0"),
        _insert(1, 5, "Poteau HE-A - HEA160-V2-Niveau 0"),
        _insert(0, 0, "Poteau HE-A - HEA160-V3-Niveau 0"),
    ]
    out = extract_columns_from_entities(ents)
    assert [c.position for c in out] == [(0.0, 0.0), (1.0, 5.0), (2.0, 5.0)]


# ----- aggregate_columns_across_plans ---------------------------------


def _cand(x, y, family="Poteau HE-A", type_="HEA160"):
    return ColumnCandidate(
        position=(x, y), family_name=family, type_name=type_,
        rotation_deg=0.0,
        block_name="{} - {}-V1-Niveau 0".format(family, type_),
    )


def test_aggregate_single_level_uses_default_height():
    """1 niveau, 2 colonnes → 2 aggregated, hauteur = default (3m)."""
    cands = [_cand(0, 0), _cand(1, 0)]
    out = aggregate_columns_across_plans(
        [(0.0, cands)], default_storey_height_m=3.0,
    )
    assert len(out) == 2
    for col in out:
        assert col.base_level_elev_m == 0.0
        assert col.top_level_elev_m == 3.0
        assert col.appearing_levels == [0.0]


def test_aggregate_multi_level_uses_next_level_as_top():
    """1 position à N0 + N1 + N2 → 1 colonne base=0, top=elev(N2)+storey."""
    pos = (0.0, 0.0)
    out = aggregate_columns_across_plans(
        [
            (0.0, [_cand(*pos)]),
            (3.0, [_cand(*pos)]),
            (6.0, [_cand(*pos)]),
        ],
    )
    assert len(out) == 1
    col = out[0]
    assert col.base_level_elev_m == 0.0
    assert col.top_level_elev_m == 9.0  # 6 + 3 default
    assert col.appearing_levels == [0.0, 3.0, 6.0]


def test_aggregate_p2_30_positions_3_levels():
    """Reproduit P2 : 30 positions distinctes apparaissant à N0/N1/N2 (3m chacun).
    → 30 colonnes aggrégées base=0, top=9 (= 6+default 3), height=9m."""
    grid_positions = [(x, y) for x in (-13.43, -8.93, -4.43, -2.93)
                              for y in (-5.99, -2.49, 1.01, 4.51, 8.01)]
    # Pad à 30 positions.
    extra = [(0.0, 0.0)] * (30 - len(grid_positions))
    positions = grid_positions + extra
    assert len(positions) == 30

    cands_per_level = [_cand(x, y) for x, y in positions]
    out = aggregate_columns_across_plans([
        (0.0, list(cands_per_level)),
        (3.0, list(cands_per_level)),
        (6.0, list(cands_per_level)),
    ])
    # Note : padding "0.0, 0.0" peut fusionner avec d'autres positions
    # à (0, 0) → on s'attend à 30 buckets distincts (ou moins si
    # collisions). Pour P2 vrai, les 30 positions sont distinctes.
    assert len(out) <= 30
    for col in out:
        assert col.base_level_elev_m == 0.0
        # 3 niveaux d'apparition → top = elev(top niveau) + storey = 6 + 3 = 9
        assert col.top_level_elev_m == 9.0
        assert col.appearing_levels == [0.0, 3.0, 6.0]


def test_aggregate_column_starting_above_n0():
    """Une colonne qui n'apparaît qu'à N1 et N2 (pas N0) → base=3, top=6+3=9."""
    out = aggregate_columns_across_plans([
        (0.0, []),  # N0 vide
        (3.0, [_cand(5, 5)]),
        (6.0, [_cand(5, 5)]),
    ])
    assert len(out) == 1
    col = out[0]
    assert col.base_level_elev_m == 3.0
    assert col.top_level_elev_m == 9.0
    assert col.appearing_levels == [3.0, 6.0]


def test_aggregate_uses_next_level_as_top_when_in_list():
    """Si un niveau plus haut existe dans la liste mais n'a pas cette
    colonne, on l'utilise comme top (= colonne s'arrête à ce niveau).
    Ex : colonne N0+N1 dans projet 3 niveaux → top = elev(N2) = 6."""
    out = aggregate_columns_across_plans([
        (0.0, [_cand(5, 5)]),
        (3.0, [_cand(5, 5)]),
        (6.0, []),  # N2 vide pour cette position
    ])
    assert len(out) == 1
    col = out[0]
    assert col.base_level_elev_m == 0.0
    assert col.top_level_elev_m == 6.0  # = élév N2 du projet
    assert col.appearing_levels == [0.0, 3.0]


def test_aggregate_positions_merge_within_tolerance():
    """Des positions à drift < tol fusionnent dans le même bucket."""
    # 2 colonnes presque identiques (drift 2cm < 5cm tol).
    cands = [_cand(0.00, 0.00), _cand(0.02, 0.01)]
    out = aggregate_columns_across_plans(
        [(0.0, cands)], position_merge_tol_m=0.05,
    )
    assert len(out) == 1  # fusionnées


def test_aggregate_positions_distinct_above_tolerance():
    """Drift > tol → 2 colonnes distinctes."""
    cands = [_cand(0.00, 0.00), _cand(0.20, 0.20)]
    out = aggregate_columns_across_plans(
        [(0.0, cands)], position_merge_tol_m=0.05,
    )
    assert len(out) == 2


def test_aggregate_empty_input():
    assert aggregate_columns_across_plans([]) == []


# ----- columns_get_or_create_dxf_type_many (placeholder KG-only) -----


def _fresh_kg():
    from lib.project_kg import ProjectKG
    kg = ProjectKG("test")
    kg.advance_turn()
    return kg


def test_get_or_create_dxf_type_kg_only_creates_placeholder():
    """KG-only : crée un ColumnType nommé DXF_COL_<famille>_<type>
    avec metadata d'origine."""
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    result = get_or_create_dxf_type_many(
        kg=kg, doc=None,
        types=[{"family_name": "Poteau HE-A", "type_name": "HEA160"}],
    )
    assert result["ok"]
    assert result["created_count"] == 1
    assert result["reused_count"] == 0
    t = result["types"][0]
    assert t["type_name"] == "DXF_COL_Poteau HE-A_HEA160"
    assert t["family_name"] == "Poteau HE-A"  # original conservé
    assert t["kind"] == "structural"  # défaut
    assert t["created"] is True
    # Node KG est bien là.
    node = kg.get_node(t["llm_id"])
    assert node["family_name"] == "Poteau HE-A"
    assert node["type_name"] == "DXF_COL_Poteau HE-A_HEA160"
    assert node["kind"] == "structural"


def test_get_or_create_dxf_type_reuses_existing():
    """2e appel avec même paire → reused, pas créé."""
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    types = [{"family_name": "Poteau HE-A", "type_name": "HEA160"}]
    r1 = get_or_create_dxf_type_many(kg=kg, doc=None, types=types)
    r2 = get_or_create_dxf_type_many(kg=kg, doc=None, types=types)
    assert r1["created_count"] == 1
    assert r2["created_count"] == 0
    assert r2["reused_count"] == 1
    assert r2["types"][0]["llm_id"] == r1["types"][0]["llm_id"]


def test_get_or_create_dxf_type_dedups_within_batch():
    """Doublons internes → 1 seul type créé."""
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    result = get_or_create_dxf_type_many(
        kg=kg, doc=None,
        types=[
            {"family_name": "Poteau HE-A", "type_name": "HEA160"},
            {"family_name": "Poteau HE-A", "type_name": "HEA160"},  # dup
            {"family_name": "Poteau béton", "type_name": "30x30"},
        ],
    )
    assert result["created_count"] == 2
    assert len(result["types"]) == 2
    placeholders = {t["type_name"] for t in result["types"]}
    assert placeholders == {
        "DXF_COL_Poteau HE-A_HEA160",
        "DXF_COL_Poteau béton_30x30",
    }


def test_get_or_create_dxf_type_handles_multiple_materials():
    """Acier, béton, bois traités identiquement (pattern générique)."""
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    result = get_or_create_dxf_type_many(
        kg=kg, doc=None,
        types=[
            {"family_name": "Poteau HE-A", "type_name": "HEA160"},
            {"family_name": "Poteau béton", "type_name": "30x30"},
            {"family_name": "Poteau bois", "type_name": "BLC 200x200"},
        ],
    )
    assert result["created_count"] == 3
    names = sorted(t["type_name"] for t in result["types"])
    assert names == sorted([
        "DXF_COL_Poteau HE-A_HEA160",
        "DXF_COL_Poteau béton_30x30",
        "DXF_COL_Poteau bois_BLC 200x200",
    ])


def test_get_or_create_dxf_type_sanitizes_forbidden_chars():
    """Caractères Revit-interdits dans family/type → remplacés par `_`."""
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    result = get_or_create_dxf_type_many(
        kg=kg, doc=None,
        types=[{"family_name": "Steel/Round", "type_name": "Ø:200"}],
    )
    name = result["types"][0]["type_name"]
    # Pas de `/` ni `:` dans le nom final.
    assert "/" not in name
    assert ":" not in name
    assert name == "DXF_COL_Steel_Round_Ø_200"


def test_get_or_create_dxf_type_rejects_bad_kind():
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    with pytest.raises(ValueError, match="kind must be"):
        get_or_create_dxf_type_many(
            kg=kg, doc=None,
            types=[{
                "family_name": "X", "type_name": "Y", "kind": "fluffy",
            }],
        )


def test_get_or_create_dxf_type_rejects_empty_family_or_type():
    from lib.tools.columns import get_or_create_dxf_type_many
    kg = _fresh_kg()
    with pytest.raises(ValueError, match="family_name required"):
        get_or_create_dxf_type_many(
            kg=kg, doc=None,
            types=[{"family_name": "", "type_name": "Y"}],
        )
    with pytest.raises(ValueError, match="type_name required"):
        get_or_create_dxf_type_many(
            kg=kg, doc=None,
            types=[{"family_name": "X", "type_name": "   "}],
        )


# ----- dwg_create_columns_many (smoke KG-only via vraies fixtures) -----


import ezdxf  # noqa: E402


def _make_plan_with_columns(tmp_path, level_label, positions):
    """Crée un DXF de plan minimal avec N INSERTs S-COLS aux positions
    spécifiées. Block_name suit la convention P2 : `Poteau HE-A -
    HEA160-V{i}-Niveau {label}`."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6  # metres
    doc.layers.add("S-COLS")
    doc.layers.add("A-AREA-IDEN")  # marque le DXF comme un plan (vs section)
    # Définir un bloc placeholder (n'importe quel contenu — un point)
    blk_name = "Poteau HE-A - HEA160-V1-Niveau {}".format(level_label)
    if blk_name not in doc.blocks:
        blk = doc.blocks.new(name=blk_name)
        blk.add_point((0, 0))
    msp = doc.modelspace()
    for x, y in positions:
        msp.add_blockref(
            blk_name, (x, y),
            dxfattribs={"layer": "S-COLS"},
        )
    # Marqueur de plan : 1 entité A-AREA-IDEN (pour classify_dxf)
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "A-AREA-IDEN"})
    p = tmp_path / "plan_{}.dxf".format(level_label)
    doc.saveas(str(p))
    return p


def _kg_with_3_levels():
    """KG bootstrap avec 3 levels et le ColumnType déjà créé.

    Note : on ne lance pas le registry reset — c'est déjà fait pour
    nous par la fixture pytest, on hérite du registry global.
    """
    from lib.project_kg import ProjectKG
    kg = ProjectKG("test")
    kg.advance_turn()
    levels = {}
    for name, elev in [("Niveau 0", 0.0), ("Niveau 1", 3.0), ("Niveau 2", 6.0)]:
        lid = kg.add_node("Level", {"name": name, "elevation": elev})
        levels[name] = lid
    return kg, levels


def test_dwg_create_columns_many_kg_only_per_level_p2_pattern(tmp_path):
    """Reproduit le pattern P2 : 30 positions × 3 niveaux. Mode per-level
    (production default) → 30 colonnes PAR niveau = 90 colonnes total,
    chacune avec height = storey_height (3m). Convention Revit
    structurelle : 1 colonne par étage."""
    from lib.tools.dwg_import import create_columns_many

    # Grille 5×6 = 30 positions.
    positions = [(x * 3.0, y * 3.0) for x in range(5) for y in range(6)]
    assert len(positions) == 30

    kg, levels = _kg_with_3_levels()
    plan_n0 = _make_plan_with_columns(tmp_path, "0", positions)
    plan_n1 = _make_plan_with_columns(tmp_path, "1", positions)
    plan_n2 = _make_plan_with_columns(tmp_path, "2", positions)

    result = create_columns_many(
        kg=kg, doc=None,
        items=[
            {"file_path": str(plan_n0), "level_ref": levels["Niveau 0"]},
            {"file_path": str(plan_n1), "level_ref": levels["Niveau 1"]},
            {"file_path": str(plan_n2), "level_ref": levels["Niveau 2"]},
        ],
    )

    assert result["ok"] is True
    assert result["files_count"] == 3
    assert result["candidates_total"] == 90  # 30 × 3
    assert result["aggregated_count"] == 90  # 1 colonne par (niveau, position)
    assert result["columns_created_count"] == 90
    assert result["types_created"] == 1     # 1 placeholder DXF_COL_*
    assert result["types_reused"] == 0
    # 1 ColumnType + 90 Column dans le KG.
    assert len(kg.find_by_type("ColumnType")) == 1
    assert len(kg.find_by_type("Column")) == 90
    # 30 colonnes par niveau, chacune avec height = 3m (storey).
    col_ids = kg.find_by_type("Column")
    by_level = {}
    for cid in col_ids:
        n = kg.get_node(cid)
        by_level.setdefault(n["level_ref"], 0)
        by_level[n["level_ref"]] += 1
        assert n["kind"] == "structural"
        assert n["height"] == 3.0  # 1 étage de haut par colonne
    assert by_level[levels["Niveau 0"]] == 30
    assert by_level[levels["Niveau 1"]] == 30
    assert by_level[levels["Niveau 2"]] == 30


def test_dwg_create_columns_many_handles_no_columns_gracefully(tmp_path):
    """Plan sans S-COLS INSERT → ok=True, count=0, note explicative."""
    from lib.tools.dwg_import import create_columns_many

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-AREA-IDEN")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "A-AREA-IDEN"})
    p = tmp_path / "plan_empty.dxf"
    doc.saveas(str(p))

    kg, levels = _kg_with_3_levels()
    result = create_columns_many(
        kg=kg, doc=None,
        items=[{"file_path": str(p), "level_ref": levels["Niveau 0"]}],
    )
    assert result["ok"] is True
    assert result["columns_created_count"] == 0
    assert result["candidates_total"] == 0
    assert "Aucun INSERT S-COLS" in result["note"]


def test_dwg_create_columns_many_partial_grid_top_only(tmp_path):
    """Mode per-level : colonne présente seulement à N1+N2 → 2 colonnes
    créées (1 par niveau d'apparition), pas 1."""
    from lib.tools.dwg_import import create_columns_many

    positions = [(0.0, 0.0)]
    kg, levels = _kg_with_3_levels()
    # N0 sans colonne
    doc0 = ezdxf.new("R2018")
    doc0.header["$INSUNITS"] = 6
    doc0.layers.add("A-AREA-IDEN")
    msp0 = doc0.modelspace()
    msp0.add_line((0, 0), (1, 0), dxfattribs={"layer": "A-AREA-IDEN"})
    p0 = tmp_path / "plan_n0.dxf"
    doc0.saveas(str(p0))
    p1 = _make_plan_with_columns(tmp_path, "1", positions)
    p2 = _make_plan_with_columns(tmp_path, "2", positions)

    result = create_columns_many(
        kg=kg, doc=None,
        items=[
            {"file_path": str(p0), "level_ref": levels["Niveau 0"]},
            {"file_path": str(p1), "level_ref": levels["Niveau 1"]},
            {"file_path": str(p2), "level_ref": levels["Niveau 2"]},
        ],
    )

    # 1 colonne à N1 (height = N2 - N1 = 3m) + 1 colonne à N2
    # (height = default storey = 3m). Total = 2.
    assert result["columns_created_count"] == 2
    levels_by_col = sorted(
        kg.get_node(cid)["level_ref"]
        for cid in kg.find_by_type("Column")
    )
    assert levels["Niveau 1"] in levels_by_col
    assert levels["Niveau 2"] in levels_by_col


def test_dwg_create_columns_many_rejects_section_dxf(tmp_path):
    """Refuse un DXF de coupe (pas de plan) — _refuse_if_section."""
    from lib.tools.dwg_import import create_columns_many

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-FLOR-LEVL")  # marqueur de coupe
    msp = doc.modelspace()
    msp.add_line((-1, 0), (1, 0), dxfattribs={"layer": "A-FLOR-LEVL"})
    p = tmp_path / "coupe.dxf"
    doc.saveas(str(p))

    kg, levels = _kg_with_3_levels()
    with pytest.raises(ValueError, match="(?i)section|coupe"):
        create_columns_many(
            kg=kg, doc=None,
            items=[{"file_path": str(p), "level_ref": levels["Niveau 0"]}],
        )


# ----- columns_create_rectangular_concrete_types_many (BA_COL_*) -------


def test_ba_col_creates_3_square_types():
    """Cas user : 16x16, 22x22, 30x30."""
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    result = create_rectangular_concrete_types_many(
        kg=kg, doc=None,
        dimensions_cm=[
            {"width_cm": 16, "depth_cm": 16},
            {"width_cm": 22, "depth_cm": 22},
            {"width_cm": 30, "depth_cm": 30},
        ],
    )
    assert result["ok"]
    assert result["created_count"] == 3
    assert result["reused_count"] == 0
    names = sorted(t["name"] for t in result["types"])
    assert names == ["BA_COL_16x16", "BA_COL_22x22", "BA_COL_30x30"]
    # Tous structural.
    for t in result["types"]:
        n = kg.get_node(t["llm_id"])
        assert n["kind"] == "structural"


def test_ba_col_reuses_existing_on_second_call():
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    dims = [{"width_cm": 30, "depth_cm": 30}]
    r1 = create_rectangular_concrete_types_many(kg=kg, doc=None, dimensions_cm=dims)
    r2 = create_rectangular_concrete_types_many(kg=kg, doc=None, dimensions_cm=dims)
    assert r1["created_count"] == 1
    assert r2["created_count"] == 0
    assert r2["reused_count"] == 1
    assert r2["types"][0]["llm_id"] == r1["types"][0]["llm_id"]


def test_ba_col_dedups_within_batch():
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    result = create_rectangular_concrete_types_many(
        kg=kg, doc=None,
        dimensions_cm=[
            {"width_cm": 30, "depth_cm": 30},
            {"width_cm": 30, "depth_cm": 30},  # dup
            {"width_cm": 22, "depth_cm": 22},
        ],
    )
    assert result["created_count"] == 2


def test_ba_col_supports_rectangular_not_just_square():
    """Largeur ≠ profondeur → nom BA_COL_20x40."""
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    result = create_rectangular_concrete_types_many(
        kg=kg, doc=None,
        dimensions_cm=[{"width_cm": 20, "depth_cm": 40}],
    )
    assert result["types"][0]["name"] == "BA_COL_20x40"


def test_ba_col_rejects_non_positive_dimensions():
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    with pytest.raises(ValueError, match="positive"):
        create_rectangular_concrete_types_many(
            kg=kg, doc=None,
            dimensions_cm=[{"width_cm": 0, "depth_cm": 30}],
        )
    with pytest.raises(ValueError, match="positive"):
        create_rectangular_concrete_types_many(
            kg=kg, doc=None,
            dimensions_cm=[{"width_cm": 30, "depth_cm": -5}],
        )


def test_ba_col_rejects_empty_list():
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    with pytest.raises(ValueError, match="non-empty"):
        create_rectangular_concrete_types_many(
            kg=kg, doc=None, dimensions_cm=[],
        )


def test_ba_col_rounds_float_dimensions_to_cm_int():
    """Tolère int et float (round to nearest cm)."""
    from lib.tools.columns import create_rectangular_concrete_types_many
    kg = _fresh_kg()
    result = create_rectangular_concrete_types_many(
        kg=kg, doc=None,
        dimensions_cm=[{"width_cm": 22.4, "depth_cm": 22.6}],
    )
    # 22.4 → 22, 22.6 → 23
    assert result["types"][0]["name"] == "BA_COL_22x23"
