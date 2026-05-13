"""Tests math BBox + Transform pour ViewSection (Étape 6 Phase 1).

Pure Python — pas d'import Revit. Vérifie l'orientation correcte des
basis vectors et les dimensions du BBox selon la convention Revit.
"""
from __future__ import annotations

import math

import pytest

from lib.section_view_geom import (
    SectionViewBounds,
    compute_section_view_bounds,
)


# ----- Validation des inputs -------------------------------------------


def test_invalid_view_dir_raises():
    with pytest.raises(ValueError, match="view_dir must be one of"):
        compute_section_view_bounds([0, 0], [10, 0], "northeast")


def test_zero_length_section_raises():
    with pytest.raises(ValueError, match="zero length"):
        compute_section_view_bounds([5, 5], [5, 5], "down")


# ----- Direction de regard "down" (trait horizontal) ------------------


def test_horizontal_section_view_down():
    """Trait horizontal Y=-6m, viewer regarde vers le sud (-Y).

    BasisX = horizontal in view. Viewer face -Y → world +X is on left.
    BasisX devrait pointer vers le right-in-view = world -X.
    BasisZ pointe vers viewer = +Y.
    Origin = midpoint horizontal + Z au CENTRE de la plage d'élévation.
    """
    r = compute_section_view_bounds(
        [-1.8, -6.0], [11.9, -6.0], "down",
        bottom_elev_m=0.0, top_elev_m=6.0, height_buffer_m=1.0,
    )
    # Origin = midpoint horizontal + center of (bottom=0 → top+buffer=7) = 3.5
    assert r.origin_m == pytest.approx(((-1.8 + 11.9) / 2, -6.0, 3.5))
    # BasisZ = -look = -(0, -1, 0) = (0, +1, 0) → vers le viewer (au +Y).
    assert r.basis_z == pytest.approx((0.0, 1.0, 0.0))
    # BasisY = world up.
    assert r.basis_y == pytest.approx((0.0, 0.0, 1.0))
    # BasisX = BasisY × BasisZ = (0,0,1) × (0,1,0) = (-1, 0, 0).
    assert r.basis_x == pytest.approx((-1.0, 0.0, 0.0))


def test_horizontal_section_view_up():
    """Même trait, viewer regarde vers le nord (+Y)."""
    r = compute_section_view_bounds([-1.8, -6.0], [11.9, -6.0], "up")
    assert r.basis_z == pytest.approx((0.0, -1.0, 0.0))
    assert r.basis_x == pytest.approx((1.0, 0.0, 0.0))


# ----- Direction de regard sur trait vertical -------------------------


def test_vertical_section_view_right():
    """Trait vertical X=5.25, viewer regarde vers l'est (+X)."""
    r = compute_section_view_bounds([5.25, -13.0], [5.25, 14.0], "right")
    # BasisZ = -(+X) = -X.
    assert r.basis_z == pytest.approx((-1.0, 0.0, 0.0))
    # BasisX = (0,0,1) × (-1,0,0) = (0×0 - 1×0, 1×(-1) - 0×0, 0×0 - 0×(-1)) = (0, -1, 0).
    assert r.basis_x == pytest.approx((0.0, -1.0, 0.0))


def test_vertical_section_view_left():
    """Même trait, viewer regarde vers l'ouest (-X)."""
    r = compute_section_view_bounds([5.25, -13.0], [5.25, 14.0], "left")
    assert r.basis_z == pytest.approx((1.0, 0.0, 0.0))
    assert r.basis_x == pytest.approx((0.0, 1.0, 0.0))


# ----- BBox local frame ------------------------------------------------


def test_bbox_dimensions_match_section_length_and_height():
    """BBox symétrique autour d'Origin (convention Revit).

    Total height = top - bottom + buffer = 7m → half = 3.5m.
    Origin.Z = bottom + half = 0 + 3.5 = 3.5 (centre vertical).
    BBox Y local = ±3.5 (symétrique).
    """
    r = compute_section_view_bounds(
        [0, 0], [10, 0], "down",
        bottom_elev_m=0.0, top_elev_m=6.0, height_buffer_m=1.0,
        far_clip_m=20.0, near_clip_m=1.0,
    )
    # X span = section length 10m → half-length each side.
    assert r.bbox_min_m[0] == pytest.approx(-5.0)
    assert r.bbox_max_m[0] == pytest.approx(5.0)
    # Y span symétrique : ±3.5.
    assert r.bbox_min_m[1] == pytest.approx(-3.5)
    assert r.bbox_max_m[1] == pytest.approx(3.5)
    # Z span = -far_clip to +near_clip.
    assert r.bbox_min_m[2] == pytest.approx(-20.0)
    assert r.bbox_max_m[2] == pytest.approx(1.0)
    # Origin.Z = centre vertical (3.5m above ground).
    assert r.origin_m[2] == pytest.approx(3.5)


def test_section_length_computed():
    r = compute_section_view_bounds([0, 0], [3, 4], "right")
    assert r.section_length_m == pytest.approx(5.0)  # 3-4-5 triangle


def test_origin_at_midpoint_with_center_elev_z():
    """Origin Z = centre vertical de la vue (bottom + half_height)."""
    r = compute_section_view_bounds(
        [0, 0], [10, 0], "down",
        bottom_elev_m=2.5, top_elev_m=5.5, height_buffer_m=0.0,
    )
    # half_height = (5.5 - 2.5 + 0) / 2 = 1.5 → Origin.Z = 2.5 + 1.5 = 4.0
    assert r.origin_m == pytest.approx((5.0, 0.0, 4.0))


# ----- Basis vectors are orthonormal -----------------------------------


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


@pytest.mark.parametrize("vd", ["left", "right", "up", "down"])
def test_basis_vectors_orthonormal(vd):
    r = compute_section_view_bounds([0, 0], [10, 0], vd)
    # Each basis is unit length.
    for v in (r.basis_x, r.basis_y, r.basis_z):
        n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        assert n == pytest.approx(1.0, abs=1e-9)
    # Pairwise orthogonal.
    assert _dot(r.basis_x, r.basis_y) == pytest.approx(0.0, abs=1e-9)
    assert _dot(r.basis_x, r.basis_z) == pytest.approx(0.0, abs=1e-9)
    assert _dot(r.basis_y, r.basis_z) == pytest.approx(0.0, abs=1e-9)
