"""wall_inter_level_dedup.py — supprime les murs « fantômes » d'un niveau
supérieur qui dupliquent géométriquement des murs d'un niveau inférieur.

**Pourquoi** : un plan d'étage exporté depuis Revit montre par défaut les
éléments à et **sous** le niveau (View Range default). Un mur entre N0 et
N1 (assigné à N0 dans Revit) apparaît donc dans le DXF du plan N1. Si
notre import naïvement crée un mur N1 par LINE A-WALL trouvée, il
duplique le mur N0 → l'user voit « des murs en trop au niveau N ».

**Algo (par niveau du plus bas au plus haut)** :
1. Grouper les murs collinéaires (même épaisseur, centerlines sur la
   même ligne infinie à `perp_tol` près).
2. Pour chaque mur W du niveau courant, le projeter sur la ligne
   commune et calculer l'intervalle `[t1, t2]` de sa centerline.
3. Calculer l'union des intervalles couverts par les murs (tous niveaux
   inférieurs confondus, mêmes épaisseur + ligne). Si W est inclus
   dans cette union (à `join_tol` près pour les joints), il est marqué
   comme duplicate → skip.

Pure Python, pas d'import Revit, pas d'I/O. Testable hors-Revit avec des
DataClass de murs synthétiques (cf. tests/test_wall_inter_level_dedup.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


_EPSILON = 1e-9


@dataclass
class _CenterlineKey:
    """Identifiant d'une ligne infinie + thickness (groupage des murs)."""
    thickness_bucket_mm: int     # épaisseur arrondie au mm
    angle_quadrant: int          # 0..3 (quadrant pour normaliser direction)
    normal_dot: int              # signé*1000, distance à l'origine sur la normale


def _segment_length(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _unit_axis(
    p1: Tuple[float, float], p2: Tuple[float, float],
) -> Tuple[float, float]:
    """Axe unitaire orienté de p1 vers p2. Si segment dégénéré, retourne (1,0)."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    if L < _EPSILON:
        return (1.0, 0.0)
    return (dx / L, dy / L)


def _are_collinear(
    w1_p1: Tuple[float, float], w1_p2: Tuple[float, float],
    w2_p1: Tuple[float, float], w2_p2: Tuple[float, float],
    perp_tol_m: float,
) -> bool:
    """True si w2 est sur la même ligne infinie que w1 (centerline à
    centerline, à `perp_tol_m` près perpendiculairement).

    Test : projeter w2.p1 et w2.p2 sur la normale unitaire de w1 ; les
    deux distances doivent être proches de 0 (= w2 colinéaire à w1).
    """
    ax, ay = _unit_axis(w1_p1, w1_p2)
    # Normale unitaire de w1 (rotation 90°).
    nx, ny = -ay, ax
    # Distance perpendiculaire de chaque endpoint de w2 à la ligne de w1.
    d1 = (w2_p1[0] - w1_p1[0]) * nx + (w2_p1[1] - w1_p1[1]) * ny
    d2 = (w2_p2[0] - w1_p1[0]) * nx + (w2_p2[1] - w1_p1[1]) * ny
    return abs(d1) <= perp_tol_m and abs(d2) <= perp_tol_m


def _project_endpoint_onto_axis(
    pt: Tuple[float, float],
    origin: Tuple[float, float],
    axis: Tuple[float, float],
) -> float:
    """Abscisse signée de `pt` projeté sur l'axe d'origine `origin`."""
    return (pt[0] - origin[0]) * axis[0] + (pt[1] - origin[1]) * axis[1]


def _union_intervals(
    intervals: List[Tuple[float, float]],
    join_tol_m: float,
) -> List[Tuple[float, float]]:
    """Fusionne les intervalles qui se touchent (gap ≤ `join_tol_m`).
    Retourne la liste des intervalles unionisés, triés.
    """
    if not intervals:
        return []
    sorted_iv = sorted(intervals)
    out: List[Tuple[float, float]] = [sorted_iv[0]]
    for lo, hi in sorted_iv[1:]:
        last_lo, last_hi = out[-1]
        if lo <= last_hi + join_tol_m:
            out[-1] = (last_lo, max(last_hi, hi))
        else:
            out.append((lo, hi))
    return out


def _is_top_wall_covered_by_lower(
    w_top_p1: Tuple[float, float], w_top_p2: Tuple[float, float],
    w_top_thickness: float,
    lower_walls: List[Any],
    *,
    thickness_tol_m: float,
    perp_tol_m: float,
    join_tol_m: float,
    coverage_tol_m: float,
) -> bool:
    """True si w_top (du niveau N+) est géométriquement couvert par
    l'union d'un ou plusieurs murs de `lower_walls` collinéaires.

    `lower_walls` : liste d'objets ducktyped avec `.p1, .p2, .thickness`.
    """
    # Filtrer les lower_walls collinéaires avec w_top et d'épaisseur compatible.
    collinear_lower: List[Any] = []
    for w_low in lower_walls:
        if abs(w_low.thickness - w_top_thickness) > thickness_tol_m:
            continue
        if not _are_collinear(
            w_top_p1, w_top_p2,
            (w_low.p1[0], w_low.p1[1]),
            (w_low.p2[0], w_low.p2[1]),
            perp_tol_m,
        ):
            continue
        collinear_lower.append(w_low)
    if not collinear_lower:
        return False

    # Référentiel : axe de w_top, origin = w_top.p1.
    axis = _unit_axis(w_top_p1, w_top_p2)
    origin = w_top_p1
    top_t1 = 0.0
    top_t2 = _segment_length(w_top_p1, w_top_p2)
    if top_t2 < _EPSILON:
        # Segment dégénéré : considéré couvert si au moins 1 lower wall
        # collinear contient son point.
        return True

    # Intervalles projetés des lower walls collinéaires sur l'axe.
    lower_intervals: List[Tuple[float, float]] = []
    for w_low in collinear_lower:
        t1 = _project_endpoint_onto_axis(
            (w_low.p1[0], w_low.p1[1]), origin, axis,
        )
        t2 = _project_endpoint_onto_axis(
            (w_low.p2[0], w_low.p2[1]), origin, axis,
        )
        lo, hi = (t1, t2) if t1 <= t2 else (t2, t1)
        lower_intervals.append((lo, hi))

    union = _union_intervals(lower_intervals, join_tol_m)
    for lo, hi in union:
        if lo <= top_t1 + coverage_tol_m and hi >= top_t2 - coverage_tol_m:
            return True
    return False


def dedup_inter_level_walls(
    walls_by_level: List[Tuple[float, List[Any]]],
    *,
    thickness_tol_m: float = 0.005,
    perp_tol_m: float = 0.05,
    join_tol_m: float = 0.10,
    coverage_tol_m: float = 0.05,
    full_coverage_only: bool = True,
    top_level_only: bool = True,
) -> Tuple[List[Tuple[float, List[Any]]], List[Dict[str, Any]]]:
    """Supprime les murs « fantômes » d'un niveau supérieur qui
    dupliquent géométriquement des murs d'un niveau inférieur (effet
    View Range Revit).

    **Mode `top_level_only=True` (défaut)** : ne dédoublonne **que le
    niveau le plus haut** contre l'union de tous les niveaux inférieurs.
    Les niveaux intermédiaires gardent tous leurs murs même si 100%
    matche le niveau d'en-dessous (= étage habitable avec murs porteurs
    légitimement empilés, cf. P2 N1 « apartment level »).

    **Mode `full_coverage_only=True` (défaut, combiné avec top_level_only)** :
    un niveau n'est dédoublonné que si **100% de ses murs sont des
    doublons** (= « niveau toiture fantôme » typique). Si seulement une
    partie des murs duplique → on garde tout (signale empilage volontaire,
    cf. P7 où 5/10 murs N1 stackent N0 mais sont des murs porteurs).

    **Pourquoi top_level_only par défaut** : un faux positif observé sur
    P2 a montré qu'un niveau intermédiaire (apartment) peut avoir 100%
    de ses murs identiques à un niveau inférieur (la base sans cloisons
    intérieures) sans que ce soit un artifact View Range. Le top level
    est le seul cas où « 100% identique » signale fiablement un plan
    fantôme (toiture/terrasse). Mettre `top_level_only=False` pour
    revenir à l'ancien mode (vérifier tous les niveaux au-dessus du
    plus bas, plus agressif, plus risqué).

    Args:
        walls_by_level: liste de `(elevation_m, [walls])`. Pas trié —
            la fonction trie par elevation asc. Walls sont ducktyped :
            `.p1`, `.p2`, `.thickness`.
        thickness_tol_m: tolérance d'épaisseur (défaut 5 mm).
        perp_tol_m: tolérance perpendiculaire pour collinéarité (défaut
            5 cm).
        join_tol_m: gap max entre 2 intervalles lower pour les fusionner
            (défaut 10 cm).
        coverage_tol_m: extension de l'intervalle top pour considérer
            couvert (défaut 5 cm).
        full_coverage_only: si True (défaut), dédup ne se déclenche que
            si 100% des murs du niveau testé matche.
        top_level_only: si True (défaut), seul le niveau le plus haut
            est testé pour dédoublonnage. Les niveaux intermédiaires
            sont préservés tels quels. Mettre False pour vérifier tous
            les niveaux au-dessus du plus bas (ancien comportement,
            plus agressif).

    Returns:
        `(filtered_walls_by_level, dedup_events)`.
    """
    if not walls_by_level:
        return [], []

    # Trie par elevation asc, et copie défensive des listes.
    sorted_by_elev = sorted(
        [(e, list(walls)) for e, walls in walls_by_level],
        key=lambda x: x[0],
    )
    if len(sorted_by_elev) == 1:
        return list(sorted_by_elev), []

    events: List[Dict[str, Any]] = []
    filtered: List[Tuple[float, List[Any]]] = []

    if top_level_only:
        # Tous les niveaux sauf le top → preserved tels quels.
        for elev, walls in sorted_by_elev[:-1]:
            filtered.append((elev, list(walls)))
        # Le top niveau est testé contre l'union de tous les niveaux
        # inférieurs.
        accumulated_lower: List[Any] = []
        for _, walls in sorted_by_elev[:-1]:
            accumulated_lower.extend(walls)
        top_elev, top_walls = sorted_by_elev[-1]
        kept, level_events = _dedup_one_level_against_lower(
            top_elev, top_walls, accumulated_lower,
            thickness_tol_m=thickness_tol_m,
            perp_tol_m=perp_tol_m,
            join_tol_m=join_tol_m,
            coverage_tol_m=coverage_tol_m,
            full_coverage_only=full_coverage_only,
        )
        filtered.append((top_elev, kept))
        events.extend(level_events)
        return filtered, events

    # Mode all-above (legacy) : check every level above the lowest.
    accumulated_lower = list(sorted_by_elev[0][1])
    filtered.append((sorted_by_elev[0][0], list(sorted_by_elev[0][1])))
    for i in range(1, len(sorted_by_elev)):
        elev, walls = sorted_by_elev[i]
        kept, level_events = _dedup_one_level_against_lower(
            elev, walls, accumulated_lower,
            thickness_tol_m=thickness_tol_m,
            perp_tol_m=perp_tol_m,
            join_tol_m=join_tol_m,
            coverage_tol_m=coverage_tol_m,
            full_coverage_only=full_coverage_only,
        )
        filtered.append((elev, kept))
        events.extend(level_events)
        accumulated_lower.extend(kept)
    return filtered, events


def _dedup_one_level_against_lower(
    elev: float,
    walls: List[Any],
    lower_walls: List[Any],
    *,
    thickness_tol_m: float,
    perp_tol_m: float,
    join_tol_m: float,
    coverage_tol_m: float,
    full_coverage_only: bool,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Pour un niveau donné, retourne `(kept_walls, events)` selon la
    stratégie de dédup choisie (full_coverage_only ou individuel)."""
    if not walls:
        return [], []
    covered_flags: List[bool] = []
    for w in walls:
        covered = _is_top_wall_covered_by_lower(
            (w.p1[0], w.p1[1]), (w.p2[0], w.p2[1]), w.thickness,
            lower_walls,
            thickness_tol_m=thickness_tol_m,
            perp_tol_m=perp_tol_m,
            join_tol_m=join_tol_m,
            coverage_tol_m=coverage_tol_m,
        )
        covered_flags.append(covered)

    all_covered = all(covered_flags)
    kept: List[Any] = []
    events: List[Dict[str, Any]] = []
    for w, covered in zip(walls, covered_flags):
        should_skip = covered and (
            all_covered if full_coverage_only else True
        )
        if should_skip:
            events.append({
                "level_elev_m": round(elev, 4),
                "p1": [round(w.p1[0], 4), round(w.p1[1], 4)],
                "p2": [round(w.p2[0], 4), round(w.p2[1], 4)],
                "thickness_m": round(w.thickness, 4),
                "reason": (
                    "level_fully_covered_by_lower"
                    if full_coverage_only
                    else "covered_by_lower_level_walls"
                ),
            })
        else:
            kept.append(w)
    return kept, events
