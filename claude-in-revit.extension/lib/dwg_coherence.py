"""dwg_coherence.py — recoupement plan ↔ coupes / élévations (Phase 2 étape 1).

Module pur : pas d'I/O fichier, pas d'import Revit. Conçu pour devenir
le siège du futur `check_planset_coherence` qui agrégera tous les
checks de cohérence d'un dossier DXF (plan ↔ coupes ↔ élévations) —
cf. mémoire `project-planset-coherence-byproduct`.

V0 (Phase 2 étape 1) : 1 fonction `reconcile_plan_section_walls(...)`
qui croise les murs détectés au plan avec ceux observés dans chaque
coupe, via les section_lines persistées au `DxfImportContext`.

Convention DXF coupe → plan (cf. mémoire `project-dxf-section-anchor-
investigation`) :
- Trait vertical (cut along world Y) : DXF X de la coupe = world Y.
- Trait horizontal (cut along world X) : DXF X de la coupe = world X.

Pour une intersection mur ↔ trait en `(X_w, Y_w)` :
- Trait vertical → x_cut_attendu = Y_w
- Trait horizontal → x_cut_attendu = X_w
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ----- Géométrie 2D ----------------------------------------------------


def _segment_intersection_2d(
    p1: Tuple[float, float], p2: Tuple[float, float],
    q1: Tuple[float, float], q2: Tuple[float, float],
    eps: float = 1e-9,
) -> Optional[Tuple[float, float, float, float]]:
    """Intersection stricte de 2 segments 2D.

    Args:
        p1, p2: premier segment.
        q1, q2: second segment.
        eps: tolérance numérique pour parallélisme et bornes.

    Returns:
        `(x, y, t, u)` où `(x, y)` est le point d'intersection,
        `t ∈ [0, 1]` sa position normalisée sur p, `u ∈ [0, 1]` sur q.
        None si parallèles ou si l'intersection tombe hors d'un des
        deux segments (avec tolérance `eps`).
    """
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    sx, sy = q2[0] - q1[0], q2[1] - q1[1]
    denom = rx * sy - ry * sx
    if abs(denom) < eps:
        return None
    qx, qy = q1[0] - p1[0], q1[1] - p1[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    if t < -eps or t > 1 + eps or u < -eps or u > 1 + eps:
        return None
    return (p1[0] + t * rx, p1[1] + t * ry, t, u)


# ----- Schémas de retour ----------------------------------------------


@dataclass
class WallSectionMatch:
    """Un mur plan croisé par un trait de coupe, avec son pendant
    éventuel dans le DXF coupe.

    `status` :
    - `"ok"` : match unique, drift d'épaisseur ≤ tolérance.
    - `"thickness_mismatch"` : match unique mais drift > tolérance.
    - `"no_section_wall_at_x"` : pas de mur en coupe à cette position.
    - `"ambiguous_multiple_candidates"` : plusieurs murs en coupe dans
      la fenêtre de tolérance (ex : mur principal + linteau ou allège
      à la même abscisse). Le primary candidat (extension verticale max)
      est retenu, mais le statut signale l'ambiguïté.
    """
    plan_wall_index: int
    plan_thickness_m: float
    section_line_index: int
    section_line_name: Optional[str]
    coupe_path: str
    x_cut_expected_m: float
    section_wall_index: Optional[int]
    section_thickness_m: Optional[float]
    thickness_drift_m: Optional[float]
    status: str
    candidate_indices: List[int] = field(default_factory=list)


@dataclass
class WallsReconciliation:
    """Rapport de recoupement plan ↔ coupes."""
    matches: List[WallSectionMatch]
    walls_plan_not_crossed: List[int]
    section_walls_unmatched: List[Dict[str, Any]]
    summary: Dict[str, int]


# ----- Reconciliation -------------------------------------------------


def reconcile_plan_section_walls(
    plan_walls: List[Dict[str, Any]],
    section_lines: List[Dict[str, Any]],
    section_walls_by_coupe: Dict[str, List[Dict[str, Any]]],
    *,
    thickness_tol_m: float = 0.02,
    x_cut_tol_m: float = 0.10,
) -> WallsReconciliation:
    """Croise les murs du plan avec ceux observés dans chaque coupe.

    Pour chaque mur du plan, on cherche les intersections strictes avec
    chaque trait de coupe (segments 2D). Pour chaque intersection, on
    calcule `x_cut_expected` selon l'orientation du trait, puis on
    cherche un `SectionWall` à cette abscisse dans le DXF coupe
    correspondant. On compare alors les épaisseurs.

    Args:
        plan_walls: liste de dicts {p1: [x,y], p2: [x,y],
            thickness_m: float, …}. Output de `dwg_classify` (champ
            `walls`) ou de `_wall_candidate_to_dict`.
        section_lines: liste de dicts {plan_p1: [x,y], plan_p2: [x,y],
            view_dir: str, coupe_path: str, name: str?}. Output
            `dxf_context_get().section_lines`.
        section_walls_by_coupe: `{coupe_path: [{x_cut_m, thickness_m,
            y_bottom_m, y_top_m, …}, …]}`. Output de `read_section_walls`
            sérialisé (un par coupe DXF référencée).
        thickness_tol_m: tolérance d'épaisseur pour valider un match
            (défaut 2 cm).
        x_cut_tol_m: tolérance de position le long du cut (défaut
            10 cm).

    Returns:
        `WallsReconciliation` avec :
        - `matches` : un `WallSectionMatch` par intersection trouvée.
        - `walls_plan_not_crossed` : indices des murs plan qui ne
          croisent aucun trait (normal — ils ne sont pas en coupe).
        - `section_walls_unmatched` : murs coupe sans pendant au plan
          (suspect — peut indiquer un mur oublié dans le plan).
        - `summary` : compteurs agrégés par statut.
    """
    matches: List[WallSectionMatch] = []
    walls_plan_not_crossed: List[int] = []
    matched_section_walls: Dict[str, set] = {
        p: set() for p in section_walls_by_coupe
    }

    for pi, pw in enumerate(plan_walls):
        p1 = (float(pw["p1"][0]), float(pw["p1"][1]))
        p2 = (float(pw["p2"][0]), float(pw["p2"][1]))
        thickness = float(pw.get("thickness_m", pw.get("thickness", 0.0)))

        crossed_any = False
        for si, sl in enumerate(section_lines):
            sp1 = (float(sl["plan_p1"][0]), float(sl["plan_p1"][1]))
            sp2 = (float(sl["plan_p2"][0]), float(sl["plan_p2"][1]))

            inter = _segment_intersection_2d(p1, p2, sp1, sp2)
            if inter is None:
                continue
            crossed_any = True
            ix, iy, _t_plan, _u_sec = inter

            # x_cut_expected = projection de l'intersection dans le
            # repère DXF coupe, selon l'orientation du trait.
            trait_dx = sp2[0] - sp1[0]
            trait_dy = sp2[1] - sp1[1]
            x_cut_expected = iy if abs(trait_dx) < abs(trait_dy) else ix

            coupe_path = sl.get("coupe_path", "")
            sec_walls = section_walls_by_coupe.get(coupe_path, [])
            candidates = [
                (idx, sw) for idx, sw in enumerate(sec_walls)
                if abs(float(sw["x_cut_m"]) - x_cut_expected) <= x_cut_tol_m
            ]

            if not candidates:
                matches.append(WallSectionMatch(
                    plan_wall_index=pi,
                    plan_thickness_m=thickness,
                    section_line_index=si,
                    section_line_name=sl.get("name"),
                    coupe_path=coupe_path,
                    x_cut_expected_m=round(x_cut_expected, 4),
                    section_wall_index=None,
                    section_thickness_m=None,
                    thickness_drift_m=None,
                    status="no_section_wall_at_x",
                ))
                continue

            if len(candidates) > 1:
                # Plusieurs candidats : trier par extension verticale
                # décroissante. Le plus haut est le « mur principal » ;
                # les autres sont typiquement linteaux ou allèges.
                candidates.sort(
                    key=lambda c: (
                        float(c[1].get("y_top_m", 0))
                        - float(c[1].get("y_bottom_m", 0))
                    ),
                    reverse=True,
                )
                primary_idx, primary_sw = candidates[0]
                matched_section_walls[coupe_path].add(primary_idx)
                cand_thickness = float(primary_sw["thickness_m"])
                drift = abs(thickness - cand_thickness)
                matches.append(WallSectionMatch(
                    plan_wall_index=pi,
                    plan_thickness_m=thickness,
                    section_line_index=si,
                    section_line_name=sl.get("name"),
                    coupe_path=coupe_path,
                    x_cut_expected_m=round(x_cut_expected, 4),
                    section_wall_index=primary_idx,
                    section_thickness_m=cand_thickness,
                    thickness_drift_m=round(drift, 4),
                    status="ambiguous_multiple_candidates",
                    candidate_indices=[idx for idx, _ in candidates],
                ))
                continue

            # Exactly 1 candidate.
            cand_idx, cand_sw = candidates[0]
            matched_section_walls[coupe_path].add(cand_idx)
            cand_thickness = float(cand_sw["thickness_m"])
            drift = abs(thickness - cand_thickness)
            status = "ok" if drift <= thickness_tol_m else "thickness_mismatch"
            matches.append(WallSectionMatch(
                plan_wall_index=pi,
                plan_thickness_m=thickness,
                section_line_index=si,
                section_line_name=sl.get("name"),
                coupe_path=coupe_path,
                x_cut_expected_m=round(x_cut_expected, 4),
                section_wall_index=cand_idx,
                section_thickness_m=cand_thickness,
                thickness_drift_m=round(drift, 4),
                status=status,
            ))

        if not crossed_any:
            walls_plan_not_crossed.append(pi)

    # Section walls non matchés (potentiellement suspects).
    section_walls_unmatched: List[Dict[str, Any]] = []
    for coupe_path, sec_walls in section_walls_by_coupe.items():
        matched = matched_section_walls.get(coupe_path, set())
        for idx, sw in enumerate(sec_walls):
            if idx in matched:
                continue
            section_walls_unmatched.append({
                "coupe_path": coupe_path,
                "section_wall_index": idx,
                "x_cut_m": float(sw["x_cut_m"]),
                "thickness_m": float(sw["thickness_m"]),
                "y_bottom_m": float(sw.get("y_bottom_m", 0.0)),
                "y_top_m": float(sw.get("y_top_m", 0.0)),
            })

    summary = {
        "matches_ok": sum(1 for m in matches if m.status == "ok"),
        "thickness_mismatches": sum(
            1 for m in matches if m.status == "thickness_mismatch"
        ),
        "no_section_wall_at_x": sum(
            1 for m in matches if m.status == "no_section_wall_at_x"
        ),
        "ambiguous": sum(
            1 for m in matches if m.status == "ambiguous_multiple_candidates"
        ),
        "plan_walls_total": len(plan_walls),
        "plan_walls_not_crossed": len(walls_plan_not_crossed),
        "section_walls_unmatched": len(section_walls_unmatched),
        "section_lines_count": len(section_lines),
    }

    return WallsReconciliation(
        matches=matches,
        walls_plan_not_crossed=walls_plan_not_crossed,
        section_walls_unmatched=section_walls_unmatched,
        summary=summary,
    )


# ----- Détection convention DXF X axis (P2 mirror fix) ----------------
#
# La convention « DXF X = world axis identité » établie sur P7 ne tient
# pas systématiquement (cf. P2 où certaines coupes longitudinales sont
# miroitées). Source probable : le user a `FlipDirection()` la vue
# section dans Revit (ou changé le sens de dessin du trait), modifiant
# l'orientation du BasisX de la vue source. L'export DXF reflète cette
# orientation, mais le marqueur de coupe en plan ne le révèle pas
# directement.
#
# **Stratégie auto-détection** : tester les 2 conventions sur les murs
# du plan croisés par le trait. Celle qui matche le plus de murs gagne.
# Si égalité ou aucun match → "identity" par défaut (= convention P7).


@dataclass
class SectionXAxisConvention:
    """Verdict de détection de convention X axis pour une coupe.

    - `convention` : `"identity"` (DXF X = +world axis, défaut P7) ou
      `"reversed"` (DXF X = -world axis, cas P2 longitudinal).
    - `matches_identity` / `matches_reversed` : nb de murs plan
      retrouvés en coupe selon chaque convention.
    - `confidence` : `(winner - loser) / max(walls_crossed, 1)`. ≥ 0.3
      = signal clair ; entre 0 et 0.3 = à confirmer ; 0 = égalité ou
      aucun mur croisé.
    """
    convention: str
    matches_identity: int
    matches_reversed: int
    walls_crossed: int
    confidence: float


def detect_section_x_axis_convention(
    plan_walls: List[Dict[str, Any]],
    section_line: Dict[str, Any],
    section_walls_in_coupe: List[Dict[str, Any]],
    *,
    thickness_tol_m: float = 0.02,
    x_cut_tol_m: float = 0.10,
) -> SectionXAxisConvention:
    """Détecte si la coupe utilise convention `identity` ou `reversed`.

    Pour chaque mur plan croisant `section_line` :
    - `x_cut_identity = ix` (trait horizontal) ou `iy` (trait vertical).
    - `x_cut_reversed = -x_cut_identity`.
    - Cherche un section_wall dans `section_walls_in_coupe` à
      `x_cut_identity` (± tol, épaisseur compatible) → +1 identity.
    - Cherche à `x_cut_reversed` → +1 reversed.

    Convention retenue = celle avec le plus de matches.

    Args:
        plan_walls: liste `[{p1, p2, thickness_m}, ...]`.
        section_line: dict `{plan_p1, plan_p2, ...}`.
        section_walls_in_coupe: liste `[{x_cut_m, thickness_m, ...}, ...]`.
        thickness_tol_m / x_cut_tol_m: tolérances de matching.

    Returns:
        `SectionXAxisConvention`.
    """
    sp1 = (float(section_line["plan_p1"][0]), float(section_line["plan_p1"][1]))
    sp2 = (float(section_line["plan_p2"][0]), float(section_line["plan_p2"][1]))
    trait_dx = sp2[0] - sp1[0]
    trait_dy = sp2[1] - sp1[1]
    is_vertical_trait = abs(trait_dx) < abs(trait_dy)

    matches_identity = 0
    matches_reversed = 0
    walls_crossed = 0
    for pw in plan_walls:
        p1 = (float(pw["p1"][0]), float(pw["p1"][1]))
        p2 = (float(pw["p2"][0]), float(pw["p2"][1]))
        thickness = float(pw.get("thickness_m", pw.get("thickness", 0.0)))
        inter = _segment_intersection_2d(p1, p2, sp1, sp2)
        if inter is None:
            continue
        ix, iy, _t, _u = inter
        x_cut_identity = iy if is_vertical_trait else ix
        x_cut_reversed = -x_cut_identity
        walls_crossed += 1
        for sw in section_walls_in_coupe:
            if abs(float(sw["thickness_m"]) - thickness) > thickness_tol_m:
                continue
            x_dxf = float(sw["x_cut_m"])
            if abs(x_dxf - x_cut_identity) <= x_cut_tol_m:
                matches_identity += 1
                break  # 1 match par mur suffit
        for sw in section_walls_in_coupe:
            if abs(float(sw["thickness_m"]) - thickness) > thickness_tol_m:
                continue
            x_dxf = float(sw["x_cut_m"])
            if abs(x_dxf - x_cut_reversed) <= x_cut_tol_m:
                matches_reversed += 1
                break

    if walls_crossed == 0:
        return SectionXAxisConvention(
            convention="identity", matches_identity=0, matches_reversed=0,
            walls_crossed=0, confidence=0.0,
        )

    if matches_reversed > matches_identity:
        winner = "reversed"
        margin = matches_reversed - matches_identity
    else:
        winner = "identity"
        margin = matches_identity - matches_reversed

    confidence = margin / float(walls_crossed)
    return SectionXAxisConvention(
        convention=winner,
        matches_identity=matches_identity,
        matches_reversed=matches_reversed,
        walls_crossed=walls_crossed,
        confidence=round(confidence, 3),
    )


# ----- Reconciliation plan ↔ coupes pour les dalles (Phase 2c) ---------
#
# Symétrique de `reconcile_plan_section_walls` pour les sols. Une dalle
# plan a un contour 2D + niveau Z + épaisseur (via FloorType). Une coupe
# révèle des paires top/bot horizontales A-FLOR (`SectionFloorSlab`).
#
# Pour chaque (Floor, section_line) qui se croisent, on calcule les
# intervalles `[x_cut_min, x_cut_max]` où la coupe est *à l'intérieur*
# de la dalle (en tenant compte des trous), exprimés dans le repère DXF
# coupe via la même convention que pour les murs : `x_cut = iy` si trait
# vertical, sinon `ix`. On cherche ensuite une paire dans cette coupe
# dont `top_y_m ≈ floor.elevation_m` ET dont `[x_min, x_max]` recouvre
# l'intervalle. On compare alors les épaisseurs.


def _polygon_line_intersections(
    polygon: List[Tuple[float, float]],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> List[Tuple[float, float, float]]:
    """Intersections d'un segment `a→b` avec le contour d'un polygone fermé.

    Args:
        polygon: liste de sommets `[(x, y), ...]`. Fermeture implicite
            (le dernier sommet reboucle sur le premier).
        a, b: extrémités du segment.

    Returns:
        Liste de `(x, y, u)` où `u ∈ [0, 1]` est l'abscisse normalisée
        du point d'intersection le long du segment `a→b`. Pas dédoublonnée
        — un segment qui passe pile par un sommet apparaît deux fois.
    """
    out: List[Tuple[float, float, float]] = []
    n = len(polygon)
    if n < 2:
        return out
    for i in range(n):
        p = polygon[i]
        q = polygon[(i + 1) % n]
        inter = _segment_intersection_2d(a, b, p, q)
        if inter is None:
            continue
        ix, iy, t, _u = inter
        out.append((ix, iy, t))
    return out


def _point_in_polygon(
    pt: Tuple[float, float],
    polygon: List[Tuple[float, float]],
) -> bool:
    """Test d'inclusion strict d'un point dans un polygone fermé
    (ray casting horizontal vers +X). Frontière → True (par tolérance
    de l'algorithme classique ; on n'a pas besoin de stricte ouverture
    pour notre cas d'usage)."""
    x, y = pt
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def _floor_cut_intervals_in_section(
    boundary: List[Tuple[float, float]],
    holes: List[List[Tuple[float, float]]],
    section_p1: Tuple[float, float],
    section_p2: Tuple[float, float],
    *,
    cut_dedupe_tol_m: float = 1e-4,
) -> List[Tuple[float, float]]:
    """Pour une dalle (boundary + holes) traversée par un trait de coupe
    `section_p1→section_p2`, retourne les intervalles `[x_cut_min,
    x_cut_max]` dans le repère DXF coupe où le trait est à l'intérieur
    de la dalle (donc où on attend une paire top/bot dans la coupe).

    Algo :
    1. Calculer toutes les intersections du trait avec outer + chaque hole.
    2. Trier par `t` (abscisse normalisée sur le trait), en dédoublonnant
       les `t` quasi-égaux (pile sur un sommet → 2 hits à fusionner en 1).
    3. Évaluer un test d'inclusion en milieu de chaque intervalle entre
       hits successifs : si le milieu est *dans la dalle nette*
       (outer ∧ ¬hole_i), c'est un intervalle "à l'intérieur".
    4. Projeter `(x, y)` du couple `(t_a, t_b)` selon l'orientation du
       trait (vertical → y, horizontal → x) pour obtenir `[x_cut_min,
       x_cut_max]` en coordonnées coupe.

    Returns:
        Liste d'intervalles `(x_cut_min, x_cut_max)` triés et non-vides.
    """
    # 1. Hits sur outer + holes.
    hits: List[Tuple[float, float, float]] = []
    hits.extend(_polygon_line_intersections(boundary, section_p1, section_p2))
    for hole in holes:
        hits.extend(_polygon_line_intersections(hole, section_p1, section_p2))

    # Compléter par les extrémités du trait pour traiter le cas où une
    # extrémité tombe à l'intérieur de la dalle (sinon on rate l'intervalle).
    hits.append((section_p1[0], section_p1[1], 0.0))
    hits.append((section_p2[0], section_p2[1], 1.0))

    hits.sort(key=lambda h: h[2])
    # Dédoublonner les t quasi-égaux.
    deduped: List[Tuple[float, float, float]] = []
    for h in hits:
        if deduped and abs(h[2] - deduped[-1][2]) < 1e-6:
            continue
        deduped.append(h)

    if len(deduped) < 2:
        return []

    trait_dx = section_p2[0] - section_p1[0]
    trait_dy = section_p2[1] - section_p1[1]
    is_vertical = abs(trait_dx) < abs(trait_dy)

    def _to_cut_x(x: float, y: float) -> float:
        return y if is_vertical else x

    intervals: List[Tuple[float, float]] = []
    for i in range(len(deduped) - 1):
        x_a, y_a, _t_a = deduped[i]
        x_b, y_b, _t_b = deduped[i + 1]
        mid = ((x_a + x_b) / 2.0, (y_a + y_b) / 2.0)
        # Inside = inside outer AND not inside any hole.
        if not _point_in_polygon(mid, boundary):
            continue
        if any(_point_in_polygon(mid, h) for h in holes):
            continue
        cx_a = _to_cut_x(x_a, y_a)
        cx_b = _to_cut_x(x_b, y_b)
        x_min, x_max = (cx_a, cx_b) if cx_a <= cx_b else (cx_b, cx_a)
        if x_max - x_min < cut_dedupe_tol_m:
            continue
        intervals.append((round(x_min, 4), round(x_max, 4)))

    # Fusion d'intervalles adjacents (trait colinéaire à un bord, etc.).
    if not intervals:
        return []
    intervals.sort()
    merged: List[Tuple[float, float]] = [intervals[0]]
    for x_min, x_max in intervals[1:]:
        last_min, last_max = merged[-1]
        if x_min <= last_max + cut_dedupe_tol_m:
            merged[-1] = (last_min, max(last_max, x_max))
        else:
            merged.append((x_min, x_max))
    return merged


@dataclass
class FloorSectionMatch:
    """Une dalle plan croisée par un trait de coupe, avec son pendant
    éventuel dans le DXF coupe.

    `status` :
    - `"ok"` : Z + épaisseur + extent matchent.
    - `"thickness_mismatch"` : paire trouvée au bon Z + extent OK, mais
      drift d'épaisseur > tolérance.
    - `"extent_partial"` : paire trouvée au bon Z + épaisseur OK, mais
      ne recouvre pas tout l'intervalle de coupe (signal : trémie non-
      détectée en plan OU dalle plan trop large).
    - `"no_section_pair_at_z"` : aucune paire dans la coupe à l'élévation
      du niveau de cette dalle (signal fort : dalle plan « fantôme »
      ou paire mal détectée côté coupe).
    - `"no_section_pair_at_x"` : paire(s) trouvée(s) au bon Z mais
      aucune n'a d'overlap X avec l'intervalle de coupe.
    """
    plan_floor_index: int
    plan_level_elevation_m: float
    plan_thickness_m: float
    section_line_index: int
    section_line_name: Optional[str]
    coupe_path: str
    cut_interval_m: Tuple[float, float]
    section_slab_index: Optional[int]
    section_top_y_m: Optional[float]
    section_thickness_m: Optional[float]
    thickness_drift_m: Optional[float]
    coverage_ratio: Optional[float]
    status: str


@dataclass
class FloorsReconciliation:
    """Rapport de recoupement plan ↔ coupes pour les dalles."""
    matches: List[FloorSectionMatch]
    floors_plan_not_crossed: List[int]
    section_slabs_unmatched: List[Dict[str, Any]]
    summary: Dict[str, int]


def reconcile_plan_section_floors(
    plan_floors: List[Dict[str, Any]],
    section_lines: List[Dict[str, Any]],
    section_slabs_by_coupe: Dict[str, List[Dict[str, Any]]],
    *,
    z_tol_m: float = 0.05,
    thickness_tol_m: float = 0.02,
    coverage_min_ratio: float = 0.80,
) -> FloorsReconciliation:
    """Croise les dalles du plan avec celles observées dans chaque coupe.

    Pour chaque dalle plan, on cherche les intersections avec chaque
    trait de coupe (segment 2D vs polygone). Pour chaque intervalle
    `[x_cut_min, x_cut_max]` où le trait est à l'intérieur de la dalle,
    on cherche une `SectionFloorSlab` dans cette coupe telle que
    `top_y_m ≈ floor.elevation_m` et `[x_min_m, x_max_m]` recouvre
    suffisamment l'intervalle. On compare alors les épaisseurs.

    Args:
        plan_floors: liste de dicts `{boundary: [[x,y]], holes: [[[x,y]]],
            elevation_m: float, thickness_m: float, …}`. Le `level_ref`
            n'est pas requis ici (résolu côté caller).
        section_lines: liste de dicts `{plan_p1, plan_p2, view_dir,
            coupe_path, name?}`. Identique au format murs.
        section_slabs_by_coupe: `{coupe_path: [{top_y_m, bot_y_m,
            thickness_m, x_min_m, x_max_m}, …]}`. Sérialisation de
            `read_section_floor_slabs`.
        z_tol_m: tolérance Z pour match niveau (défaut 5 cm — empirique,
            les exports Revit posent le top de dalle pile à l'élévation
            du niveau, drift < 1 mm normalement).
        thickness_tol_m: tolérance d'épaisseur (défaut 2 cm).
        coverage_min_ratio: fraction min de l'intervalle de coupe qui
            doit être recouverte par la paire (défaut 0.80). Si < ratio :
            statut `extent_partial`.

    Returns:
        `FloorsReconciliation` avec :
        - `matches` : un `FloorSectionMatch` par intervalle de coupe.
        - `floors_plan_not_crossed` : indices des dalles plan qui ne
          croisent aucun trait.
        - `section_slabs_unmatched` : paires coupe sans pendant au plan
          (suspect — dalle oubliée ou détection plan incomplète).
        - `summary` : compteurs agrégés par statut.
    """
    matches: List[FloorSectionMatch] = []
    floors_plan_not_crossed: List[int] = []
    matched_slabs: Dict[str, set] = {p: set() for p in section_slabs_by_coupe}

    for fi, fl in enumerate(plan_floors):
        boundary_raw = fl.get("boundary") or []
        boundary: List[Tuple[float, float]] = [
            (float(p[0]), float(p[1])) for p in boundary_raw
        ]
        holes_raw = fl.get("holes") or []
        holes: List[List[Tuple[float, float]]] = [
            [(float(p[0]), float(p[1])) for p in h] for h in holes_raw
        ]
        elevation = float(fl.get("elevation_m", 0.0))
        thickness = float(fl.get("thickness_m", 0.0))

        if len(boundary) < 3:
            floors_plan_not_crossed.append(fi)
            continue

        crossed_any = False
        for si, sl in enumerate(section_lines):
            sp1 = (float(sl["plan_p1"][0]), float(sl["plan_p1"][1]))
            sp2 = (float(sl["plan_p2"][0]), float(sl["plan_p2"][1]))
            intervals = _floor_cut_intervals_in_section(
                boundary, holes, sp1, sp2,
            )
            if not intervals:
                continue
            crossed_any = True
            coupe_path = sl.get("coupe_path", "")
            slabs = section_slabs_by_coupe.get(coupe_path, [])

            # Sous-ensemble des paires au bon niveau Z.
            slabs_at_z: List[Tuple[int, Dict[str, Any]]] = [
                (idx, s) for idx, s in enumerate(slabs)
                if abs(float(s["top_y_m"]) - elevation) <= z_tol_m
            ]

            for (x_min, x_max) in intervals:
                interval_len = x_max - x_min
                if not slabs_at_z:
                    matches.append(FloorSectionMatch(
                        plan_floor_index=fi,
                        plan_level_elevation_m=round(elevation, 4),
                        plan_thickness_m=round(thickness, 4),
                        section_line_index=si,
                        section_line_name=sl.get("name"),
                        coupe_path=coupe_path,
                        cut_interval_m=(x_min, x_max),
                        section_slab_index=None,
                        section_top_y_m=None,
                        section_thickness_m=None,
                        thickness_drift_m=None,
                        coverage_ratio=None,
                        status="no_section_pair_at_z",
                    ))
                    continue

                # Best candidate : meilleur overlap X.
                best_idx: Optional[int] = None
                best_slab: Optional[Dict[str, Any]] = None
                best_overlap: float = 0.0
                for idx, s in slabs_at_z:
                    ox = max(x_min, float(s["x_min_m"]))
                    oX = min(x_max, float(s["x_max_m"]))
                    overlap = oX - ox
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_idx = idx
                        best_slab = s

                if best_slab is None or best_overlap <= 0:
                    matches.append(FloorSectionMatch(
                        plan_floor_index=fi,
                        plan_level_elevation_m=round(elevation, 4),
                        plan_thickness_m=round(thickness, 4),
                        section_line_index=si,
                        section_line_name=sl.get("name"),
                        coupe_path=coupe_path,
                        cut_interval_m=(x_min, x_max),
                        section_slab_index=None,
                        section_top_y_m=None,
                        section_thickness_m=None,
                        thickness_drift_m=None,
                        coverage_ratio=0.0,
                        status="no_section_pair_at_x",
                    ))
                    continue

                cov_ratio = (
                    best_overlap / interval_len if interval_len > 0 else 1.0
                )
                cand_thk = float(best_slab["thickness_m"])
                drift = abs(thickness - cand_thk)
                matched_slabs[coupe_path].add(best_idx)  # type: ignore[arg-type]

                if drift > thickness_tol_m:
                    status = "thickness_mismatch"
                elif cov_ratio < coverage_min_ratio:
                    status = "extent_partial"
                else:
                    status = "ok"

                matches.append(FloorSectionMatch(
                    plan_floor_index=fi,
                    plan_level_elevation_m=round(elevation, 4),
                    plan_thickness_m=round(thickness, 4),
                    section_line_index=si,
                    section_line_name=sl.get("name"),
                    coupe_path=coupe_path,
                    cut_interval_m=(x_min, x_max),
                    section_slab_index=best_idx,
                    section_top_y_m=round(float(best_slab["top_y_m"]), 4),
                    section_thickness_m=round(cand_thk, 4),
                    thickness_drift_m=round(drift, 4),
                    coverage_ratio=round(cov_ratio, 3),
                    status=status,
                ))

        if not crossed_any:
            floors_plan_not_crossed.append(fi)

    # Paires coupe non-matchées : potentiellement dalles oubliées en plan.
    section_slabs_unmatched: List[Dict[str, Any]] = []
    for coupe_path, slabs in section_slabs_by_coupe.items():
        matched = matched_slabs.get(coupe_path, set())
        for idx, s in enumerate(slabs):
            if idx in matched:
                continue
            section_slabs_unmatched.append({
                "coupe_path": coupe_path,
                "section_slab_index": idx,
                "top_y_m": round(float(s["top_y_m"]), 4),
                "bot_y_m": round(float(s["bot_y_m"]), 4),
                "thickness_m": round(float(s["thickness_m"]), 4),
                "x_min_m": round(float(s["x_min_m"]), 4),
                "x_max_m": round(float(s["x_max_m"]), 4),
            })

    summary = {
        "matches_ok": sum(1 for m in matches if m.status == "ok"),
        "thickness_mismatches": sum(
            1 for m in matches if m.status == "thickness_mismatch"
        ),
        "extent_partial": sum(
            1 for m in matches if m.status == "extent_partial"
        ),
        "no_section_pair_at_z": sum(
            1 for m in matches if m.status == "no_section_pair_at_z"
        ),
        "no_section_pair_at_x": sum(
            1 for m in matches if m.status == "no_section_pair_at_x"
        ),
        "plan_floors_total": len(plan_floors),
        "plan_floors_not_crossed": len(floors_plan_not_crossed),
        "section_slabs_unmatched": len(section_slabs_unmatched),
        "section_lines_count": len(section_lines),
    }

    return FloorsReconciliation(
        matches=matches,
        floors_plan_not_crossed=floors_plan_not_crossed,
        section_slabs_unmatched=section_slabs_unmatched,
        summary=summary,
    )


# ----- Audit d'intégrité du plan set (gate avant écriture modèle) -------
#
# Le user veut un audit holistique du dossier DXF AVANT toute proposition
# de modif au modèle (typiquement les niveaux). Ce module agrège les
# briques de check existantes en un rapport unique avec gate.
#
# Severity hierarchy : "clean" < "warnings" < "errors". Gate :
# - "errors" → `gate_status = "abort"`, ok=False côté tool. L'agent doit
#   stopper et présenter à l'user.
# - "warnings" → `gate_status = "needs_user"`. L'agent doit présenter
#   les warnings mais peut continuer si l'user confirme.
# - "clean" → `gate_status = "pass"`. Continuer le flow.


# Limites de sévérité paramétrables, alignées avec dwg_verify_section_scale.
SCALE_DRIFT_WARNING_PCT = 25.0
SCALE_DRIFT_ERROR_PCT = 50.0


@dataclass
class IntegrityCheck:
    """Un check unitaire d'audit. `severity` ∈ {clean, warnings, errors}."""
    name: str
    severity: str
    summary: Dict[str, Any]
    issues: List[Dict[str, Any]] = field(default_factory=list)


def _severity_max(severities: List[str]) -> str:
    """Aggregate severities : `errors` > `warnings` > `clean`."""
    order = {"clean": 0, "warnings": 1, "errors": 2}
    inverse = {v: k for k, v in order.items()}
    if not severities:
        return "clean"
    return inverse[max(order[s] for s in severities)]


def check_source_consistency(
    source_per_file: Dict[str, str],
) -> IntegrityCheck:
    """Vérifie que tous les fichiers DXF utilisent la même convention.

    `source_per_file` : dict `{path: source}` où source ∈ {"aia", "iso",
    "other"}. Sortie :
    - severity `clean` si tous égaux.
    - severity `warnings` si tous == "other" (convention non reconnue
      mais cohérente).
    - severity `errors` si mix AIA + autre (les mappings de layers
      vont diverger entre fichiers).
    """
    if not source_per_file:
        return IntegrityCheck(
            name="source_consistency",
            severity="clean",
            summary={"distinct_sources": [], "files_count": 0},
        )
    distinct = sorted(set(source_per_file.values()))
    files_by_source: Dict[str, List[str]] = {}
    for fp, src in source_per_file.items():
        files_by_source.setdefault(src, []).append(fp)

    if len(distinct) == 1:
        if distinct[0] == "other":
            severity = "warnings"
            issues = [{
                "kind": "all_other",
                "message": (
                    "Tous les fichiers ont une source 'other' (convention "
                    "de layers non reconnue). Les mappings AIA-default "
                    "(A-WALL, A-FLOR-LEVL, etc.) pourraient ne pas "
                    "fonctionner."
                ),
            }]
        else:
            severity = "clean"
            issues = []
    else:
        severity = "errors"
        issues = [{
            "kind": "mixed_sources",
            "sources_observed": distinct,
            "files_by_source": files_by_source,
            "message": (
                "Sources de layers mixtes dans le dossier : {}. Les "
                "fichiers doivent avoir été exportés avec la même "
                "convention pour que les mappings A-WALL / A-FLOR-LEVL "
                "marchent uniformément.".format(distinct)
            ),
        }]

    return IntegrityCheck(
        name="source_consistency",
        severity=severity,
        summary={
            "distinct_sources": distinct,
            "files_count": len(source_per_file),
            "files_by_source": {
                src: len(paths) for src, paths in files_by_source.items()
            },
        },
        issues=issues,
    )


def check_levels_consistency_between_coupes(
    levels_by_coupe: Dict[str, List[Dict[str, Any]]],
    *,
    elevation_tol_m: float = 0.01,
) -> IntegrityCheck:
    """Vérifie que les coupes déclarent un jeu de niveaux cohérent.

    `levels_by_coupe` : `{coupe_path: [{name, elevation_m}, ...]}`.
    Pour chaque paire de coupes, on compare leurs niveaux : un niveau
    présent dans Coupe A à élévation X doit aussi être dans Coupe B à
    élévation X ± `elevation_tol_m`.

    Severity :
    - `clean` si tous les coupes déclarent le même jeu (mêmes
      élévations à tol près).
    - `warnings` si subset/superset (une coupe ne traverse pas tous
      les niveaux — fréquent et normal).
    - `errors` si conflit d'élévation pour le même niveau nommé.
    """
    if len(levels_by_coupe) <= 1:
        return IntegrityCheck(
            name="levels_consistency",
            severity="clean",
            summary={"coupes_count": len(levels_by_coupe)},
        )

    # Construit un index global { name : { coupe_path : elevation_m } }.
    levels_by_name: Dict[str, Dict[str, float]] = {}
    all_elevations: Dict[str, List[Tuple[str, float]]] = {}
    for coupe_path, levels in levels_by_coupe.items():
        for lv in levels:
            name = lv.get("name", "<unnamed>")
            elev = float(lv.get("elevation_m", lv.get("elevation", 0.0)))
            levels_by_name.setdefault(name, {})[coupe_path] = elev
            all_elevations.setdefault(name, []).append((coupe_path, elev))

    issues: List[Dict[str, Any]] = []
    severities: List[str] = ["clean"]

    # Conflits d'élévation pour un même nom.
    for name, elevs in all_elevations.items():
        if len(elevs) <= 1:
            continue
        elev_values = [e for _, e in elevs]
        if max(elev_values) - min(elev_values) > elevation_tol_m:
            issues.append({
                "kind": "elevation_conflict_same_name",
                "level_name": name,
                "elevations_by_coupe": dict(elevs),
                "delta_m": round(max(elev_values) - min(elev_values), 4),
                "message": (
                    "Niveau '{}' a des élévations différentes selon les "
                    "coupes (Δ={:.3f}m). Les coupes ne sont pas "
                    "auto-cohérentes — possible erreur d'export.".format(
                        name, max(elev_values) - min(elev_values),
                    )
                ),
            })
            severities.append("errors")

    # Subset/superset : un niveau présent dans certaines coupes mais
    # pas dans d'autres → warning (normal si la coupe ne traverse pas
    # ce niveau).
    coupe_paths = list(levels_by_coupe.keys())
    for name, by_coupe in levels_by_name.items():
        missing = [cp for cp in coupe_paths if cp not in by_coupe]
        if missing and len(missing) < len(coupe_paths):
            issues.append({
                "kind": "level_missing_in_some_coupes",
                "level_name": name,
                "present_in": list(by_coupe.keys()),
                "missing_in": missing,
                "message": (
                    "Niveau '{}' présent dans {} coupe(s) mais absent "
                    "dans {} autre(s). Normal si la coupe ne traverse "
                    "pas ce niveau.".format(
                        name, len(by_coupe), len(missing),
                    )
                ),
            })
            severities.append("warnings")

    severity = _severity_max(severities)

    return IntegrityCheck(
        name="levels_consistency",
        severity=severity,
        summary={
            "coupes_count": len(levels_by_coupe),
            "distinct_level_names": len(levels_by_name),
            "elevation_conflicts": sum(
                1 for i in issues
                if i["kind"] == "elevation_conflict_same_name"
            ),
            "subset_warnings": sum(
                1 for i in issues
                if i["kind"] == "level_missing_in_some_coupes"
            ),
        },
        issues=issues,
    )


def check_openings_matching(
    openings_match_reports: List[Dict[str, Any]],
) -> IntegrityCheck:
    """Évalue le matching ouvertures plan ↔ coupes.

    `openings_match_reports` : liste de dicts comme retournés par
    `dwg_inspect_sections.section_to_plan_matches`, contenant
    `match_count`, `unmatched_section_count`, `unmatched_plan_count`.

    Severity :
    - `clean` si tous les openings sont matchés des deux côtés.
    - `warnings` si quelques openings unmatched (export partiel,
      cliché, etc.). Non bloquant.
    - Pas de severity `errors` ici — un opening unmatched est rarement
      critique (l'user peut compléter post-import).
    """
    if not openings_match_reports:
        return IntegrityCheck(
            name="openings_matching",
            severity="clean",
            summary={"coupes_checked": 0},
        )

    total_matches = sum(r.get("match_count", 0) for r in openings_match_reports)
    total_unmatched_section = sum(
        r.get("unmatched_section_count", 0) for r in openings_match_reports
    )
    total_unmatched_plan = sum(
        r.get("unmatched_plan_count", 0) for r in openings_match_reports
    )

    issues: List[Dict[str, Any]] = []
    if total_unmatched_section > 0 or total_unmatched_plan > 0:
        for r in openings_match_reports:
            unmatched_sec = r.get("unmatched_section_count", 0)
            unmatched_plan = r.get("unmatched_plan_count", 0)
            if unmatched_sec == 0 and unmatched_plan == 0:
                continue
            issues.append({
                "kind": "unmatched_openings",
                "section_name": r.get("section_name"),
                "section_path": r.get("section_path"),
                "match_count": r.get("match_count", 0),
                "unmatched_section_count": unmatched_sec,
                "unmatched_plan_count": unmatched_plan,
                "message": (
                    "{}: {} match(es), {} opening(s) coupe sans plan, "
                    "{} opening(s) plan sans coupe.".format(
                        r.get("section_name"), r.get("match_count", 0),
                        unmatched_sec, unmatched_plan,
                    )
                ),
            })

    severity = "warnings" if issues else "clean"
    return IntegrityCheck(
        name="openings_matching",
        severity=severity,
        summary={
            "coupes_checked": len(openings_match_reports),
            "total_matches": total_matches,
            "total_unmatched_section": total_unmatched_section,
            "total_unmatched_plan": total_unmatched_plan,
        },
        issues=issues,
    )


def check_scale_drift(
    scale_per_coupe: List[Dict[str, Any]],
) -> IntegrityCheck:
    """Évalue le drift d'échelle plan ↔ coupes.

    `scale_per_coupe` : liste de dicts `{coupe_path, marker_length_m,
    coupe_extent_m, drift_pct}`. Output de `dxf_assign_coupes_to_traits`
    enrichi, ou calcul équivalent.

    Severity :
    - `errors` si une coupe a drift > 50%.
    - `warnings` si drift > 25%.
    - `clean` sinon.
    """
    if not scale_per_coupe:
        return IntegrityCheck(
            name="scale_drift",
            severity="clean",
            summary={"coupes_checked": 0},
        )

    issues: List[Dict[str, Any]] = []
    severities: List[str] = ["clean"]
    for s in scale_per_coupe:
        drift_pct = float(s.get("drift_pct", 0.0))
        if drift_pct >= SCALE_DRIFT_ERROR_PCT:
            severities.append("errors")
            issues.append({
                "kind": "drift_error",
                "coupe_path": s.get("coupe_path"),
                "drift_pct": drift_pct,
                "marker_length_m": s.get("marker_length_m"),
                "coupe_extent_m": s.get("coupe_extent_m"),
                "message": (
                    "Coupe '{}': drift {:.1f}% entre trait ({:.2f}m) et "
                    "A-WALL extent ({:.2f}m). Possible mauvais assignment "
                    "trait↔coupe ou échelle incohérente.".format(
                        s.get("coupe_path"), drift_pct,
                        float(s.get("marker_length_m", 0)),
                        float(s.get("coupe_extent_m", 0)),
                    )
                ),
            })
        elif drift_pct >= SCALE_DRIFT_WARNING_PCT:
            severities.append("warnings")
            issues.append({
                "kind": "drift_warning",
                "coupe_path": s.get("coupe_path"),
                "drift_pct": drift_pct,
                "marker_length_m": s.get("marker_length_m"),
                "coupe_extent_m": s.get("coupe_extent_m"),
                "message": (
                    "Coupe '{}': drift {:.1f}% notable (la coupe inclut "
                    "souvent du contexte hors-bâtiment).".format(
                        s.get("coupe_path"), drift_pct,
                    )
                ),
            })

    return IntegrityCheck(
        name="scale_drift",
        severity=_severity_max(severities),
        summary={
            "coupes_checked": len(scale_per_coupe),
            "errors_count": sum(1 for i in issues if i["kind"] == "drift_error"),
            "warnings_count": sum(1 for i in issues if i["kind"] == "drift_warning"),
        },
        issues=issues,
    )


def walls_reconciliation_to_check(
    report: WallsReconciliation,
    *,
    thickness_tol_m: float = 0.02,
) -> IntegrityCheck:
    """Convertit un `WallsReconciliation` en `IntegrityCheck` pour
    intégration dans l'audit agrégé.

    Severity :
    - `errors` si thickness_mismatch dépassant 0.10m (10cm) — quasi-
      certainement un fichier corrompu ou mauvais matching trait↔coupe.
    - `warnings` si thickness_mismatch < 10cm ou ambiguous ou
      no_section_wall_at_x ou section_walls_unmatched > 0.
    - `clean` sinon.

    Note : on est plus strict que la tolérance de match (`thickness_tol_m`).
    Un drift de 3-5cm est un warning ; un drift > 10cm est probablement
    une erreur de modélisation ou de matching.
    """
    issues: List[Dict[str, Any]] = []
    severities: List[str] = ["clean"]
    error_threshold_m = 0.10

    for m in report.matches:
        if m.status == "thickness_mismatch":
            drift = m.thickness_drift_m or 0.0
            if drift >= error_threshold_m:
                severities.append("errors")
                kind = "thickness_mismatch_severe"
            else:
                severities.append("warnings")
                kind = "thickness_mismatch_mild"
            issues.append({
                "kind": kind,
                "plan_wall_index": m.plan_wall_index,
                "plan_thickness_m": m.plan_thickness_m,
                "section_thickness_m": m.section_thickness_m,
                "thickness_drift_m": drift,
                "coupe_path": m.coupe_path,
                "section_line_name": m.section_line_name,
                "x_cut_expected_m": m.x_cut_expected_m,
            })
        elif m.status == "ambiguous_multiple_candidates":
            severities.append("warnings")
            issues.append({
                "kind": "ambiguous_candidates",
                "plan_wall_index": m.plan_wall_index,
                "coupe_path": m.coupe_path,
                "candidate_indices": m.candidate_indices,
                "primary_thickness_m": m.section_thickness_m,
            })
        elif m.status == "no_section_wall_at_x":
            severities.append("warnings")
            issues.append({
                "kind": "no_section_wall_at_x",
                "plan_wall_index": m.plan_wall_index,
                "coupe_path": m.coupe_path,
                "x_cut_expected_m": m.x_cut_expected_m,
            })

    if report.section_walls_unmatched:
        severities.append("warnings")
        issues.append({
            "kind": "section_walls_unmatched",
            "count": len(report.section_walls_unmatched),
            "samples": report.section_walls_unmatched[:5],
        })

    return IntegrityCheck(
        name="walls_reconciliation",
        severity=_severity_max(severities),
        summary=report.summary,
        issues=issues,
    )


@dataclass
class PlansetIntegrityReport:
    """Rapport agrégé d'audit d'intégrité d'un dossier DXF.

    `gate_status` :
    - `"pass"` : tout est clean. L'agent peut enchaîner sans réserve.
    - `"needs_user"` : warnings non bloquants. L'agent doit présenter
      à l'user mais peut continuer après confirmation.
    - `"abort"` : errors. L'agent doit stopper et présenter à l'user
      pour résolution (export DXF à corriger, etc.).

    `ok` est False quand `gate_status == "abort"`.
    """
    severity: str
    gate_status: str
    ok: bool
    checks: Dict[str, IntegrityCheck]
    errors: List[Dict[str, Any]]
    warnings: List[Dict[str, Any]]
    files_summary: Dict[str, Any]


def check_openings_plan_vs_elevation(
    plan_block_ids: List[str],
    elevation_block_ids: List[str],
    plan_total_inserts: int,
    elevation_total_inserts: int,
) -> IntegrityCheck:
    """Cross-validation comptage + présence par block_id entre plans et
    élévations (user 2026-05-13).

    Une fenêtre représentée en plan doit aussi apparaître dans au moins
    une élévation cardinale, et inversement. Le matching positionnel
    précis est fait en Phase 2b (après création des murs : on a besoin
    de l'orientation pour savoir quelle élévation est attendue). Phase 1
    se limite à un check **non positionnel** : présence du `block_id`
    et écart de comptage.

    Args:
        plan_block_ids: liste des block_id observés dans **tous** les
            plans (avec multiplicité — chaque INSERT compte).
        elevation_block_ids: idem pour les élévations.
        plan_total_inserts: nb total d'INSERTs A-GLAZ dans les plans.
        elevation_total_inserts: nb total d'INSERTs A-GLAZ dans les
            élévations (toutes directions sommées).

    Severity :
    - `clean` si chaque bid plan est présent en élévation et écart de
      comptage < 20%.
    - `warnings` sinon (non bloquant — l'user décide).
    """
    plan_set = set(b for b in plan_block_ids if b)
    elev_set = set(b for b in elevation_block_ids if b)
    missing_in_elev = sorted(plan_set - elev_set)
    missing_in_plan = sorted(elev_set - plan_set)

    # Écart de comptage : peut diverger légitimement (1 fenêtre EW visible
    # dans Nord ET Sud → 2 inserts élév pour 1 plan-opening). On s'attend
    # à ce que elevation_total ≈ plan_total (chaque fenêtre dans 1 élév
    # cardinale, sauf cas particuliers). Seuil 20% retenu.
    count_diff_pct: Optional[float] = None
    if plan_total_inserts > 0:
        count_diff_pct = round(
            abs(elevation_total_inserts - plan_total_inserts)
            / plan_total_inserts * 100.0,
            1,
        )

    issues: List[Dict[str, Any]] = []
    if missing_in_elev:
        issues.append({
            "kind": "block_ids_missing_in_elevation",
            "block_ids": missing_in_elev,
            "message": (
                "{} block_id(s) présent(s) en plan mais absent(s) des "
                "élévations : {}. Fenêtres potentiellement orientées vers "
                "une face non documentée par une élévation.".format(
                    len(missing_in_elev), ", ".join(missing_in_elev)[:200],
                )
            ),
        })
    if missing_in_plan:
        issues.append({
            "kind": "block_ids_missing_in_plan",
            "block_ids": missing_in_plan,
            "message": (
                "{} block_id(s) présent(s) en élévation mais absent(s) "
                "des plans : {}. Soit fenêtres dessinées en élévation "
                "mais oubliées en plan, soit fenêtres d'un niveau "
                "non-exporté.".format(
                    len(missing_in_plan), ", ".join(missing_in_plan)[:200],
                )
            ),
        })
    if count_diff_pct is not None and count_diff_pct >= 20.0:
        issues.append({
            "kind": "opening_count_divergence",
            "plan_total": plan_total_inserts,
            "elevation_total": elevation_total_inserts,
            "diff_pct": count_diff_pct,
            "message": (
                "Plan: {} INSERT(s) A-GLAZ, Élévations cumulées: {} → "
                "écart {}% (seuil warning 20%). Investigation conseillée "
                "avant de créer les fenêtres.".format(
                    plan_total_inserts, elevation_total_inserts,
                    count_diff_pct,
                )
            ),
        })

    severity = "warnings" if issues else "clean"
    return IntegrityCheck(
        name="openings_plan_vs_elevation",
        severity=severity,
        summary={
            "plan_total_inserts": plan_total_inserts,
            "elevation_total_inserts": elevation_total_inserts,
            "count_diff_pct": count_diff_pct,
            "plan_block_ids_unique": len(plan_set),
            "elevation_block_ids_unique": len(elev_set),
            "missing_in_elevation_count": len(missing_in_elev),
            "missing_in_plan_count": len(missing_in_plan),
        },
        issues=issues,
    )


def aggregate_planset_integrity(
    checks: List[IntegrityCheck],
    files_summary: Dict[str, Any],
) -> PlansetIntegrityReport:
    """Combine N checks en un rapport global avec severity max et gate."""
    severity = _severity_max([c.severity for c in checks])
    if severity == "errors":
        gate_status, ok = "abort", False
    elif severity == "warnings":
        gate_status, ok = "needs_user", True
    else:
        gate_status, ok = "pass", True

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    for c in checks:
        for issue in c.issues:
            entry = {"check": c.name, **issue}
            if c.severity == "errors" and issue.get("kind", "").endswith(
                ("error", "_severe", "mixed_sources", "conflict_same_name", "drift_error"),
            ):
                errors.append(entry)
            elif c.severity == "errors":
                # Issue dans un check error mais pas tagué error individuellement
                # — escalade par défaut.
                errors.append(entry)
            else:
                warnings.append(entry)

    return PlansetIntegrityReport(
        severity=severity,
        gate_status=gate_status,
        ok=ok,
        checks={c.name: c for c in checks},
        errors=errors,
        warnings=warnings,
        files_summary=files_summary,
    )
