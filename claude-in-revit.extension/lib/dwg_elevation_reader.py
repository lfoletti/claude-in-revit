"""dwg_elevation_reader.py — lecture des élévations DXF + vote présence
mur/opening (V2 Phase 2.5).

User 2026-05-13 : « la lecture en élévation est déterminante pour
établir s'il y a un mur ou pas, ou de quelle hauteur il est (murets) ».

**Convention de projection world → élévation** calibrée sur P7 session
t :
- `Nord` : x_elev = **-world_X**, y_elev = world_Z.
- `Sud`  : x_elev = **+world_X**, y_elev = world_Z.
- `Est`  : x_elev = **+world_Y**, y_elev = world_Z.
- `Ouest`: x_elev = **-world_Y**, y_elev = world_Z.

Origine de l'élévation = origine du projet (z_world=0 ↔ y_elev=0).

Module pur — pas d'I/O fichier hors `parse_elevation` qui prend des
`DwgEntity` (déjà parsées par `dwg_reader`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .dwg_reader import DwgEntity
from .dwg_voting import Vote, abstain, no_vote, yes_vote


LAYER_WALLS = "A-WALL"
LAYER_LEVELS = "A-FLOR-LEVL"

# Directions cardinales acceptées (cohérent avec `classify_dxf`
# `evidence.direction` pour les élévations).
CARDINAL_DIRECTIONS: Tuple[str, ...] = ("Nord", "Sud", "Est", "Ouest")


@dataclass
class ElevationLine:
    """Une LINE A-WALL d'une élévation, en coordonnées DXF de la vue
    (= x_elev horizontal, y_elev vertical au-dessus du sol projet)."""
    p1: Tuple[float, float]
    p2: Tuple[float, float]
    is_horizontal: bool
    is_vertical: bool


@dataclass
class ElevationView:
    """Élévation parsée + métadonnées."""
    direction: str
    a_wall_lines: List[ElevationLine]
    a_wall_bbox: Optional[Tuple[float, float, float, float]]  # (x_min, x_max, y_min, y_max)
    levels_y: List[float]  # Y des A-FLOR-LEVL (= élévations world Z des niveaux)


def parse_elevation(
    entities: List[DwgEntity],
    direction: str,
) -> ElevationView:
    """Extrait les A-WALL + A-FLOR-LEVL d'une élévation.

    Classification des LINEs A-WALL en horizontale / verticale (tol 1°).

    Args:
        entities: les DwgEntity du DXF élévation (déjà parsées).
        direction: `"Nord"` | `"Sud"` | `"Est"` | `"Ouest"`.

    Returns:
        `ElevationView` avec lignes classifiées et bbox A-WALL.
    """
    if direction not in CARDINAL_DIRECTIONS:
        raise ValueError(
            "direction must be one of {} (got {!r})".format(
                CARDINAL_DIRECTIONS, direction,
            )
        )

    a_wall_lines: List[ElevationLine] = []
    xs: List[float] = []
    ys: List[float] = []
    levels_y: List[float] = []
    for e in entities:
        if e.kind != "LINE":
            continue
        (x1, y1, _), (x2, y2, _) = e.coords
        if e.layer == LAYER_WALLS:
            line = ElevationLine(
                p1=(round(x1, 4), round(y1, 4)),
                p2=(round(x2, 4), round(y2, 4)),
                is_horizontal=abs(y2 - y1) < 0.01,
                is_vertical=abs(x2 - x1) < 0.01,
            )
            a_wall_lines.append(line)
            xs.extend([x1, x2])
            ys.extend([y1, y2])
        elif e.layer == LAYER_LEVELS:
            if abs(y2 - y1) < 0.01:
                levels_y.append(round((y1 + y2) / 2.0, 4))

    bbox: Optional[Tuple[float, float, float, float]] = None
    if xs:
        bbox = (
            round(min(xs), 4), round(max(xs), 4),
            round(min(ys), 4), round(max(ys), 4),
        )

    levels_y_unique = sorted(set(levels_y))

    return ElevationView(
        direction=direction,
        a_wall_lines=a_wall_lines,
        a_wall_bbox=bbox,
        levels_y=levels_y_unique,
    )


def project_world_to_elevation(
    x_world: float, y_world: float, z_world: float,
    direction: str,
) -> Tuple[float, float]:
    """Projette un point world (X, Y, Z) dans le repère élévation
    selon la direction de la vue.

    Convention P7 (calibrée session t) :
    - `Nord` : x_elev = -X_world.
    - `Sud`  : x_elev = +X_world.
    - `Est`  : x_elev = +Y_world.
    - `Ouest`: x_elev = -Y_world.
    - Y_elev = Z_world dans tous les cas.

    Args:
        x_world / y_world / z_world: coords en mètres.
        direction: `"Nord"` | `"Sud"` | `"Est"` | `"Ouest"`.

    Returns:
        `(x_elev, y_elev)` en mètres dans le repère élévation.
    """
    if direction == "Nord":
        return (-x_world, z_world)
    if direction == "Sud":
        return (x_world, z_world)
    if direction == "Est":
        return (y_world, z_world)
    if direction == "Ouest":
        return (-y_world, z_world)
    raise ValueError("direction must be one of {} (got {!r})".format(
        CARDINAL_DIRECTIONS, direction,
    ))


def vote_wall_visible_in_elevation(
    wall_p1: Tuple[float, float],
    wall_p2: Tuple[float, float],
    level_elevation_m: float,
    height_m: float,
    elevation: ElevationView,
    *,
    perp_tol_m: float = 0.30,
    min_overlap_m: float = 0.30,
) -> Vote:
    """Vote si un mur (en plan) est visible dans l'élévation donnée.

    Algorithme :

    1. Project les 2 endpoints du mur en x_elev via convention cardinale.
    2. x_range_elev = (min, max) de ces 2 projections (= position
       horizontale du mur dans la vue).
    3. y_range_elev = (level_elevation_m, level_elevation_m + height_m).
    4. Cherche dans `elevation.a_wall_lines` :
       - Lignes verticales dont x ∈ [x_min - perp_tol, x_max + perp_tol]
         et qui traversent le y_range : signe d'un mur extérieur de
         côté.
       - Lignes horizontales dans le y_range et chevauchent x_range :
         signe d'un linteau / allège / dalle.
    5. Si overlap horizontal du x_range avec des A-WALL ≥ `min_overlap_m`
       → vote `yes`. Sinon → `no` ou `abstain` selon le contexte.

    Args:
        wall_p1 / wall_p2: endpoints centerline du mur en world plan (m).
        level_elevation_m: élévation absolue de la base du mur (world Z).
        height_m: hauteur du mur (m).
        elevation: `ElevationView` parsée.
        perp_tol_m: tolérance horizontale autour du x_range (défaut 30cm).
        min_overlap_m: overlap min pour voter yes (défaut 30cm).

    Returns:
        `Vote` avec source `"elevation_<direction>"` et evidence détaillée.
    """
    if elevation.a_wall_bbox is None:
        return abstain(
            "elevation_{}".format(elevation.direction),
            reason="no A-WALL in elevation",
        )

    # Project endpoints.
    p1_elev = project_world_to_elevation(
        wall_p1[0], wall_p1[1], level_elevation_m, elevation.direction,
    )
    p2_elev = project_world_to_elevation(
        wall_p2[0], wall_p2[1], level_elevation_m, elevation.direction,
    )
    x_min_w = min(p1_elev[0], p2_elev[0])
    x_max_w = max(p1_elev[0], p2_elev[0])
    y_min_w = level_elevation_m
    y_max_w = level_elevation_m + height_m

    # Si la projection est COMPLÈTEMENT en dehors de la bbox A-WALL de
    # l'élévation, le mur n'est pas visible dans cette vue.
    bx_min, bx_max, by_min, by_max = elevation.a_wall_bbox
    if x_max_w < bx_min - perp_tol_m or x_min_w > bx_max + perp_tol_m:
        return abstain(
            "elevation_{}".format(elevation.direction),
            reason="wall projects outside elevation bbox",
            wall_x_range=[round(x_min_w, 3), round(x_max_w, 3)],
            elev_x_range=[bx_min, bx_max],
        )

    # Cherche des A-WALL lines dans la zone projetée.
    overlap_h = 0.0  # somme des longueurs des lignes horizontales chevauchant x_range
    overlap_v = 0.0  # somme des longueurs des lignes verticales dans x_range
    h_lines_count = 0
    v_lines_count = 0
    for line in elevation.a_wall_lines:
        x_l, x_r = sorted((line.p1[0], line.p2[0]))
        y_l, y_h = sorted((line.p1[1], line.p2[1]))
        # Filter par y_range (la ligne doit toucher le rectangle y).
        if y_h < y_min_w - 1e-3 or y_l > y_max_w + 1e-3:
            continue
        if line.is_horizontal:
            # Overlap horizontal avec x_range.
            ov = max(0.0, min(x_max_w, x_r) - max(x_min_w, x_l))
            if ov > 0:
                overlap_h += ov
                h_lines_count += 1
        elif line.is_vertical:
            # Vertical : check si x ∈ x_range (± perp_tol).
            x_v = (x_l + x_r) / 2.0
            if x_min_w - perp_tol_m <= x_v <= x_max_w + perp_tol_m:
                ov_v = min(y_max_w, y_h) - max(y_min_w, y_l)
                if ov_v > 0:
                    overlap_v += ov_v
                    v_lines_count += 1

    evidence = {
        "wall_x_range": [round(x_min_w, 3), round(x_max_w, 3)],
        "wall_y_range": [round(y_min_w, 3), round(y_max_w, 3)],
        "h_lines_count": h_lines_count,
        "v_lines_count": v_lines_count,
        "overlap_h_m": round(overlap_h, 3),
        "overlap_v_m": round(overlap_v, 3),
    }

    # Critère yes : overlap_h ou overlap_v ≥ min_overlap_m.
    if overlap_h >= min_overlap_m or overlap_v >= min_overlap_m:
        # Confidence proportional to combined overlap, capped at 1.0.
        wall_length = math.sqrt(
            (wall_p2[0] - wall_p1[0]) ** 2 + (wall_p2[1] - wall_p1[1]) ** 2
        )
        denom = max(wall_length, height_m, 1e-3)
        ratio = min(1.0, (overlap_h + overlap_v) / (2 * denom))
        return yes_vote(
            "elevation_{}".format(elevation.direction),
            confidence=round(max(0.3, ratio), 3),
            **evidence,
        )

    # Aucun overlap significatif mais bbox compatible → no (mur attendu
    # mais absent en élévation).
    return no_vote(
        "elevation_{}".format(elevation.direction),
        confidence=0.5,
        **evidence,
    )
