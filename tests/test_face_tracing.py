"""Tests for `dwg_face_tracing.trace_outer_boundary_2d` + fallback wrapper.

Couvre des cas planar canoniques :
- Rectangle simple : 4 corners CCW.
- Plan en L : 6 corners CCW (le coeur du fix sur le hull convexe).
- Plan avec mur intérieur (T-junction au corner) : outer face = 4 corners,
  pas affecté par le split.
- X-junction : 2 segments qui se croisent au milieu — split planar requis.
- Murs déconnectés : retourne None (caller doit fallback).
- Hybride avec fallback : `trace_outer_boundary_with_fallback`.

Validation : aire signée du résultat > 0 (CCW), premier point ≠ dernier
(Revit veut un CurveLoop ouvert).
"""
from __future__ import annotations

from lib.dwg_face_tracing import (
    trace_outer_boundary_2d,
    trace_outer_boundary_with_fallback,
    _signed_area,
)


def _signed_area_pts(pts):
    """Aire signée d'une liste de points (formule Gauss). + pour CCW."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


# ----- Cas canoniques --------------------------------------------------


def test_rectangle_4_corners():
    walls = [
        ((0.0, 0.0), (10.0, 0.0)),
        ((10.0, 0.0), (10.0, 5.0)),
        ((10.0, 5.0), (0.0, 5.0)),
        ((0.0, 5.0), (0.0, 0.0)),
    ]
    boundary = trace_outer_boundary_2d(walls)
    assert boundary is not None
    assert len(boundary) == 4
    # Aire CCW = 50 m².
    assert abs(_signed_area_pts(boundary) - 50.0) < 1e-6


def test_l_shape_6_corners():
    """Plan en L : rectangle 10×10 avec coin retiré (haut-droite 5×5).
    Le convex hull donnerait 4 corners (100 m²), face-tracing doit
    donner 6 corners (75 m²)."""
    walls = [
        ((0, 0), (10, 0)),
        ((10, 0), (10, 5)),
        ((10, 5), (5, 5)),
        ((5, 5), (5, 10)),
        ((5, 10), (0, 10)),
        ((0, 10), (0, 0)),
    ]
    boundary = trace_outer_boundary_2d(walls)
    assert boundary is not None
    assert len(boundary) == 6
    # Aire CCW = 75 m² (100 - coin 25).
    assert abs(_signed_area_pts(boundary) - 75.0) < 1e-6


def test_interior_wall_doesnt_affect_outer():
    """Rectangle 10×6 avec un mur intérieur vertical à x=5 (T-junctions).
    L'outer face doit rester 4 corners du rectangle."""
    walls = [
        ((0, 0), (10, 0)),
        ((10, 0), (10, 6)),
        ((10, 6), (0, 6)),
        ((0, 6), (0, 0)),
        ((5, 0), (5, 6)),  # internal split
    ]
    boundary = trace_outer_boundary_2d(walls)
    assert boundary is not None
    assert len(boundary) == 4
    assert abs(_signed_area_pts(boundary) - 60.0) < 1e-6


def test_x_junction_splits_internally():
    """Deux segments qui se croisent au milieu sans endpoint partagé —
    le planar graph splits chaque segment au point d'intersection.
    Le résultat n'est pas un building "fermé" (juste un X), donc
    pas de face fermée valide → retourne None."""
    walls = [
        ((0, 0), (10, 10)),
        ((0, 10), (10, 0)),
    ]
    # No closed face : retourne None (à fallback côté caller).
    boundary = trace_outer_boundary_2d(walls)
    assert boundary is None


def test_disconnected_walls_returns_none():
    """Deux segments isolés sans connection → pas de face fermée."""
    walls = [
        ((0, 0), (5, 0)),
        ((10, 10), (15, 10)),
    ]
    assert trace_outer_boundary_2d(walls) is None


def test_empty_walls_returns_none():
    assert trace_outer_boundary_2d([]) is None


# ----- Hybride avec fallback -------------------------------------------


def test_hybrid_uses_face_tracing_when_possible():
    walls = [
        ((0, 0), (10, 0)),
        ((10, 0), (10, 5)),
        ((10, 5), (0, 5)),
        ((0, 5), (0, 0)),
    ]
    fallback = [(0, 0), (10, 0), (10, 5), (0, 5)]
    boundary, method = trace_outer_boundary_with_fallback(walls, fallback)
    assert method == "face_tracing"
    assert len(boundary) == 4


def test_hybrid_falls_back_to_hull_when_disconnected():
    walls = [
        ((0, 0), (5, 0)),    # disconnected
        ((10, 10), (15, 10)),
    ]
    fallback = [(0, 0), (15, 0), (15, 15), (0, 15)]
    boundary, method = trace_outer_boundary_with_fallback(walls, fallback)
    assert method == "convex_hull_fallback"
    assert boundary == fallback


def test_floor_loops_outer_only():
    """4 LINEs formant un rectangle 10×6 = 1 outer, 0 holes (cas P2 N0)."""
    from lib.dwg_face_tracing import trace_floor_loops_2d
    segments = [
        ((0, 0), (10, 0)),
        ((10, 0), (10, 6)),
        ((10, 6), (0, 6)),
        ((0, 6), (0, 0)),
    ]
    result = trace_floor_loops_2d(segments)
    assert result is not None
    assert len(result["outer"]) == 4
    assert result["holes"] == []


def test_floor_loops_outer_plus_one_hole():
    """4 LINEs outer + 4 LINEs inner = 1 outer + 1 hole (cas P2 N2 clean)."""
    from lib.dwg_face_tracing import trace_floor_loops_2d
    segments = [
        # Outer 20×16.
        ((0, 0), (20, 0)),
        ((20, 0), (20, 16)),
        ((20, 16), (0, 16)),
        ((0, 16), (0, 0)),
        # Inner trémie 3×7.
        ((5, 4), (8, 4)),
        ((8, 4), (8, 11)),
        ((8, 11), (5, 11)),
        ((5, 11), (5, 4)),
    ]
    result = trace_floor_loops_2d(segments)
    assert result is not None
    assert len(result["outer"]) == 4
    assert len(result["holes"]) == 1
    assert len(result["holes"][0]) == 4


def test_floor_loops_handles_landing_gaps_in_tremie():
    """Trémie en U avec 2 gaps de 20cm (landing-access escalier — cas P2 N1).
    `snap_tol_m=0.2` doit fermer les gaps et détecter la trémie comme
    1 hole de 6 vertices."""
    from lib.dwg_face_tracing import trace_floor_loops_2d
    segments = [
        # Outer 20×16.
        ((0, 0), (20, 0)),
        ((20, 0), (20, 16)),
        ((20, 16), (0, 16)),
        ((0, 16), (0, 0)),
        # Trémie 3×7 avec 2 gaps de 20cm sur le côté gauche x=5.
        ((5, 4), (5, 7.4)),   # bas
        ((5, 7.6), (5, 11)),  # haut (gap de 20cm à y=7.4-7.6)
        ((5, 11), (8, 11)),
        ((8, 11), (8, 7.6)),
        ((8, 7.4), (8, 4)),   # gap symétrique côté droit
        ((8, 4), (5, 4)),
    ]
    result = trace_floor_loops_2d(segments, snap_tol_m=0.25)
    assert result is not None
    assert len(result["outer"]) == 4
    assert len(result["holes"]) == 1
    # Hole = 6 verts (la trémie traverse les gaps).
    assert len(result["holes"][0]) == 6


def test_floor_loops_returns_none_for_open_segments():
    """Segments isolés sans loops fermés → None."""
    from lib.dwg_face_tracing import trace_floor_loops_2d
    segments = [
        ((0, 0), (5, 0)),
        ((10, 10), (15, 10)),
    ]
    assert trace_floor_loops_2d(segments) is None


def test_hybrid_tries_progressively_larger_tolerance():
    """Murs avec petit gap (1cm) qui ferait échouer le snap_tol=0.005 mais
    passer à snap_tol=0.025. Le hybride essaie tol croissant."""
    # Build a rectangle with a 2cm gap at one corner.
    walls = [
        ((0.0, 0.0), (10.0, 0.0)),
        ((10.02, 0.0), (10.02, 5.0)),  # x=10.02 instead of 10.0 (2cm gap)
        ((10.0, 5.0), (0.0, 5.0)),
        ((0.0, 5.0), (0.0, 0.0)),
    ]
    fallback = [(0, 0), (10, 0), (10, 5), (0, 5)]
    # snap_tol=0.005 fails (gap > tol), snap_tol_m=0.025 should succeed
    # (gap < tol). The hybrid tries snap_tol then snap_tol*5 then snap_tol*20.
    boundary, method = trace_outer_boundary_with_fallback(
        walls, fallback, snap_tol_m=0.005,
    )
    assert method == "face_tracing", f"Expected face_tracing, got {method}"
    assert len(boundary) >= 4
