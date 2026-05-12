"""Tests for `lib.preprocess` — exhaustive-quantifier detection + autoscan.

Two layers :

1. **Pure regex** (`detect_exhaustive_collections`) — fast, no KG,
   focuses on the natural-language pattern surface (FR + EN, sing/plur,
   accent-less fallbacks, false-positive avoidance).
2. **Autoscan dispatch** (`autoscan_payload`) — exercises the full
   pipeline against a seeded ProjectKG, verifies the `<auto_scan_kg>`
   block formatting and that the dispatched catalog results actually
   reach the preamble.
"""
from __future__ import annotations

import pytest

from lib import llm_protocol, preprocess
from lib.project_kg import ProjectKG


# ----- detect_exhaustive_collections -----------------------------------


def test_detects_toutes_les_fenetres():
    detected = preprocess.detect_exhaustive_collections(
        "passe toutes les fenêtres à 1 m d'allège"
    )
    assert detected == [("catalog_list_windows", "windows")]


def test_detects_tous_les_murs():
    detected = preprocess.detect_exhaustive_collections(
        "supprime tous les murs du R+1"
    )
    assert detected == [("catalog_list_walls", "walls")]


def test_detects_chaque_porte():
    detected = preprocess.detect_exhaustive_collections(
        "vérifie chaque porte du projet"
    )
    assert detected == [("catalog_list_doors", "doors")]


def test_detects_lensemble_des_poteaux():
    detected = preprocess.detect_exhaustive_collections(
        "l'ensemble des poteaux du sous-sol"
    )
    assert detected == [("catalog_list_columns", "columns")]


def test_detects_la_totalite_des_niveaux():
    detected = preprocess.detect_exhaustive_collections(
        "donne-moi la totalité des niveaux"
    )
    assert detected == [("catalog_list_levels", "levels")]


def test_detects_english_all_the_windows():
    detected = preprocess.detect_exhaustive_collections(
        "raise all the windows to 1.2m sill height"
    )
    assert detected == [("catalog_list_windows", "windows")]


def test_detects_english_every_door():
    detected = preprocess.detect_exhaustive_collections(
        "check every door for fire rating"
    )
    assert detected == [("catalog_list_doors", "doors")]


def test_no_match_on_singular_indefinite():
    """« j'ai vu une fenêtre » ≠ exhaustive : no autoscan."""
    detected = preprocess.detect_exhaustive_collections(
        "ajoute une fenêtre dans wall_001"
    )
    assert detected == []


def test_no_match_on_isolated_noun():
    """« fenêtre » seule, sans quantifier, ne déclenche pas."""
    detected = preprocess.detect_exhaustive_collections(
        "déplace cette fenêtre de 30 cm vers la gauche"
    )
    assert detected == []


def test_multi_collections_ordered_by_position():
    """« toutes les portes et toutes les fenêtres » → deux entrées,
    dans l'ordre où elles apparaissent dans le prompt."""
    detected = preprocess.detect_exhaustive_collections(
        "supprime toutes les portes et toutes les fenêtres du R+1"
    )
    assert detected == [
        ("catalog_list_doors", "doors"),
        ("catalog_list_windows", "windows"),
    ]


def test_deduplicates_same_collection_mentioned_twice():
    """Le user qui répète « toutes les fenêtres » ne déclenche qu'un
    seul autoscan — pas de double appel."""
    detected = preprocess.detect_exhaustive_collections(
        "toutes les fenêtres : passe toutes les fenêtres à 1.2 m"
    )
    assert detected == [("catalog_list_windows", "windows")]


def test_detects_type_catalog_prefers_specific_over_generic():
    """« tous les types de mur » → catalog_list_wall_types, PAS
    catalog_list_walls. La règle d'ordre dans `_COLLECTION_MAP`
    privilégie le noun phrase le plus spécifique."""
    detected = preprocess.detect_exhaustive_collections(
        "liste tous les types de mur disponibles"
    )
    assert detected == [("catalog_list_wall_types", "wall_types")]


def test_detects_types_de_fenetre():
    detected = preprocess.detect_exhaustive_collections(
        "donne-moi tous les types de fenêtre du projet"
    )
    assert detected == [("catalog_list_window_types", "window_types")]


def test_accent_less_fallback_for_fenetre():
    """Si l'utilisateur tape sans accent (« fenetres »), on détecte
    quand même — le pattern accepte e/é interchangeables."""
    detected = preprocess.detect_exhaustive_collections(
        "passe toutes les fenetres à 1m d'allège"
    )
    assert detected == [("catalog_list_windows", "windows")]


def test_case_insensitive():
    detected = preprocess.detect_exhaustive_collections(
        "SUPPRIME TOUS LES MURS"
    )
    assert detected == [("catalog_list_walls", "walls")]


# ----- autoscan_payload ------------------------------------------------


@pytest.fixture
def kg_with_two_windows():
    """KG carrying 1 Level + 1 WallType + 1 Wall + 1 FamilyType
    (Windows) + 2 Window instances — minimal substrate to exercise
    catalog_list_windows."""
    # Auto-import the registry so dispatch_tool_use works.
    llm_protocol.reset_registry()
    llm_protocol.get_registry()

    kg = ProjectKG("p")
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})
    wall = kg.add_node("Wall", {
        "type_ref": wt, "level_ref": level,
        "p1": [0.0, 0.0], "p2": [5.0, 0.0], "length": 5.0, "height": 2.7,
    })
    ft = kg.add_node("FamilyType", {
        "family_name": "Fenêtre fixe", "type_name": "1200x1500",
        "category": "Windows",
    })
    w1 = kg.add_node("Window", {
        "type_ref": ft, "host_wall_ref": wall,
        "position": [1.0, 0.0], "sill_height": 0.9, "head_height": 2.4,
    })
    w2 = kg.add_node("Window", {
        "type_ref": ft, "host_wall_ref": wall,
        "position": [3.0, 0.0], "sill_height": 0.9, "head_height": 2.4,
    })
    return kg, w1, w2


def test_autoscan_payload_empty_for_non_exhaustive_prompt(kg_with_two_windows):
    kg, _, _ = kg_with_two_windows
    payload = preprocess.autoscan_payload(
        "ajoute une fenêtre dans wall_001", kg,
    )
    assert payload == ""


def test_autoscan_payload_includes_window_llm_ids(kg_with_two_windows):
    """« toutes les fenêtres » → bloc contient w1 et w2."""
    kg, w1, w2 = kg_with_two_windows
    payload = preprocess.autoscan_payload(
        "passe toutes les fenêtres à 1 m d'allège", kg,
    )
    assert payload != ""
    assert "<auto_scan_kg>" in payload
    assert "</auto_scan_kg>" in payload
    assert "catalog_list_windows" in payload
    assert w1 in payload
    assert w2 in payload
    # Trailing note nudges the model.
    assert "exhaustive" in payload.lower()


def test_autoscan_payload_handles_multi_collection(kg_with_two_windows):
    """« toutes les portes et toutes les fenêtres » → bloc inclut les
    deux catalogues, même si le catalog_list_doors retourne vide."""
    kg, _, _ = kg_with_two_windows
    payload = preprocess.autoscan_payload(
        "supprime toutes les portes et toutes les fenêtres", kg,
    )
    assert "catalog_list_doors" in payload
    assert "catalog_list_windows" in payload


# ----- infer_tier_max ----------------------------------------------------


@pytest.mark.parametrize("prompt,expected", [
    ("crée un mur de 5m", 1),
    ("liste les fenêtres", 1),
    ("place une porte", 1),
    # DWG / DXF keywords → tier-2.
    ("importe ce dxf", 2),
    ("inspecte le fichier plan.DXF", 2),
    ("ingest ce DWG", 2),
    ("importer depuis ce plan CAD", 2),
    ("import the DXF file", 2),
    # No match — mots isolés sans contexte.
    ("crée une nouvelle vue plan", 1),
])
def test_infer_tier_max(prompt, expected):
    assert preprocess.infer_tier_max(prompt) == expected


def test_infer_tier_max_empty_prompt():
    assert preprocess.infer_tier_max("") == 1
    assert preprocess.infer_tier_max(None) == 1
