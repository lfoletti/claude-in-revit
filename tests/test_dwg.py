"""Tests UC1 DWG ingest : dwg_reader + dwg_classifier + tools/dwg_import.

Fixtures DXF générées programmatiquement via ezdxf à l'exécution (pas
de binaires versionnés). Couvre :
- parsing DXF, unités, layers
- heuristique de nom (suggest_layer_role)
- pair detection (4 murs orthogonaux → 4 walls)
- classification end-to-end (mapping + détection)
- import_walls roundtrip (KG-only, doc=None)

Pas de test DWG (nécessiterait ODA File Converter installé sur la
machine de test).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

# ezdxf est marqué optionnel pour l'instant — skip propre si absent (mais
# il est installé en venv depuis cette session).
ezdxf = pytest.importorskip("ezdxf")

from lib import dwg_classifier, dwg_reader, llm_protocol
from lib.dwg_classifier import (
    Segment,
    WallCandidate,
    detect_wall_segments,
    extract_straight_segments,
    suggest_layer_role,
)
from lib.dwg_reader import DwgEntity
from lib.project_kg import ProjectKG


# ----- Fixtures DXF synthétiques ---------------------------------------


def _make_rectangle_room_dxf(
    tmp_path: Path,
    *,
    width_m: float = 5.0,
    depth_m: float = 4.0,
    wall_thickness_m: float = 0.20,
    units_code: int = 6,   # 6 = mètres (les coords seront déjà en m)
    wall_layer: str = "WALL",
    extras: bool = False,
) -> Path:
    """Génère un DXF avec 4 murs orthogonaux (8 lignes parallèles paire).

    Layout : pièce rectangulaire centrée sur l'origine, faces externes
    aux bords (0,0)→(width, depth), faces internes décalées de
    `wall_thickness_m` vers l'intérieur.

    Si `extras=True`, ajoute :
    - 1 ligne orpheline (sans paire) sur le layer WALL
    - 1 ligne sur un layer FURNITURE (à ignorer)
    - 1 INSERT sur un layer DOOR (porte schématique)
    """
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = units_code
    msp = doc.modelspace()

    # Layers.
    doc.layers.add(wall_layer)
    if extras:
        doc.layers.add("FURNITURE")
        doc.layers.add("DOOR")

    w = width_m
    d = depth_m
    t = wall_thickness_m
    # Faces externes (4 lignes).
    msp.add_line((0, 0), (w, 0), dxfattribs={"layer": wall_layer})  # sud ext
    msp.add_line((w, 0), (w, d), dxfattribs={"layer": wall_layer})  # est ext
    msp.add_line((w, d), (0, d), dxfattribs={"layer": wall_layer})  # nord ext
    msp.add_line((0, d), (0, 0), dxfattribs={"layer": wall_layer})  # ouest ext
    # Faces internes (4 lignes parallèles décalées de t).
    msp.add_line((t, t), (w - t, t), dxfattribs={"layer": wall_layer})        # sud int
    msp.add_line((w - t, t), (w - t, d - t), dxfattribs={"layer": wall_layer}) # est int
    msp.add_line((w - t, d - t), (t, d - t), dxfattribs={"layer": wall_layer}) # nord int
    msp.add_line((t, d - t), (t, t), dxfattribs={"layer": wall_layer})         # ouest int

    if extras:
        msp.add_line((10.0, 10.0), (11.0, 10.0), dxfattribs={"layer": wall_layer})  # orpheline
        msp.add_line((1.0, 1.0), (2.0, 1.0), dxfattribs={"layer": "FURNITURE"})

    path = tmp_path / "test_room.dxf"
    doc.saveas(str(path))
    return path


# ----- dwg_reader -------------------------------------------------------


def test_reader_parses_simple_room(tmp_path):
    """4 lignes externes + 4 lignes internes = 8 entités LINE."""
    path = _make_rectangle_room_dxf(tmp_path)
    entities, meta = dwg_reader.parse(path)
    assert meta["source_format"] == "dxf"
    assert meta["units_code"] == 6  # mètres
    assert meta["units_factor_to_m"] == 1.0
    assert meta["total_entities"] == 8
    assert all(e.kind == "LINE" for e in entities)
    assert all(e.layer == "WALL" for e in entities)


def test_reader_applies_mm_unit_conversion(tmp_path):
    """Si $INSUNITS = 4 (mm), les coords doivent être divisées par 1000."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4  # mm
    msp = doc.modelspace()
    doc.layers.add("WALL")
    # Mur de 5000 mm = 5 m après conversion.
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "WALL"})
    path = tmp_path / "mm.dxf"
    doc.saveas(str(path))

    entities, meta = dwg_reader.parse(path)
    assert meta["units_factor_to_m"] == 0.001
    line = entities[0]
    assert line.coords[0] == [0.0, 0.0, 0.0]
    assert math.isclose(line.coords[1][0], 5.0, abs_tol=1e-9)


def test_reader_scale_override(tmp_path):
    """scale_override s'applique en plus du $INSUNITS factor."""
    path = _make_rectangle_room_dxf(tmp_path, units_code=6)
    entities, meta = dwg_reader.parse(path, scale_override=2.0)
    # base_factor=1.0 (m), scale_override=2.0 → x2.
    assert meta["units_factor_to_m"] == 2.0


def test_reader_dwg_without_oda_raises_configerror(tmp_path, monkeypatch):
    """Si on présente un .dwg et ODA non installé, ConfigError actionable."""
    from lib import config
    monkeypatch.setattr(config, "oda_converter_path", lambda: None)
    fake_dwg = tmp_path / "fake.dwg"
    fake_dwg.write_bytes(b"not really a DWG")
    with pytest.raises(config.ConfigError, match="ODA File Converter"):
        dwg_reader.parse(fake_dwg)


# ----- dwg_classifier : suggest_layer_role -----------------------------


@pytest.mark.parametrize("name,expected", [
    ("WALL", "wall"),
    ("A-WALL-EXTR", "wall"),
    ("MUR", "wall"),
    ("MURS_PORTEUR", "wall"),
    ("M01", "wall"),
    ("CLOISONS", "wall"),
    ("DOOR", "door"),
    ("PORTES", "door"),
    ("OUVR", "door"),
    ("A-WIND-FRAME", "window"),
    ("WINDOWS", "window"),
    ("FENETRE", "window"),
    ("FENÊTRES", "window"),
    ("TEXT", "text"),
    ("COTES", "text"),
    ("DIM", "text"),
    ("MOBILIER", "ignore"),
    ("FURNITURE", "ignore"),
    ("HATCHES", "ignore"),
    ("0", None),  # layer DXF par défaut, pas de match
    ("RANDOM_NAME", None),
])
def test_suggest_layer_role(name, expected):
    assert suggest_layer_role(name) == expected


def test_annotate_layers_mutates_in_place():
    layers = [
        {"name": "WALL", "entity_count": 8},
        {"name": "FURNITURE", "entity_count": 2},
        {"name": "RANDOM", "entity_count": 1},
    ]
    out = dwg_classifier.annotate_layers(layers)
    assert out is layers
    assert layers[0]["suggested_role"] == "wall"
    assert layers[1]["suggested_role"] == "ignore"
    assert layers[2]["suggested_role"] is None


# ----- dwg_classifier : pair detection ----------------------------------


def test_detect_wall_pair_orthogonal():
    """Deux lignes parallèles horizontales distantes de 0.2m → 1 wall."""
    segments = [
        Segment(p1=(0.0, 0.0), p2=(5.0, 0.0), layer="WALL"),
        Segment(p1=(0.0, 0.2), p2=(5.0, 0.2), layer="WALL"),
    ]
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 1
    assert len(rejected) == 0
    w = walls[0]
    assert math.isclose(w.thickness, 0.2, abs_tol=1e-9)
    # Centerline à y = 0.1.
    assert math.isclose(w.p1[1], 0.1, abs_tol=1e-9)
    assert math.isclose(w.p2[1], 0.1, abs_tol=1e-9)
    assert math.isclose(w.p1[0], 0.0, abs_tol=1e-9)
    assert math.isclose(w.p2[0], 5.0, abs_tol=1e-9)


def test_detect_wall_pair_rejects_too_far():
    """Distance > max_thickness → pas de pair, 2 orphelins."""
    segments = [
        Segment(p1=(0.0, 0.0), p2=(5.0, 0.0), layer="WALL"),
        Segment(p1=(0.0, 1.0), p2=(5.0, 1.0), layer="WALL"),  # 1 m
    ]
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 0
    assert len(rejected) == 2


def test_detect_wall_pair_rejects_non_parallel():
    """Lignes croisées (90°) → pas de pair."""
    segments = [
        Segment(p1=(0.0, 0.0), p2=(5.0, 0.0), layer="WALL"),
        Segment(p1=(2.0, -1.0), p2=(2.0, 1.0), layer="WALL"),
    ]
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 0
    assert len(rejected) == 2


def test_detect_wall_pair_rejects_no_overlap():
    """Parallèles à bonne distance mais sans overlap projeté → orphelins."""
    segments = [
        Segment(p1=(0.0, 0.0), p2=(1.0, 0.0), layer="WALL"),
        Segment(p1=(5.0, 0.2), p2=(6.0, 0.2), layer="WALL"),
    ]
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 0
    assert len(rejected) == 2


def test_detect_wall_pair_reverse_orientation():
    """La 2e ligne est dessinée en sens inverse → pair quand même reconnue."""
    segments = [
        Segment(p1=(0.0, 0.0), p2=(5.0, 0.0), layer="WALL"),
        Segment(p1=(5.0, 0.2), p2=(0.0, 0.2), layer="WALL"),  # reverse
    ]
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 1
    assert math.isclose(walls[0].thickness, 0.2, abs_tol=1e-9)


def test_detect_wall_pair_different_layers_no_match():
    """Lignes parallèles mais sur layers différents → pas de pair."""
    segments = [
        Segment(p1=(0.0, 0.0), p2=(5.0, 0.0), layer="WALL"),
        Segment(p1=(0.0, 0.2), p2=(5.0, 0.2), layer="ROOF"),
    ]
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 0


def test_detect_full_orthogonal_room(tmp_path):
    """Pièce rect 4 murs → 4 WallCandidate après extraction + détection."""
    path = _make_rectangle_room_dxf(tmp_path)
    entities, _ = dwg_reader.parse(path)
    segments = extract_straight_segments(entities, layer_filter=["WALL"])
    assert len(segments) == 8
    walls, rejected = detect_wall_segments(segments)
    assert len(walls) == 4
    assert len(rejected) == 0
    # Tous d'épaisseur ~ 0.20m.
    assert all(math.isclose(w.thickness, 0.20, abs_tol=1e-9) for w in walls)


# ----- dwg_classifier.classify (entry point) ----------------------------


def test_classify_with_mapping_yields_4_walls(tmp_path):
    path = _make_rectangle_room_dxf(tmp_path)
    entities, _ = dwg_reader.parse(path)
    result = dwg_classifier.classify(entities, {"WALL": "wall"})
    assert len(result.walls) == 4
    assert len(result.rejected) == 0
    assert result.layer_mapping_used == {"WALL": "wall"}


def test_classify_ignores_layers_not_mapped_as_wall(tmp_path):
    """Layers en dehors du mapping wall sont silencieusement ignorés."""
    path = _make_rectangle_room_dxf(tmp_path, extras=True)
    entities, _ = dwg_reader.parse(path)
    # On NE map PAS FURNITURE → ses lignes ignorées.
    result = dwg_classifier.classify(entities, {"WALL": "wall"})
    # 8 lignes paires + 1 orpheline = 4 walls + 1 rejected sur WALL.
    assert len(result.walls) == 4
    assert len(result.rejected) == 1
    assert result.rejected[0]["layer"] == "WALL"


# ----- tools/dwg_import : inspect / classify / import_walls ------------


@pytest.fixture
def registry_loaded():
    """Force l'auto-import des tools (lib.tools.*)."""
    llm_protocol.reset_registry()
    llm_protocol.get_registry()


def test_tool_dwg_inspect_returns_layer_summary(tmp_path, registry_loaded):
    path = _make_rectangle_room_dxf(tmp_path)
    kg = ProjectKG("p")
    result = llm_protocol.dispatch_tool_use(
        "dwg_inspect", {"file_path": str(path)}, "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["ok"] is True
    assert payload["source_format"] == "dxf"
    assert payload["total_entities"] == 8
    layer_names = [l["name"] for l in payload["layers"]]
    assert "WALL" in layer_names
    wall_layer = next(l for l in payload["layers"] if l["name"] == "WALL")
    assert wall_layer["suggested_role"] == "wall"


def test_tool_dwg_classify_returns_walls(tmp_path, registry_loaded):
    path = _make_rectangle_room_dxf(tmp_path)
    kg = ProjectKG("p")
    result = llm_protocol.dispatch_tool_use(
        "dwg_classify",
        {"file_path": str(path), "layer_mapping": {"WALL": "wall"}},
        "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["walls_count"] == 4
    assert payload["rejected_count"] == 0
    assert len(payload["walls"]) == 4


def test_tool_dwg_import_walls_kg_only_creates_walls(tmp_path, registry_loaded):
    """Import end-to-end KG-only : 4 walls dans le KG après dispatch."""
    path = _make_rectangle_room_dxf(tmp_path)
    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})

    result = llm_protocol.dispatch_tool_use(
        "dwg_import_walls",
        {
            "file_path": str(path),
            "level_ref": level,
            "wall_type_ref": wt,
            "layer_mapping": {"WALL": "wall"},
            "height_m": 2.7,
        },
        "t1", kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload["walls_imported"] == 4
    assert payload["rejected_count"] == 0
    assert kg.count_by_type("Wall") == 4


def test_tool_dwg_import_walls_translates_with_dx_dy(tmp_path, registry_loaded):
    """dx_m=10, dy_m=20 translate la pièce dans le KG."""
    path = _make_rectangle_room_dxf(tmp_path)
    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})

    result = llm_protocol.dispatch_tool_use(
        "dwg_import_walls",
        {
            "file_path": str(path),
            "level_ref": level,
            "wall_type_ref": wt,
            "layer_mapping": {"WALL": "wall"},
            "dx_m": 10.0,
            "dy_m": 20.0,
            "height_m": 2.7,
        },
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["walls_imported"] == 4
    # Tous les endpoints translatés.
    walls = [kg.get_node(nid) for nid in kg.find_by_type("Wall")]
    xs = [w["p1"][0] for w in walls] + [w["p2"][0] for w in walls]
    ys = [w["p1"][1] for w in walls] + [w["p2"][1] for w in walls]
    assert all(x >= 10.0 - 1e-6 for x in xs)
    assert all(y >= 20.0 - 1e-6 for y in ys)


def test_tool_dwg_import_walls_refuses_above_max_walls(tmp_path, registry_loaded):
    path = _make_rectangle_room_dxf(tmp_path)
    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})

    result = llm_protocol.dispatch_tool_use(
        "dwg_import_walls",
        {
            "file_path": str(path),
            "level_ref": level,
            "wall_type_ref": wt,
            "layer_mapping": {"WALL": "wall"},
            "max_walls": 2,  # garde-fou très bas
        },
        "t1", kg,
    )
    assert result["is_error"] is True
    assert "max_walls" in result["content"]
    # Atomicité : aucun wall créé.
    assert kg.count_by_type("Wall") == 0


def test_tool_dwg_import_walls_empty_mapping_noop(tmp_path, registry_loaded):
    """Mapping sans rôle 'wall' → no-op, pas d'erreur."""
    path = _make_rectangle_room_dxf(tmp_path)
    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})

    result = llm_protocol.dispatch_tool_use(
        "dwg_import_walls",
        {
            "file_path": str(path),
            "level_ref": level,
            "wall_type_ref": wt,
            "layer_mapping": {"WALL": "ignore"},
        },
        "t1", kg,
    )
    payload = json.loads(result["content"])
    assert payload["walls_imported"] == 0
    assert payload["inner"] is None
    assert "No wall candidates" in payload["note"]
