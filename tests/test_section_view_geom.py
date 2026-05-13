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
    Origin Z = bottom_elev_m pour que DXF lié en OrientToView aligne
    son y=0 sur world Z=0 (= Niveau 0).
    """
    r = compute_section_view_bounds(
        [-1.8, -6.0], [11.9, -6.0], "down",
        bottom_elev_m=0.0, top_elev_m=6.0, height_buffer_m=1.0,
    )
    # Origin = midpoint horizontal + bottom_elev_m en Z.
    assert r.origin_m == pytest.approx(((-1.8 + 11.9) / 2, -6.0, 0.0))
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
    """X symétrique autour d'Origin, Y asymétrique [0, height] depuis
    Origin.Z = bottom_elev, Z = [-far_clip, 0] (cut plane à Max.Z).

    Origin.Z = bottom_elev = 0 → DXF y=0 lié au view aligne avec
    Niveau 0 Revit (world Z=0).
    """
    r = compute_section_view_bounds(
        [0, 0], [10, 0], "down",
        bottom_elev_m=0.0, top_elev_m=6.0, height_buffer_m=1.0,
        far_clip_m=20.0,
    )
    # X span = section length 10m → half-length each side.
    assert r.bbox_min_m[0] == pytest.approx(-5.0)
    assert r.bbox_max_m[0] == pytest.approx(5.0)
    # Y span asymétrique : 0 à full_height = 7m (= 6 + buffer 1).
    assert r.bbox_min_m[1] == pytest.approx(0.0)
    assert r.bbox_max_m[1] == pytest.approx(7.0)
    # Z span = [-far_clip, 0] : cut plane à Max.Z=0, fond à Min.Z=-20.
    assert r.bbox_min_m[2] == pytest.approx(-20.0)
    assert r.bbox_max_m[2] == pytest.approx(0.0)
    # Origin.Z = bottom_elev_m (= 0 pour alignement DXF).
    assert r.origin_m[2] == pytest.approx(0.0)


def test_max_z_is_cut_plane_at_zero():
    """Revit convention : Max.Z (local) = plan de coupe = 0 (à l'Origin).

    Rien ne doit être visible côté viewer (Z > 0). C'est le fix runtime
    2026-05-13 : avant, Max.Z=+1m plaçait le cut plane 1m offset du
    trait → user voyait coupe et fond inversés.
    """
    r = compute_section_view_bounds([0, 0], [5, 0], "down", far_clip_m=15.0)
    assert r.bbox_max_m[2] == 0.0
    assert r.bbox_min_m[2] == -15.0


def test_section_length_computed():
    r = compute_section_view_bounds([0, 0], [3, 4], "right")
    assert r.section_length_m == pytest.approx(5.0)  # 3-4-5 triangle


def test_origin_at_midpoint_with_bottom_elev_z():
    """Origin Z = bottom_elev_m (et pas le centre)."""
    r = compute_section_view_bounds(
        [0, 0], [10, 0], "down",
        bottom_elev_m=2.5, top_elev_m=5.5, height_buffer_m=0.0,
    )
    assert r.origin_m == pytest.approx((5.0, 0.0, 2.5))


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
