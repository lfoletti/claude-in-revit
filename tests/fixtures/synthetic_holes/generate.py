"""Génère un DXF synthétique « plan d'étage avec trémie + patio »
pour les tests unit de la détection de holes dans Phase 2c.

Convention AIA Revit standard :
- A-WALL    : paires de lignes parallèles définissant les murs
- A-FLOR    : aire utilisable du sol (closed polyline du contour intérieur)
- A-FLOR-STAIR : closed polyline de la trémie d'escalier
- A-FLOR-OPEN  : closed polyline d'un trou générique (patio, atrium, …)
- A-FLOR-OVHD  : projection horizontale d'éléments en surplomb (non-trou)

Géométrie produite (rectangle 12 × 8 m avec une trémie 2×3 m et un patio 2×2 m) :

    +----------------------+  (12, 8)
    |                      |
    |    [STAIR 2x3]       |
    |                      |
    |              [PATIO  |
    |               2x2]   |
    |                      |
    +----------------------+
  (0,0)                  (12, 0)

Exécution : `python tests/fixtures/synthetic_holes/generate.py`. Régénère le
fichier `floor_with_holes.dxf` à côté. Idempotent.
"""
from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf import zoom

WALL_THICKNESS = 0.2  # mètres
BUILDING_W = 12.0
BUILDING_H = 8.0

# Trémie escalier (rectangle 2×3 m, en haut à gauche).
STAIR = [(2.0, 4.5), (4.0, 4.5), (4.0, 7.5), (2.0, 7.5)]
# Patio (carré 2×2 m, en bas à droite).
PATIO = [(8.5, 1.0), (10.5, 1.0), (10.5, 3.0), (8.5, 3.0)]


def _wall_pair(msp, p1, p2, thickness=WALL_THICKNESS, normal_side=1):
    """Ajoute deux lignes parallèles séparées de `thickness` (un mur)."""
    import math
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    # Normal unit vector (rotated 90° from direction).
    nx, ny = -dy / length, dx / length
    half = thickness / 2.0 * normal_side
    # Inner line.
    msp.add_line(
        (p1[0] - nx * half, p1[1] - ny * half),
        (p2[0] - nx * half, p2[1] - ny * half),
        dxfattribs={"layer": "A-WALL"},
    )
    # Outer line.
    msp.add_line(
        (p1[0] + nx * half, p1[1] + ny * half),
        (p2[0] + nx * half, p2[1] + ny * half),
        dxfattribs={"layer": "A-WALL"},
    )


def main():
    doc = ezdxf.new(dxfversion="R2010", units=ezdxf.units.M)
    msp = doc.modelspace()

    # 4 murs périmètre (rectangle).
    _wall_pair(msp, (0.0, 0.0), (BUILDING_W, 0.0))  # mur sud
    _wall_pair(msp, (BUILDING_W, 0.0), (BUILDING_W, BUILDING_H))  # mur est
    _wall_pair(msp, (BUILDING_W, BUILDING_H), (0.0, BUILDING_H))  # mur nord
    _wall_pair(msp, (0.0, BUILDING_H), (0.0, 0.0))  # mur ouest

    # Contour intérieur du sol (informative — pas un hole en soi).
    msp.add_lwpolyline(
        [(0.1, 0.1), (BUILDING_W - 0.1, 0.1),
         (BUILDING_W - 0.1, BUILDING_H - 0.1),
         (0.1, BUILDING_H - 0.1)],
        close=True,
        dxfattribs={"layer": "A-FLOR"},
    )

    # Trémie escalier sur A-FLOR-STAIR (closed polyline).
    msp.add_lwpolyline(STAIR, close=True, dxfattribs={"layer": "A-FLOR-STAIR"})

    # Patio sur A-FLOR-OPEN (closed polyline).
    msp.add_lwpolyline(PATIO, close=True, dxfattribs={"layer": "A-FLOR-OPEN"})

    # Bruit : un objet sur A-FLOR-OVHD (projection / non-trou) — doit être
    # EXCLU de la détection de holes.
    msp.add_lwpolyline(
        [(6.0, 6.0), (7.0, 6.0), (7.0, 7.0), (6.0, 7.0)],
        close=True,
        dxfattribs={"layer": "A-FLOR-OVHD"},
    )

    out = Path(__file__).parent / "floor_with_holes.dxf"
    zoom.extents(msp)
    doc.saveas(out)
    print("Wrote", out)


if __name__ == "__main__":
    main()
