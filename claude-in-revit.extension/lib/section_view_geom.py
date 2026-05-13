"""section_view_geom.py — math du BoundingBoxXYZ + Transform pour
`ViewSection.CreateSection` (Étape 6 Phase 1 import projet).

**Aucun import Revit** — pure Python pour testabilité hors-Revit. Le
caller (tool `views_create_section`) consomme la sortie et instancie
les objets Autodesk.Revit.DB.XYZ + Transform + BoundingBoxXYZ.

Convention Revit pour ViewSection :

- `Transform.Origin` : position dans le monde du centre de la vue
  (midpoint de la section line en plan).
- `Transform.BasisX` : axe horizontal de la vue (de gauche à droite
  dans la vue rendue).
- `Transform.BasisY` : axe vertical = world up (0, 0, 1).
- `Transform.BasisZ` : axe « out of page » — pointe VERS le viewer
  (opposé de la direction de regard).
- `BBox.Min/Max` : dans le repère LOCAL du transform.
  - X : largeur de la vue, ±half_length de la section line.
  - Y : hauteur de la vue, bottom_elev → top_elev.
  - Z : profondeur de coupe, -far_clip → +near_clip.

Note de convention : « view_dir » dans nos APIs désigne la direction
DANS LAQUELLE LE VIEWER REGARDE en plan ("down" = regard vers -Y).
La BasisZ Revit est l'opposé.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple


# Lookup view_dir (plan-natural) → vecteur de regard (world).
_VIEW_DIR_TO_LOOK_VECTOR = {
    "left":  (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "up":    (0.0, 1.0, 0.0),
    "down":  (0.0, -1.0, 0.0),
}


@dataclass
class SectionViewBounds:
    """Tout ce dont le caller a besoin pour construire un BoundingBoxXYZ
    + Transform Revit.

    Coords en mètres ; conversion en feet faite par le tool wrapper.
    """
    origin_m: Tuple[float, float, float]
    basis_x: Tuple[float, float, float]  # vecteur unitaire "right in view"
    basis_y: Tuple[float, float, float]  # vecteur unitaire "up in view"
    basis_z: Tuple[float, float, float]  # vecteur unitaire "out of page" (vers viewer)
    bbox_min_m: Tuple[float, float, float]  # coords locales
    bbox_max_m: Tuple[float, float, float]
    section_length_m: float
    far_clip_m: float
    height_range_m: Tuple[float, float]  # (bottom, top) en m absolus


def _cross(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normalize(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if n < 1e-12:
        raise ValueError("Cannot normalize zero vector")
    return (v[0] / n, v[1] / n, v[2] / n)


def compute_section_view_bounds(
    p1_m: List[float],
    p2_m: List[float],
    view_dir: str,
    *,
    bottom_elev_m: float = 0.0,
    top_elev_m: float = 6.0,
    far_clip_m: float = 20.0,
    height_buffer_m: float = 1.0,
) -> SectionViewBounds:
    """Calcule le Transform + BBox pour `ViewSection.CreateSection`.

    **Convention BBox Revit** (cf. Building Coder + RevitAPI docs) :
    - `Max.Z` (local) = plan de coupe (= position du trait dans le plan).
      Doit être 0 puisque l'Origin est sur le trait.
    - `Min.Z` (local) = fond de la vue = `-far_clip_m` (profondeur
      dans le sens du regard, opposé à BasisZ).
    - Pas de « near_clip » : le trait EST le plan de coupe, rien
      n'est visible côté viewer. Bug initial reporté runtime
      2026-05-13 (« plan de coupe et fond inversés ») corrigé en
      passant de `Max.Z=+near_clip` à `Max.Z=0`.

    Args:
        p1_m, p2_m: les 2 endpoints du trait de coupe dans le plan,
            coords `[x, y]` ou `[x, y, z]` en mètres. Z ignoré (toujours
            0 dans le plan).
        view_dir: direction de regard dans le plan,
            `"left" | "right" | "up" | "down"`.
        bottom_elev_m: élévation du bas de la vue (default 0, niveau
            sol).
        top_elev_m: élévation du haut de la vue (default 6 m).
        far_clip_m: profondeur du clip arrière dans le sens du regard
            (default 20 m — typique pour traverser un bâtiment de bord
            en bord).
        height_buffer_m: marge au-dessus du top_elev_m pour inclure
            toiture / parapet (default 1 m).

    Returns:
        `SectionViewBounds` avec origin, basis vectors, bbox min/max.
    """
    if view_dir not in _VIEW_DIR_TO_LOOK_VECTOR:
        raise ValueError(
            "view_dir must be one of: left, right, up, down (got {!r})".format(view_dir)
        )
    if len(p1_m) < 2 or len(p2_m) < 2:
        raise ValueError("p1_m / p2_m must have at least [x, y]")

    # Promote 2D points to 3D with z=0.
    p1 = (float(p1_m[0]), float(p1_m[1]), 0.0)
    p2 = (float(p2_m[0]), float(p2_m[1]), 0.0)

    # Section length (in plan).
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    section_length = math.sqrt(dx * dx + dy * dy)
    if section_length < 1e-6:
        raise ValueError("Section line has zero length (p1 == p2)")

    # Origin Z = bottom_elev_m (PAS au centre). Raison : avec
    # OrientToView=True sur les liens CAD, Revit aligne DXF (0,0) sur
    # le local (0,0) de la vue, qui est sous-tendu par Origin en world.
    # Si Origin.Z = 0 (bottom), DXF y=0 (= Niveau 0) → world Z = 0
    # = bon alignement avec Niveau 0 Revit. Si Origin.Z = center (3.5m),
    # le DXF est shifté de +3.5m → bug runtime 2026-05-13 (niveaux
    # DXF/Revit non alignés). Le BBox Y est asymétrique en conséquence :
    # de 0 (bottom) à full_height (haut). X reste symétrique autour du
    # midpoint horizontal de la section line.
    origin = (
        (p1[0] + p2[0]) / 2.0,
        (p1[1] + p2[1]) / 2.0,
        bottom_elev_m,
    )

    # Look vector (toward where viewer is looking).
    look = _VIEW_DIR_TO_LOOK_VECTOR[view_dir]
    # BasisZ = opposite of look (out of page toward viewer).
    basis_z = (-look[0], -look[1], -look[2])
    # BasisY = world up.
    basis_y = (0.0, 0.0, 1.0)
    # BasisX = BasisY × BasisZ (right-hand rule).
    basis_x = _cross(basis_y, basis_z)
    basis_x = _normalize(basis_x)

    # BBox en repère LOCAL :
    # X: ±half-section-length (symétrique le long de la section line).
    # Y: [0, full_height] (asymétrique : Origin.Z = bottom_elev, donc
    #    Y=0 local = bottom_elev en world, Y=full_height local = top + buffer).
    # Z: [-far_clip_m, 0] où Max.Z = 0 = plan de coupe (= trait dans le
    #    plan), et Min.Z = -far_clip_m = fond de la vue (profondeur dans
    #    le sens du regard, dans la direction opposée à BasisZ).
    half_len = section_length / 2.0
    full_height = top_elev_m - bottom_elev_m + height_buffer_m
    bbox_min = (-half_len, 0.0, -far_clip_m)
    bbox_max = (half_len, full_height, 0.0)

    return SectionViewBounds(
        origin_m=origin,
        basis_x=basis_x,
        basis_y=basis_y,
        basis_z=basis_z,
        bbox_min_m=bbox_min,
        bbox_max_m=bbox_max,
        section_length_m=section_length,
        far_clip_m=far_clip_m,
        height_range_m=(bottom_elev_m, top_elev_m),
    )
