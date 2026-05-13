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

**Architecture V1 : approche par vote multi-sources** (user 2026-05-13).
Chaque source DXF (plan, coupe, élévation) produit un `Vote(answer,
confidence)` sur les hypothèses :
- « Ce candidat est un vrai mur » (plan : paire parallèle + thickness ;
  coupe : présence verticale ; élévation : présence verticale).
- « Ce mur est continu » (plan : INSERT A-GLAZ + width match gap ;
  coupe : block_id + sill/head ; élévation : linteau/allège visibles).
- « Cette opening est porte vs fenêtre » (plan : width parsable ; coupe :
  sill+height extraits ; élévation : linteau seul vs +allège).
Décision finale par majorité (ou poids selon fiabilité). Le V0
plan-only est un cas particulier (1 voix sur 3) — extension naturelle
de `check_planset_integrity` qui agrège déjà des checks votant leur
`severity`.

Brique commune : `validate_via_elevation(walls, elevation_entities,
direction, level_elevations)` projette chaque mur en élévation puis
cherche les patterns A-WALL horizontaux/verticaux dans la zone projetée.



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


# ----- V1 — Openings depuis coupe comme source primaire ---------------
#
# V0 (algo `merge_walls_with_openings` ci-dessus) part des INSERTs A-GLAZ
# du plan pour décider de la fusion. Limites observées runtime P7 :
# - `walls_merged=0` car le block_name parse ne match pas la convention
#   Projet8 (séparateur, espaces, etc.).
# - Tolérances trop strictes sur l'angle / gap.
# - Erreur Revit "ne coupent rien" car position d'opening pas exacte-
#   ment sur la centerline du mur hôte.
#
# V1 : les openings sont lus depuis les COUPES (block_id + position
# x_cut_m + sill_m + height_m), projetés en world plan via la convention
# DXF section anchor (mémoire `project-dxf-section-anchor-investigation`),
# matchés à un mur plan par proximité géométrique, puis utilisés pour
# fusionner les fragments adjacents.
#
# Source primaire = coupe parce que :
# 1. block_id partagé + parsable (Revit AIA export).
# 2. sill+height issus de la coupe + niveau = précis et fiables.
# 3. Pas de dépendance au block_name parse-width côté plan.
# 4. Convention DXF anchor déjà résolue sur P7 (session m).
#
# Limitation : un opening qui n'apparaît dans AUCUNE coupe ne sera pas
# créé. C'est aligné avec le comportement V0 actuel et acceptable V1.


@dataclass
class CoupeOpening:
    """Opening lu depuis un DXF coupe, projeté en world plan.

    Champs :
    - `block_id` : ID Revit partagé (regex sur block_name).
    - `x_world_m`, `y_world_m` : position en world plan (mètres), obtenue
      par projection x_cut_m via la section_line associée.
    - `sill_m`, `height_m` : depuis la coupe (y_dxf - base_level_y, et
      hauteur parsée du block_name).
    - `width_m` : largeur parsée du block_name (None si non parsable —
      pas utilisée pour la fusion V1, juste reportée).
    - `coupe_path` / `section_line_index` : pour traçabilité.
    """
    block_id: Optional[str]
    block_name: str
    x_world_m: float
    y_world_m: float
    sill_m: Optional[float]
    height_m: Optional[float]
    width_m: Optional[float]
    coupe_path: str
    section_line_index: int


def project_section_opening_to_world(
    x_cut_m: float,
    section_line_p1: Tuple[float, float],
    section_line_p2: Tuple[float, float],
) -> Tuple[float, float]:
    """Projette une position x_cut d'un opening en coupe vers world plan.

    Convention DXF section anchor (cf. mémoire
    `project-dxf-section-anchor-investigation`) :
    - Trait **vertical** (cut along world Y) : DXF X = world Y. Position
      world = `(X_trait, x_cut)`.
    - Trait **horizontal** (cut along world X) : DXF X = world X. Position
      world = `(x_cut, Y_trait)`.

    Le sens p1→p2 du trait n'impacte pas la convention (l'origine DXF
    coupe = origine world projet).

    Args:
        x_cut_m: position de l'opening le long du cut, en mètres
            (= `SectionOpening.x_dxf_m`).
        section_line_p1 / p2: endpoints du trait en world plan.

    Returns:
        `(x_world_m, y_world_m)` position de l'opening en world plan.
    """
    trait_dx = section_line_p2[0] - section_line_p1[0]
    trait_dy = section_line_p2[1] - section_line_p1[1]
    if abs(trait_dx) < abs(trait_dy):
        # Trait dominant vertical → x_cut = world Y, X = constante du trait.
        return (section_line_p1[0], x_cut_m)
    else:
        # Trait dominant horizontal → x_cut = world X, Y = constante du trait.
        return (x_cut_m, section_line_p1[1])


def find_host_wall_for_world_opening(
    opening_world: Tuple[float, float],
    walls: List[Any],
    *,
    perp_tol_m: float = 0.05,
) -> Optional[int]:
    """Trouve le mur dont la centerline passe au plus près de l'opening.

    Critère : distance perpendiculaire ≤ `perp_tol_m + wall.thickness/2`
    ET projection à l'intérieur du segment (clamp [0, 1]). Si plusieurs
    candidats matchent, retourne celui à perp distance minimale.

    Args:
        opening_world: `(x, y)` position en world plan.
        walls: liste de WallCandidate (ou MergedWall) avec `p1, p2,
            thickness`.
        perp_tol_m: tolérance perpendiculaire (en plus de demi-thickness).

    Returns:
        Index du mur hôte dans `walls`, ou None si aucun candidat.
    """
    best_idx: Optional[int] = None
    best_perp = float("inf")
    for i, w in enumerate(walls):
        tol = perp_tol_m + w.thickness / 2.0
        perp = _perp_distance_point_to_line(opening_world, w.p1, w.p2)
        if perp > tol:
            continue
        # Doit aussi être dans le segment (clamp 0..1).
        t = _project_param(opening_world, w.p1, w.p2)
        if t < -1e-3 or t > 1.0 + 1e-3:
            continue
        if perp < best_perp:
            best_perp = perp
            best_idx = i
    return best_idx


def project_pos_onto_wall_centerline(
    pos: Tuple[float, float],
    wall_p1: Tuple[float, float],
    wall_p2: Tuple[float, float],
    *,
    margin_m: float = 0.05,
) -> Tuple[float, float]:
    """Projette un point orthogonalement sur la centerline du mur, clampé
    à `[margin_m, length - margin_m]` pour éviter qu'une opening ne se
    retrouve sur les extrémités strictes (Revit refuserait).

    Args:
        pos: `(x, y)` position approximative.
        wall_p1 / wall_p2: endpoints de la centerline.
        margin_m: distance min de chaque extrémité (défaut 5cm).

    Returns:
        `(x, y)` position projetée sur la centerline.
    """
    dx = wall_p2[0] - wall_p1[0]
    dy = wall_p2[1] - wall_p1[1]
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-9:
        return wall_p1
    t = _project_param(pos, wall_p1, wall_p2)
    # Clamp en mètres (margin/length en paramètre normalisé).
    t_min = margin_m / length
    t_max = 1.0 - margin_m / length
    t = max(t_min, min(t_max, t))
    return (wall_p1[0] + t * dx, wall_p1[1] + t * dy)


def merge_fragments_around_opening(
    walls: List[Any],
    opening_world: Tuple[float, float],
    *,
    perp_tol_m: float = 0.05,
    max_fragment_gap_m: float = 3.0,
    angle_tol_rad: float = math.radians(5.0),
) -> Tuple[List[Any], Optional[int]]:
    """Fusionne 2 fragments collinéaires encadrant un opening world.

    Cherche dans `walls` 2 fragments dont les centerlines sont parallèles
    et passent à `perp_tol` de l'opening, dont les endpoints faisant face
    sont distants ≤ `max_fragment_gap_m`, et dont l'opening tombe entre
    les 2 endpoints du milieu.

    **Diffère du V0 `merge_walls_with_openings`** : ici la fusion est
    pilotée par la position WORLD de l'opening (de la coupe), pas par la
    présence d'un INSERT A-GLAZ avec width matchant.

    Args:
        walls: liste de WallCandidate (ou MergedWall).
        opening_world: `(x, y)` position de l'opening en world plan.
        perp_tol_m / max_fragment_gap_m / angle_tol_rad: tolérances.

    Returns:
        `(new_walls, host_index)` :
        - Si fusion : `new_walls` a 1 mur de moins (les 2 fragments sont
          remplacés par 1 mur continu) et `host_index` pointe sur ce
          nouveau mur.
        - Si pas de fusion possible mais l'opening est sur un mur
          intact : `walls` inchangé, `host_index` est l'index de ce mur.
        - Si rien trouvé : `walls` inchangé, `host_index = None`.
    """
    # Candidats : tous les walls dont la centerline est à perp_tol de
    # l'opening (et l'opening est sur le segment ou peu après).
    candidates: List[int] = []
    for i, w in enumerate(walls):
        tol = perp_tol_m + w.thickness / 2.0
        if _perp_distance_point_to_line(opening_world, w.p1, w.p2) > tol:
            continue
        candidates.append(i)

    if not candidates:
        return walls, None

    # Cas 1 : 1 seul candidat → opening sur mur intact, host = ce mur
    # (si l'opening est sur le segment).
    if len(candidates) == 1:
        i = candidates[0]
        t = _project_param(opening_world, walls[i].p1, walls[i].p2)
        if -1e-3 <= t <= 1.0 + 1e-3:
            return walls, i
        # Sinon : l'opening est dans le prolongement du mur sans 2ème
        # fragment → orphan.
        return walls, None

    # Cas 2 : 2+ candidats. Chercher 2 fragments collinéaires (angle
    # similaire) qui encadrent l'opening.
    angles = {i: _angle_mod_pi(walls[i].p1, walls[i].p2) for i in candidates}
    for ci, i in enumerate(candidates):
        for j in candidates[ci + 1:]:
            if not _angles_close(angles[i], angles[j], angle_tol_rad):
                continue
            # Project tous les 4 endpoints sur la droite portant walls[i].
            endpoints = [
                (walls[i].p1, "p1_i"), (walls[i].p2, "p2_i"),
                (walls[j].p1, "p1_j"), (walls[j].p2, "p2_j"),
            ]
            endpoints_by_t = sorted(
                endpoints,
                key=lambda e: _project_param(e[0], walls[i].p1, walls[i].p2),
            )
            inner_a, inner_b = endpoints_by_t[1], endpoints_by_t[2]
            gap_dx = inner_b[0][0] - inner_a[0][0]
            gap_dy = inner_b[0][1] - inner_a[0][1]
            gap_m = math.sqrt(gap_dx * gap_dx + gap_dy * gap_dy)
            if gap_m > max_fragment_gap_m:
                continue
            # L'opening doit être dans le gap (entre les 2 endpoints
            # intérieurs).
            t_inner_a = _project_param(inner_a[0], walls[i].p1, walls[i].p2)
            t_inner_b = _project_param(inner_b[0], walls[i].p1, walls[i].p2)
            t_op = _project_param(opening_world, walls[i].p1, walls[i].p2)
            t_lo = min(t_inner_a, t_inner_b)
            t_hi = max(t_inner_a, t_inner_b)
            if not (t_lo - 1e-3 <= t_op <= t_hi + 1e-3):
                continue
            # Fusion : nouveau mur entre les 2 endpoints extrêmes.
            outer_a = endpoints_by_t[0][0]
            outer_b = endpoints_by_t[3][0]
            thickness_merged = (walls[i].thickness + walls[j].thickness) / 2.0
            # Reuse MergedWall — accepte WallCandidate.layer / .confidence.
            merged_wall = MergedWall(
                p1=outer_a,
                p2=outer_b,
                thickness=thickness_merged,
                layer=walls[i].layer,
                confidence=min(walls[i].confidence, walls[j].confidence),
                source_indices=[i, j],
            )
            # Construit la nouvelle liste : remove i et j, append merged.
            new_walls = [w for k, w in enumerate(walls) if k != i and k != j]
            host_index = len(new_walls)
            new_walls.append(merged_wall)
            return new_walls, host_index

    # Pas de fusion possible. Si un des candidats contient l'opening en
    # projection, on l'utilise.
    for i in candidates:
        t = _project_param(opening_world, walls[i].p1, walls[i].p2)
        if -1e-3 <= t <= 1.0 + 1e-3:
            return walls, i
    return walls, None


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
