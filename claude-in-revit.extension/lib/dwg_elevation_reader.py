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
    wall_thickness_m: float = 0.20,
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

    # V3.7 : pas d'extension thickness en profil (cf. note précédente).
    # On garde wall_thickness_m en paramètre pour future utilisation
    # mais on n'élargit plus le x_range. Le perp_tol_m du critère
    # overlap_v capture déjà une bande raisonnable autour de x_elev.

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

    # V3.4 (user 2026-05-13) : « les élévations Ouest, Nord et Sud
    # donnent des indications sur les 3 murs. certains de ces murs sont
    # parfois vus de face, parfois de profil ». **On laisse toutes les
    # élévations voter** (suppression de la restriction V3.2 par
    # orientation). Une élévation qui voit le mur de profil émet quand
    # même un vote — typiquement no s'il n'y a pas de matière dans la
    # zone projetée (= ligne aplatie à x_elev=constante).

    # V3.7 rollback : pas d'extension thickness en profil (causait
    # régression pipeline : murs intérieurs en profil → 0 overlap dans
    # bande étroite → no_count élevé → filter à tort). Critère simple
    # overlap_h ou overlap_v >= min_overlap_m via perp_tol_m suffit.
    profile_view = False  # pas utilisé, conservé pour compat evidence

    # Cherche des A-WALL lines dans la zone projetée. **Exclure** les
    # lignes horizontales alignées avec les niveaux (= dalles).
    level_tol_m = 0.10
    edge_dist_m = 0.50  # V3.10 : une verticale est « médiane » si elle
                       # est à ≥ 50cm des 2 bords du x_range. Sinon =
                       # bord de silhouette (coin du bâtiment), pas un
                       # mur à l'intérieur.
    overlap_h = 0.0
    overlap_v = 0.0
    h_lines_count = 0
    v_lines_count = 0
    v_lines_centered = 0
    for line in elevation.a_wall_lines:
        x_l, x_r = sorted((line.p1[0], line.p2[0]))
        y_l, y_h = sorted((line.p1[1], line.p2[1]))
        if y_h < y_min_w - 1e-3 or y_l > y_max_w + 1e-3:
            continue
        if line.is_horizontal:
            y_line = (y_l + y_h) / 2.0
            is_floor_line = any(
                abs(y_line - lv) <= level_tol_m
                for lv in elevation.levels_y
            )
            if is_floor_line:
                continue
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
                    # V3.10 : médiane si à >= edge_dist_m des 2 bords.
                    dist_from_min = x_v - x_min_w
                    dist_from_max = x_max_w - x_v
                    if min(dist_from_min, dist_from_max) >= edge_dist_m:
                        v_lines_centered += 1

    # V3.11 : `zero_lines` = aucune ligne A-WALL du tout dans la zone
    # projetée (= « rien du tout »). User : « 1 seul trait = pas un
    # mur ». A fortiori, 0 traits = certainement pas un mur. Ce flag
    # permet au filter d'identifier les zones vides sans ambiguïté.
    zero_lines = (v_lines_count == 0 and h_lines_count == 0)
    evidence = {
        "wall_x_range": [round(x_min_w, 3), round(x_max_w, 3)],
        "wall_y_range": [round(y_min_w, 3), round(y_max_w, 3)],
        "h_lines_count": h_lines_count,
        "v_lines_count": v_lines_count,
        "v_lines_centered": v_lines_centered,
        "overlap_h_m": round(overlap_h, 3),
        "overlap_v_m": round(overlap_v, 3),
        "profile_view": profile_view,
        "zero_lines": zero_lines,
    }

    # Critère V3.8 final permissif (overlap quelconque suffit) car les
    # critères plus stricts (V3.7/V3.9/V3.10) casent les fusions et
    # régressent sur P7 sans pour autant filtrer correctement les 3 FP
    # restants. Le filter côté tool reste désactivé ; suspects flagués
    # via score 3D consensus.
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


# ----- Votes opening dans élévation -----------------------------------


def vote_opening_visible_in_elevation(
    opening_world: Tuple[float, float],
    level_elevation_m: float,
    sill_m: float,
    height_m: float,
    width_m: float,
    elevation: ElevationView,
    *,
    x_tol_m: float = 0.30,
    y_tol_m: float = 0.30,
) -> Vote:
    """Vote si une opening (porte/fenêtre) est visible dans l'élévation.

    Une opening en élévation laisse :
    - **Fenêtre** : linteau (bande horizontale en haut) + allège (bande
      horizontale en bas) au-dessus et en dessous de la zone.
    - **Porte** : linteau seulement (la zone descend jusqu'au sol).

    Vote yes si : au moins UNE ligne horizontale A-WALL chevauche
    horizontalement la zone projetée de l'opening, ET cette ligne est
    proche du sill ou du head attendu.

    Args:
        opening_world: `(x, y)` position world plan de l'opening.
        level_elevation_m: élévation absolue du niveau (m).
        sill_m: hauteur d'allège (m, au-dessus du niveau).
        height_m: hauteur de la fenêtre/porte (m).
        width_m: largeur (m).
        elevation: `ElevationView` parsée.
        x_tol_m: tolérance horizontale (défaut 30cm).
        y_tol_m: tolérance verticale pour sill / head (défaut 30cm).

    Returns:
        `Vote` avec source `"elevation_<dir>_opening"`.
    """
    if elevation.a_wall_bbox is None:
        return abstain(
            "elevation_{}_opening".format(elevation.direction),
            reason="no A-WALL",
        )

    # Project center of opening (the world point we have).
    cx_elev, _ = project_world_to_elevation(
        opening_world[0], opening_world[1], level_elevation_m,
        elevation.direction,
    )
    x_min = cx_elev - width_m / 2.0
    x_max = cx_elev + width_m / 2.0
    sill_elev_y = level_elevation_m + sill_m
    head_elev_y = level_elevation_m + sill_m + height_m

    bx_min, bx_max, _, _ = elevation.a_wall_bbox
    if x_max < bx_min - x_tol_m or x_min > bx_max + x_tol_m:
        return abstain(
            "elevation_{}_opening".format(elevation.direction),
            reason="outside elevation bbox",
        )

    # Cherche linteau (ligne horizontale à head_elev_y ± y_tol).
    linteau_found = False
    allege_found = False
    for line in elevation.a_wall_lines:
        if not line.is_horizontal:
            continue
        y_line = (line.p1[1] + line.p2[1]) / 2.0
        x_l, x_r = sorted((line.p1[0], line.p2[0]))
        # Overlap horizontal.
        ov = max(0.0, min(x_max, x_r) - max(x_min, x_l))
        if ov < width_m * 0.3:  # exige au moins 30% d'overlap
            continue
        if abs(y_line - head_elev_y) <= y_tol_m:
            linteau_found = True
        if abs(y_line - sill_elev_y) <= y_tol_m and sill_m > 0.1:
            allege_found = True

    if linteau_found:
        # Linteau présent → opening confirmée.
        conf = 0.9 if allege_found else 0.6
        return yes_vote(
            "elevation_{}_opening".format(elevation.direction),
            confidence=conf,
            linteau=True, allege=allege_found,
        )
    if allege_found:
        return yes_vote(
            "elevation_{}_opening".format(elevation.direction),
            confidence=0.5,
            linteau=False, allege=True,
        )
    return no_vote(
        "elevation_{}_opening".format(elevation.direction),
        confidence=0.4,
        linteau=False, allege=False,
    )
