"""Tests unitaires pour dedup_inter_level_walls.

Cas couverts :
- 1 mur N1 identique à 1 mur N0 → skip.
- 1 mur N1 long couvert par 2 murs N0 collés (joint) → skip.
- 1 mur N1 légèrement plus long que tout mur N0 → keep (pas couvert).
- 1 mur N1 même position mais épaisseur différente → keep.
- 1 mur N1 parallèle mais non-collinéaire (offset > tol) → keep.
- N2 mur couvert par union (N0 ∪ N1) → skip (accumulation transitive).
- Cas P2 réel : 4 murs N2 (Ouest/Sud/Est/Nord-continu) face à 5 murs N1
  (Nord en 2 segments) → tous skip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import pytest

from lib.wall_inter_level_dedup import (
    _are_collinear,
    _is_top_wall_covered_by_lower,
    _union_intervals,
    dedup_inter_level_walls,
)


@dataclass
class W:
    """Ducktyped wall for the dedup function : p1, p2, thickness."""
    p1: Tuple[float, float]
    p2: Tuple[float, float]
    thickness: float


# ----- Helpers géométriques --------------------------------------------


def test_are_collinear_same_line():
    assert _are_collinear((0, 0), (10, 0), (2, 0), (5, 0), perp_tol_m=0.05)


def test_are_collinear_offset_below_tol():
    # Centerline w2 à y=0.03 (3cm offset), tol 5cm → collinéaire.
    assert _are_collinear((0, 0), (10, 0), (2, 0.03), (5, 0.03), perp_tol_m=0.05)


def test_are_collinear_offset_above_tol():
    # Offset y=0.10 (10cm) > tol 5cm → pas collinéaire.
    assert not _are_collinear((0, 0), (10, 0), (2, 0.10), (5, 0.10), perp_tol_m=0.05)


def test_are_collinear_perpendicular():
    # w2 perpendiculaire à w1 → pas collinéaire (sauf au point de croisement).
    assert not _are_collinear((0, 0), (10, 0), (5, -5), (5, 5), perp_tol_m=0.05)


def test_union_intervals_disjoint():
    assert _union_intervals([(0, 1), (3, 4)], join_tol_m=0.1) == [(0, 1), (3, 4)]


def test_union_intervals_touching_below_tol():
    # Gap 0.05 ≤ tol 0.10 → fusion.
    assert _union_intervals([(0, 1), (1.05, 2)], join_tol_m=0.10) == [(0, 2)]


def test_union_intervals_touching_above_tol():
    # Gap 0.20 > tol 0.10 → pas de fusion.
    assert _union_intervals(
        [(0, 1), (1.20, 2)], join_tol_m=0.10,
    ) == [(0, 1), (1.20, 2)]


def test_union_intervals_overlapping():
    assert _union_intervals([(0, 2), (1, 3)], join_tol_m=0.1) == [(0, 3)]


# ----- _is_top_wall_covered_by_lower -----------------------------------


def test_top_wall_covered_by_single_lower():
    lower = [W((0, 0), (10, 0), 0.20)]
    covered = _is_top_wall_covered_by_lower(
        (0, 0), (10, 0), 0.20, lower,
        thickness_tol_m=0.005, perp_tol_m=0.05,
        join_tol_m=0.10, coverage_tol_m=0.05,
    )
    assert covered


def test_top_wall_covered_by_union_of_two_lower():
    # Lower : 2 murs collés (gap 5 cm < join_tol 10 cm).
    lower = [
        W((0, 0), (5, 0), 0.20),
        W((5.05, 0), (10, 0), 0.20),
    ]
    # Top : un seul mur de 0 à 10.
    covered = _is_top_wall_covered_by_lower(
        (0, 0), (10, 0), 0.20, lower,
        thickness_tol_m=0.005, perp_tol_m=0.05,
        join_tol_m=0.10, coverage_tol_m=0.05,
    )
    assert covered


def test_top_wall_not_covered_when_lower_too_short():
    # Lower : mur de 0 à 8. Top : mur de 0 à 10 → pas couvert (10 > 8).
    lower = [W((0, 0), (8, 0), 0.20)]
    covered = _is_top_wall_covered_by_lower(
        (0, 0), (10, 0), 0.20, lower,
        thickness_tol_m=0.005, perp_tol_m=0.05,
        join_tol_m=0.10, coverage_tol_m=0.05,
    )
    assert not covered


def test_top_wall_not_covered_when_thickness_differs():
    lower = [W((0, 0), (10, 0), 0.30)]  # 30cm
    covered = _is_top_wall_covered_by_lower(
        (0, 0), (10, 0), 0.20, lower,  # top = 20cm
        thickness_tol_m=0.005, perp_tol_m=0.05,
        join_tol_m=0.10, coverage_tol_m=0.05,
    )
    assert not covered


def test_top_wall_not_covered_when_offset_perpendicular():
    lower = [W((0, 1.0), (10, 1.0), 0.20)]  # 1m offset perp
    covered = _is_top_wall_covered_by_lower(
        (0, 0), (10, 0), 0.20, lower,
        thickness_tol_m=0.005, perp_tol_m=0.05,
        join_tol_m=0.10, coverage_tol_m=0.05,
    )
    assert not covered


# ----- dedup_inter_level_walls : intégration --------------------------


def test_dedup_lowest_level_keeps_all():
    walls_n0 = [W((0, 0), (10, 0), 0.20), W((0, 0), (0, 10), 0.20)]
    filtered, events = dedup_inter_level_walls([(0.0, walls_n0)])
    assert filtered[0][1] == walls_n0
    assert events == []


def test_dedup_identical_top_wall_skipped():
    """Cas P2 simple : N1 a 4 murs, N2 a les mêmes 4 murs → tous skipped à N2."""
    box_n1 = [
        W((-13.42, -5.99), (-13.42, 8.01), 0.20),  # Ouest
        W((-2.93, -5.99), (-13.42, -5.99), 0.20),  # Sud
        W((-2.93, 8.01), (-2.93, -5.99), 0.20),    # Est
        W((-13.42, 8.01), (-2.93, 8.01), 0.20),    # Nord (continu)
    ]
    box_n2 = [
        W((-13.42, -5.99), (-13.42, 8.01), 0.20),
        W((-2.93, -5.99), (-13.42, -5.99), 0.20),
        W((-2.93, 8.01), (-2.93, -5.99), 0.20),
        W((-13.42, 8.01), (-2.93, 8.01), 0.20),
    ]
    filtered, events = dedup_inter_level_walls(
        [(3.0, box_n1), (6.0, box_n2)],
    )
    assert len(filtered[0][1]) == 4    # N1 inchangé
    assert len(filtered[1][1]) == 0    # N2 vidé
    assert len(events) == 4
    # En mode full_coverage_only (défaut), reason = level_fully_covered_by_lower
    assert all(e["reason"] == "level_fully_covered_by_lower" for e in events)


def test_dedup_p2_segmented_nord_wall_skipped():
    """Cas P2 réel : N1 a mur Nord en 2 segments (3.85m + 0.78m), N2
    a mur Nord en 1 seul segment continu → le N2 doit être skip."""
    # N1 : Nord en 2 segments avec gap 0 (joint pile)
    n1 = [
        W((-2.93, 8.01), (-12.65, 8.01), 0.20),  # 9.72m
        W((-12.65, 8.01), (-13.42, 8.01), 0.20),  # 0.78m, joint pile au précédent
    ]
    # N2 : Nord continu sur tout.
    n2 = [
        W((-13.42, 8.01), (-2.93, 8.01), 0.20),  # 10.49m continu
    ]
    filtered, events = dedup_inter_level_walls([(3.0, n1), (6.0, n2)])
    assert len(filtered[0][1]) == 2  # N1 inchangé
    assert len(filtered[1][1]) == 0  # N2 vidé
    assert len(events) == 1


def test_dedup_partial_keeps_all_in_full_coverage_mode():
    """Mode défaut full_coverage_only=True : si seulement une partie
    des murs N2 duplique N1, on garde TOUT (= empilage intentionnel
    de murs porteurs sur étage habité, cf. P7). Comportement
    conservateur — n'intervient que sur dupli plan complet."""
    n1 = [W((0, 0), (10, 0), 0.20)]
    n2 = [
        W((0, 0), (10, 0), 0.20),   # duplique N1
        W((0, 5), (10, 5), 0.20),   # mur N2 only
    ]
    filtered, events = dedup_inter_level_walls([(3.0, n1), (6.0, n2)])
    assert len(filtered[0][1]) == 1
    assert len(filtered[1][1]) == 2  # AUCUN skippé en mode défaut
    assert events == []


def test_dedup_partial_skips_individually_when_full_coverage_false():
    """Mode opt-in full_coverage_only=False : skip au cas-par-cas
    (utile pour debug ou projets très propres)."""
    n1 = [W((0, 0), (10, 0), 0.20)]
    n2 = [
        W((0, 0), (10, 0), 0.20),   # duplique N1 → skip
        W((0, 5), (10, 5), 0.20),   # mur N2 only → keep
    ]
    filtered, events = dedup_inter_level_walls(
        [(3.0, n1), (6.0, n2)], full_coverage_only=False,
    )
    assert len(filtered[0][1]) == 1
    assert len(filtered[1][1]) == 1
    assert filtered[1][1][0].p1 == (0, 5)
    assert len(events) == 1


def test_dedup_transitive_through_multiple_levels():
    """N0 mur + N1 vide + N2 même mur → N2 skip (accumulation transitive
    : N2 face à l'union N0 ∪ N1)."""
    wall = W((0, 0), (10, 0), 0.20)
    filtered, events = dedup_inter_level_walls([
        (0.0, [wall]),
        (3.0, []),
        (6.0, [W((0, 0), (10, 0), 0.20)]),
    ])
    assert len(filtered[0][1]) == 1
    assert len(filtered[1][1]) == 0
    assert len(filtered[2][1]) == 0
    assert len(events) == 1
    assert events[0]["level_elev_m"] == 6.0


def test_dedup_keeps_wall_partially_extending_beyond_lower():
    """Cas frontière : N2 mur déborde légèrement à droite d'un N1 mur.
    Si le débordement > coverage_tol, le mur N2 est gardé."""
    n1 = [W((0, 0), (10, 0), 0.20)]
    # N2 mur va de 0 à 11 (1m de débordement) → coverage_tol 5cm trop petit.
    n2 = [W((0, 0), (11, 0), 0.20)]
    filtered, events = dedup_inter_level_walls(
        [(3.0, n1), (6.0, n2)],
        coverage_tol_m=0.05,
    )
    assert len(filtered[1][1]) == 1
    assert events == []
