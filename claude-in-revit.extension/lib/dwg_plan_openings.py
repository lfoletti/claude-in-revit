"""dwg_plan_openings.py — fusion fragments mur ↔ INSERTs A-GLAZ (Phase 2.5).

**Convention runtime** (user 2026-05-13) : le trait d'interruption en
plan peut indiquer soit une fenêtre, soit une porte, soit une fin de
mur. La plupart du temps porte/fenêtre, parfois rien. Cet algo se base
sur la présence d'un INSERT A-GLAZ pour distinguer :
- **Avec INSERT A-GLAZ** dont `width_m` matche le gap des fragments →
  fusion + opening hosted.
- **Sans INSERT A-GLAZ** → vraie discontinuité, fragments restent
  distincts. Pas de fusion accidentelle.

**TODO V1 — validation/raffinement par élévation** (user 2026-05-13).
La lecture en élévation est l'arbitre final pour 3 cas que l'algo
plan-only ne couvre pas :

1. **Continuité d'un mur** : un mur continu avec fenêtre montre en
   élévation linteau (bande haute) + allège (bande basse) ; avec porte,
   linteau seul. Une vraie rupture = pas de bande horizontale. À utiliser
   pour downgrade les fusions plan-only sans confirmation visuelle.

2. **Faux positifs murs** : des paires parallèles en plan peuvent
   correspondre à un changement de matériau (joint, séparation
   visuelle), pas un vrai mur. L'élévation tranche : pas de présence
   verticale → pas un mur. À filtrer côté wall candidates.

3. **Hauteur des murs** (murets vs pleine hauteur) : un muret monte
   à 0.5-1m, un vrai mur monte à la hauteur d'étage. L'élévation
   fournit cette information par projection du mur dans la zone
   correspondante. À utiliser pour set `height` correct par mur
   (au lieu d'un `height_m` global).

Brique commune V1 : `validate_via_elevation(walls, elevation_entities,
elevation_direction, level_elevations)` qui :
- Projette chaque mur en élévation (mapping coordonnées plan → DXF
  élévation selon `direction` ∈ {Est, Nord, Sud, Ouest}).
- Cherche des lignes A-WALL horizontales/verticales dans la zone projetée.
- Retourne pour chaque mur : `is_valid: bool`, `height_m: float`,
  `openings_confirmed: List[bool]`.



Problème runtime constaté 2026-05-13 (P7) : le classifier
`dwg_classifier.detect_wall_segments` voit une fenêtre dessinée par
**interruption des 2 lignes du mur** comme **2 fragments distincts**.
Résultat 3D : murs discontinus à chaque fenêtre/porte, au lieu d'1 mur
continu + 1 opening Revit. User : « les openings devraient être
évaluées en même temps pour éviter la confusion entre une interruption
du mur et la présence d'une ouverture dans un mur continu ».

Ce module pur fournit `merge_walls_with_openings()` qui :

1. Pour chaque INSERT A-GLAZ avec `width_m` parsable depuis le block name,
   cherche les 2 fragments de mur dont la centerline passe au point
   d'insertion de l'INSERT (à `perp_tol_m` près).
2. Si exactement 2 fragments → vérifie qu'ils sont collinéaires (même
   ligne portante) et que leurs endpoints faisant face sont à
   distance ≈ `width_m`.
3. Si OK : fusionne en 1 mur continu (endpoints extrêmes des 2 fragments)
   et assigne l'opening à ce mur (`host_wall_index`).
4. Si 1 seul fragment trouvé → opening est sur le mur intact.
5. Sinon → opening orphelin (skipped, signalé au caller).

Pas d'I/O fichier, pas d'import Revit. Module pure-Python testable
hors-Revit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# ----- Géométrie 2D ----------------------------------------------------


def _perp_distance_point_to_line(
    point: Tuple[float, float],
    line_p1: Tuple[float, float],
    line_p2: Tuple[float, float],
) -> float:
    """Distance perpendiculaire absolue d'un point à la droite portant
    le segment (line_p1, line_p2)."""
    dx = line_p2[0] - line_p1[0]
    dy = line_p2[1] - line_p1[1]
    norm = math.sqrt(dx * dx + dy * dy)
    if norm < 1e-12:
        return float("inf")
    nx = -dy / norm
    ny = dx / norm
    return abs((point[0] - line_p1[0]) * nx + (point[1] - line_p1[1]) * ny)


def _project_param(
    point: Tuple[float, float],
    line_p1: Tuple[float, float],
    line_p2: Tuple[float, float],
) -> float:
    """Paramètre `t` de la projection du point sur la droite portant
    le segment, mesuré depuis `line_p1` le long de `(p2 - p1)`. `t=0` à
    `line_p1`, `t=1` à `line_p2`, t>1 au-delà."""
    dx = line_p2[0] - line_p1[0]
    dy = line_p2[1] - line_p1[1]
    norm_sq = dx * dx + dy * dy
    if norm_sq < 1e-24:
        return 0.0
    return (
        (point[0] - line_p1[0]) * dx + (point[1] - line_p1[1]) * dy
    ) / norm_sq


def _angle_mod_pi(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """Angle du segment dans [0, π)."""
    a = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    if a < 0:
        a += math.pi
    if a >= math.pi:
        a -= math.pi
    return a


def _angles_close(a: float, b: float, tol: float) -> bool:
    """Comparaison angulaire modulo π."""
    d = abs(a - b)
    return min(d, math.pi - d) <= tol


# ----- Schémas de retour ----------------------------------------------


@dataclass
class MergedWall:
    """Mur continu après fusion éventuelle de fragments.

    Si `source_indices` contient plusieurs index, c'est une fusion ;
    sinon le mur est intact (1 fragment d'origine).
    """
    p1: Tuple[float, float]
    p2: Tuple[float, float]
    thickness: float
    layer: str
    confidence: float
    source_indices: List[int] = field(default_factory=list)


@dataclass
class AssignedOpening:
    """Opening (INSERT A-GLAZ) avec son mur hôte.

    `host_wall_index` : index dans la liste `MergedWall` retournée. None
    si l'opening n'a pas pu être hosté (signalé au caller).
    `position_along_wall_m` : distance depuis `wall.p1` le long de la
    centerline du mur — utilisable directement comme position pour
    `openings_create_*` (mais Revit attend [x, y] world, à convertir
    après).
    """
    block_id: Optional[str]
    block_name: str
    x_dxf_m: float
    y_dxf_m: float
    width_m: Optional[float]
    height_m_blockname: Optional[float]
    host_wall_index: Optional[int]
    position_along_wall_m: Optional[float]
    reason: str  # "merged_two_fragments" | "single_wall_intact" | "orphaned_no_match"


# ----- Algorithme principal --------------------------------------------


def merge_walls_with_openings(
    walls: List[Any],
    plan_openings: List[Any],
    *,
    perp_tol_m: float = 0.10,
    width_match_tol_m: float = 0.15,
    angle_tol_rad: float = math.radians(3.0),
) -> Tuple[List[MergedWall], List[AssignedOpening]]:
    """Fusionne les fragments de mur interrompus par des openings A-GLAZ.

    Args:
        walls: liste de `WallCandidate` (cf. `dwg_classifier.WallCandidate`)
            — doit exposer `p1, p2, thickness, layer, confidence`.
        plan_openings: liste de `SectionOpening` (cf.
            `dwg_section_reader.SectionOpening`) — doit exposer
            `block_id, block_name, x_dxf_m, y_dxf_m, width_m, height_m`.
            Ce sont les INSERTs A-GLAZ du plan (pas des coupes).
        perp_tol_m: tolérance perpendiculaire pour considérer qu'un
            opening est « sur la ligne » d'un fragment de mur (en plus
            de la demi-épaisseur du mur).
        width_match_tol_m: tolérance entre la `width_m` du block et le
            gap observé entre 2 fragments.
        angle_tol_rad: tolérance d'angle pour 2 fragments collinéaires.

    Returns:
        `(merged_walls, assigned_openings)`. `merged_walls` est la nouvelle
        liste de murs (peut être plus courte que `walls` si fusions).
        Chaque `AssignedOpening` porte `host_wall_index` dans la nouvelle
        liste — ou None si orphelin.
    """
    n = len(walls)
    used: Set[int] = set()
    merged: List[MergedWall] = []
    # Mapping old_wall_index → new_wall_index dans `merged`.
    old_to_new: Dict[int, int] = {}
    assigned: List[AssignedOpening] = []

    # Pre-compute angles pour chaque mur (sur la centerline).
    angles = [_angle_mod_pi(w.p1, w.p2) for w in walls]

    # ----- Passe 1 : pour chaque opening avec width parsable, chercher
    # les 2 fragments à fusionner.
    for o in plan_openings:
        if o.width_m is None or o.width_m <= 0:
            continue
        # Candidats : fragments dont la centerline passe à perp_tol
        # (+ demi-épaisseur) de l'opening.
        op_pt = (o.x_dxf_m, o.y_dxf_m)
        candidates: List[int] = []
        for i, w in enumerate(walls):
            if i in used:
                continue
            tol = perp_tol_m + w.thickness / 2.0
            if _perp_distance_point_to_line(op_pt, w.p1, w.p2) <= tol:
                candidates.append(i)
        if len(candidates) != 2:
            continue  # cas géré en passe 3
        i, j = candidates
        if walls[i].layer != walls[j].layer:
            continue
        if not _angles_close(angles[i], angles[j], angle_tol_rad):
            continue
        # Project les 4 endpoints sur la droite portant `walls[i]`.
        # Trier par paramètre t. Les 2 du milieu doivent être à
        # distance ≈ width_m, et leur milieu doit être ≈ op_pt.
        endpoints = [
            (walls[i].p1, i, "p1"),
            (walls[i].p2, i, "p2"),
            (walls[j].p1, j, "p1"),
            (walls[j].p2, j, "p2"),
        ]
        endpoints_by_t = sorted(
            endpoints,
            key=lambda e: _project_param(e[0], walls[i].p1, walls[i].p2),
        )
        # Distance entre les 2 endpoints du milieu.
        inner_a, inner_b = endpoints_by_t[1], endpoints_by_t[2]
        gap_dx = inner_b[0][0] - inner_a[0][0]
        gap_dy = inner_b[0][1] - inner_a[0][1]
        gap_m = math.sqrt(gap_dx * gap_dx + gap_dy * gap_dy)
        if abs(gap_m - o.width_m) > width_match_tol_m:
            continue
        # Vérifie que l'opening est entre les 2 endpoints du milieu.
        inner_mid = (
            (inner_a[0][0] + inner_b[0][0]) / 2.0,
            (inner_a[0][1] + inner_b[0][1]) / 2.0,
        )
        if _perp_distance_point_to_line(
            op_pt, inner_a[0], inner_b[0],
        ) > perp_tol_m + walls[i].thickness / 2.0:
            continue
        # OK fusion.
        outer_a, outer_b = endpoints_by_t[0][0], endpoints_by_t[3][0]
        thickness_merged = (walls[i].thickness + walls[j].thickness) / 2.0
        confidence_merged = min(walls[i].confidence, walls[j].confidence)
        merged_wall = MergedWall(
            p1=outer_a,
            p2=outer_b,
            thickness=thickness_merged,
            layer=walls[i].layer,
            confidence=confidence_merged,
            source_indices=[i, j],
        )
        new_idx = len(merged)
        merged.append(merged_wall)
        old_to_new[i] = new_idx
        old_to_new[j] = new_idx
        used.add(i)
        used.add(j)
        # Position le long du nouveau mur : projection de op_pt.
        pos_along = _project_param(
            op_pt, merged_wall.p1, merged_wall.p2,
        ) * math.sqrt(
            (merged_wall.p2[0] - merged_wall.p1[0]) ** 2
            + (merged_wall.p2[1] - merged_wall.p1[1]) ** 2
        )
        assigned.append(AssignedOpening(
            block_id=o.block_id,
            block_name=o.block_name,
            x_dxf_m=o.x_dxf_m,
            y_dxf_m=o.y_dxf_m,
            width_m=o.width_m,
            height_m_blockname=o.height_m,
            host_wall_index=new_idx,
            position_along_wall_m=pos_along,
            reason="merged_two_fragments",
        ))

    # ----- Passe 2 : ajouter les fragments restants (non fusionnés)
    # comme murs intacts.
    for i, w in enumerate(walls):
        if i in used:
            continue
        new_idx = len(merged)
        merged.append(MergedWall(
            p1=w.p1,
            p2=w.p2,
            thickness=w.thickness,
            layer=w.layer,
            confidence=w.confidence,
            source_indices=[i],
        ))
        old_to_new[i] = new_idx

    # ----- Passe 3 : pour les openings non encore assignés, chercher
    # un mur intact qui les contient (cas : opening sur un mur sans
    # fragmentation visible).
    already_assigned_block_keys: Set[Tuple[float, float]] = {
        (a.x_dxf_m, a.y_dxf_m) for a in assigned
    }
    for o in plan_openings:
        key = (o.x_dxf_m, o.y_dxf_m)
        if key in already_assigned_block_keys:
            continue
        op_pt = (o.x_dxf_m, o.y_dxf_m)
        # Cherche un mur dont la centerline contient l'opening (perp tol
        # + projection dans [0, 1] sur le segment).
        host_idx: Optional[int] = None
        host_pos: Optional[float] = None
        for nidx, w in enumerate(merged):
            tol = perp_tol_m + w.thickness / 2.0
            if _perp_distance_point_to_line(op_pt, w.p1, w.p2) > tol:
                continue
            t = _project_param(op_pt, w.p1, w.p2)
            if 0.0 - 1e-6 <= t <= 1.0 + 1e-6:
                wall_length = math.sqrt(
                    (w.p2[0] - w.p1[0]) ** 2 + (w.p2[1] - w.p1[1]) ** 2
                )
                host_idx = nidx
                host_pos = t * wall_length
                break
        reason = (
            "single_wall_intact" if host_idx is not None else "orphaned_no_match"
        )
        assigned.append(AssignedOpening(
            block_id=o.block_id,
            block_name=o.block_name,
            x_dxf_m=o.x_dxf_m,
            y_dxf_m=o.y_dxf_m,
            width_m=o.width_m,
            height_m_blockname=o.height_m,
            host_wall_index=host_idx,
            position_along_wall_m=host_pos,
            reason=reason,
        ))

    return merged, assigned


# ----- Classification porte vs fenêtre --------------------------------


def classify_opening_kind(
    sill_m: Optional[float],
    height_m: Optional[float],
    *,
    door_sill_max_m: float = 0.15,
    door_height_min_m: float = 1.9,
) -> str:
    """Distingue porte vs fenêtre depuis sill + height (issus typiquement
    d'un match coupe).

    User 2026-05-13 : « si sill <= 0.15m et height >= 1.9m alors porte,
    sinon fenêtre ».

    Args:
        sill_m: hauteur d'allège en mètres (None si inconnu).
        height_m: hauteur de l'ouverture en mètres (None si inconnu).
        door_sill_max_m: seuil sill pour porte (défaut 0.15m).
        door_height_min_m: seuil hauteur pour porte (défaut 1.9m).

    Returns:
        `"door"` | `"window"` | `"unknown"` (si sill ou height manquant
        — l'opening ne pourra pas être créé en Revit).
    """
    if sill_m is None or height_m is None:
        return "unknown"
    if sill_m <= door_sill_max_m and height_m >= door_height_min_m:
        return "door"
    return "window"
