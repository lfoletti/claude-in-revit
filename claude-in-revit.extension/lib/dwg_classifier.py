"""dwg_classifier.py — heuristique layer → rôle + détection murs par paires de lignes.

§9 V0 Sem.4-5, UC1. Étape 2 du pipeline DWG ingest :

    DwgEntity[]  → classify → WallCandidate[]
                            (+ rejected, + diagnostics)

Deux mécaniques séparées :

1. **`suggest_layer_role(layer_name)`** : regex sur le nom → rôle proposé
   (`"wall"` | `"door"` | `"window"` | `"text"` | `"ignore"` | None).
   Robuste aux conventions FR / EN / nominal AIA (`A-WALL-EXTR`).

2. **`detect_wall_segments(lines, ...)`** : sur une liste de segments
   LINE / LWPOLYLINE-éclatés appartenant à des layers "wall", trouve les
   paires de lignes parallèles distantes de [min_thickness, max_thickness]
   et synthétise un wall (`centerline_p1, centerline_p2, thickness`).
   Les lignes orphelines (sans paire) tombent dans `rejected` avec une
   raison explicite.

**Pas d'import ezdxf ni Revit** — fonctions pures sur la structure
`DwgEntity` de `dwg_reader.py`. Testable hors-Revit, hors-fichier.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .dwg_reader import DwgEntity


# ----- Layer name heuristic ---------------------------------------------
#
# Mapping regex → rôle. Ordre d'évaluation : le premier match gagne, donc
# placer les motifs les plus spécifiques en premier. Casse-insensitive
# systématique.

_LAYER_ROLE_PATTERNS: List[Tuple[str, str]] = [
    # Walls — porteurs et cloisons, FR + EN + AIA standard. La
    # normalisation `_/- → espace` dans `suggest_layer_role` transforme
    # `A-WALL` en `A WALL`, donc l'AIA-style match s'écrit `A ?WALL`.
    (r"\bA ?WALL\b", "wall"),
    (r"\bWALL[S]?\b", "wall"),
    (r"\bMUR[S]?\b", "wall"),
    (r"\bM[0-9]+\b", "wall"),       # M01, M02 (porteur / cloison)
    (r"\bCLOISON[S]?\b", "wall"),
    (r"\bPORTEUR[S]?\b", "wall"),
    # Doors.
    (r"\bA ?DOOR\b", "door"),
    (r"\bDOOR[S]?\b", "door"),
    (r"\bPORTE[S]?\b", "door"),
    (r"\bOUVR\b", "door"),          # OUVRANT
    # Windows.
    (r"\bA ?WIND\b", "window"),
    (r"\bWINDOW[S]?\b", "window"),
    (r"\bFEN[EÉÈÊË]TRE[S]?\b", "window"),
    (r"\bFEN\b", "window"),
    # Texts / annotations / dimensions — utile pour l'OCR-éq mais pas
    # créé en Revit.
    (r"\bTEXT[E]?[S]?\b", "text"),
    (r"\bDIM[S]?\b", "text"),
    (r"\bCOTE[S]?\b", "text"),
    (r"\bANNOT(?:ATION)?[S]?\b", "text"),
    # Catégories à ignorer délibérément.
    (r"\bMOBILIER\b", "ignore"),
    (r"\bFURNITURE\b", "ignore"),
    (r"\bMEUBLE[S]?\b", "ignore"),
    (r"\bHACH(?:URE|URES)?\b", "ignore"),
    (r"\bHATCH(?:ES)?\b", "ignore"),
]

_COMPILED_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(p, re.IGNORECASE), role) for p, role in _LAYER_ROLE_PATTERNS
]


def suggest_layer_role(layer_name: str) -> Optional[str]:
    """Heuristique sur le nom de layer. Renvoie le rôle suggéré ou None.

    Le caller (LLM ou utilisateur) confirme ou corrige avant
    `detect_wall_segments`. Pas de match → None (rôle inconnu, à statuer
    explicitement).

    Normalise underscores et tirets en espaces avant le matching : `\b`
    en Python considère `_` comme un word character, donc sans
    normalisation `\bMURS\b` rate `MURS_PORTEUR`. Convention courante
    des noms de layer CAD (AIA standard `A-WALL-EXTR`, etc.) est de
    traiter `_` et `-` comme séparateurs sémantiques.
    """
    if not layer_name:
        return None
    normalized = layer_name.replace("_", " ").replace("-", " ")
    for pattern, role in _COMPILED_PATTERNS:
        if pattern.search(normalized):
            return role
    return None


def annotate_layers(
    layer_summaries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Ajoute `suggested_role` à chaque entrée d'un `meta["layers"]` (cf
    `dwg_reader.parse`). Mutate-in-place et renvoie la liste pour chaînage.
    """
    for entry in layer_summaries:
        entry["suggested_role"] = suggest_layer_role(entry.get("name", ""))
    return layer_summaries


# ----- Segment extraction from DwgEntity --------------------------------


@dataclass
class Segment:
    """Segment droit 2D (z ignoré) avec son layer d'origine."""
    p1: Tuple[float, float]
    p2: Tuple[float, float]
    layer: str


def extract_straight_segments(
    entities: List[DwgEntity], layer_filter: Optional[List[str]] = None,
) -> List[Segment]:
    """Aplatit LINE / LWPOLYLINE / POLYLINE en segments droits 2D.

    `layer_filter` : si fourni, restreint aux entités dont le layer est
    dans cette liste. Sinon, accepte tous les layers.

    Les arcs / bulges des LWPolylines sont *ignorés* en V0 : seuls les
    sommets sont chaînés en segments droits. Limite documentée — la
    plupart des plans archi orthogonaux n'utilisent pas le bulge.
    """
    layers_set = set(layer_filter) if layer_filter is not None else None
    out: List[Segment] = []
    for e in entities:
        if layers_set is not None and e.layer not in layers_set:
            continue
        if e.kind == "LINE":
            if len(e.coords) >= 2:
                out.append(Segment(
                    p1=(e.coords[0][0], e.coords[0][1]),
                    p2=(e.coords[1][0], e.coords[1][1]),
                    layer=e.layer,
                ))
        elif e.kind in ("LWPOLYLINE", "POLYLINE"):
            pts = e.coords
            closed = bool(e.attrs.get("closed"))
            n = len(pts)
            if n < 2:
                continue
            for i in range(n - 1):
                out.append(Segment(
                    p1=(pts[i][0], pts[i][1]),
                    p2=(pts[i + 1][0], pts[i + 1][1]),
                    layer=e.layer,
                ))
            if closed:
                out.append(Segment(
                    p1=(pts[-1][0], pts[-1][1]),
                    p2=(pts[0][0], pts[0][1]),
                    layer=e.layer,
                ))
    return out


# ----- Parallel-pair wall detection -------------------------------------


@dataclass
class WallCandidate:
    """Mur synthétisé à partir d'une paire de segments parallèles. Output
    primary du classifier — directement consommable par `walls_create_many`.
    """
    p1: Tuple[float, float]           # centerline endpoint A
    p2: Tuple[float, float]           # centerline endpoint B
    thickness: float                  # mètres
    layer: str                        # layer d'origine (paire homogène)
    confidence: float = 1.0           # 0-1, baisse si overlap partiel / etc.
    source: Tuple[int, int] = (-1, -1)  # indices des 2 segments dans la liste


def _segment_length(s: Segment) -> float:
    return math.sqrt((s.p2[0] - s.p1[0]) ** 2 + (s.p2[1] - s.p1[1]) ** 2)


def _segment_angle_normalized(s: Segment) -> float:
    """Angle modulo π (radians dans [0, π)). Deux segments parallèles ont
    le même angle normalisé indépendamment de l'orientation."""
    dx = s.p2[0] - s.p1[0]
    dy = s.p2[1] - s.p1[1]
    a = math.atan2(dy, dx)
    if a < 0:
        a += math.pi
    if a >= math.pi:
        a -= math.pi
    return a


def _angle_close(a: float, b: float, tol: float) -> bool:
    """Distance angulaire modulo π. tol en radians."""
    d = abs(a - b)
    return min(d, math.pi - d) <= tol


def _perp_distance(s_ref: Segment, point: Tuple[float, float]) -> float:
    """Distance perpendiculaire signée d'un point à la droite portant `s_ref`.
    Renvoie la valeur absolue (on n'a pas besoin du signe pour pair detection).
    """
    # Vecteur direction normalisé.
    dx = s_ref.p2[0] - s_ref.p1[0]
    dy = s_ref.p2[1] - s_ref.p1[1]
    norm = math.sqrt(dx * dx + dy * dy)
    if norm < 1e-12:
        return float("inf")
    # Normal (perp).
    nx = -dy / norm
    ny = dx / norm
    return abs((point[0] - s_ref.p1[0]) * nx + (point[1] - s_ref.p1[1]) * ny)


def _project_to_line(point: Tuple[float, float], s_ref: Segment) -> float:
    """Paramètre `t` (en mètres) de la projection de `point` sur la
    droite portant `s_ref`, mesuré depuis `s_ref.p1` le long de la
    direction `p1 → p2`. Utilisé pour évaluer l'overlap entre deux
    segments parallèles."""
    dx = s_ref.p2[0] - s_ref.p1[0]
    dy = s_ref.p2[1] - s_ref.p1[1]
    norm_sq = dx * dx + dy * dy
    if norm_sq < 1e-24:
        return 0.0
    return (
        (point[0] - s_ref.p1[0]) * dx + (point[1] - s_ref.p1[1]) * dy
    ) / math.sqrt(norm_sq)


def _overlap_along_reference(s_ref: Segment, s_other: Segment) -> Tuple[float, float]:
    """Renvoie `(overlap_length, overlap_ratio_of_shorter)` entre `s_other`
    projeté sur la droite portant `s_ref`. Si pas d'overlap, length=0.
    Le ratio est borné dans `[0, 1]` même si overlap > segment court
    (numérique).
    """
    t_ref_a = 0.0
    t_ref_b = _segment_length(s_ref)
    t_other_a = _project_to_line(s_other.p1, s_ref)
    t_other_b = _project_to_line(s_other.p2, s_ref)
    a_lo, a_hi = min(t_ref_a, t_ref_b), max(t_ref_a, t_ref_b)
    b_lo, b_hi = min(t_other_a, t_other_b), max(t_other_a, t_other_b)
    overlap = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
    shorter = min(t_ref_b - t_ref_a, abs(t_other_b - t_other_a))
    ratio = (overlap / shorter) if shorter > 1e-12 else 0.0
    return overlap, min(ratio, 1.0)


def detect_wall_segments(
    segments: List[Segment],
    *,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.50,
    angle_tol_rad: float = math.radians(2.0),
    min_overlap_ratio: float = 0.5,
) -> Tuple[List[WallCandidate], List[Dict[str, Any]]]:
    """Détecte les paires de lignes parallèles → wall segments.

    Algorithme (O(N²) sur le nombre de segments, OK jusqu'à quelques
    milliers de lignes) :

    1. Pour chaque paire (i, j) avec j > i, vérifier :
       - même layer ;
       - angles parallèles (mod π) à `angle_tol_rad` près ;
       - distance perpendiculaire du milieu de j à la droite de i dans
         `[min_thickness_m, max_thickness_m]` ;
       - overlap projeté ≥ `min_overlap_ratio` du segment court.
    2. Si oui → synthétiser un WallCandidate :
       - centerline = projection du milieu de la paire sur la bissectrice
         des deux segments (en pratique : milieu des points appariés).
       - thickness = distance perpendiculaire.
       - confidence = overlap_ratio (proxy direct).
    3. Marquer i et j comme "used" ; segments orphelins → `rejected`.

    Returns:
        `(walls, rejected)` :
        - `walls` : liste de `WallCandidate`.
        - `rejected` : liste de `{layer, p1, p2, reason}` pour les
          segments orphelins ou les paires hors tolérance.
    """
    used: List[bool] = [False] * len(segments)
    walls: List[WallCandidate] = []
    angles = [_segment_angle_normalized(s) for s in segments]

    for i in range(len(segments)):
        if used[i]:
            continue
        s_i = segments[i]
        for j in range(i + 1, len(segments)):
            if used[j]:
                continue
            s_j = segments[j]
            if s_j.layer != s_i.layer:
                continue
            if not _angle_close(angles[i], angles[j], angle_tol_rad):
                continue
            mid_j = ((s_j.p1[0] + s_j.p2[0]) / 2.0, (s_j.p1[1] + s_j.p2[1]) / 2.0)
            d = _perp_distance(s_i, mid_j)
            if d < min_thickness_m or d > max_thickness_m:
                continue
            _, ratio = _overlap_along_reference(s_i, s_j)
            if ratio < min_overlap_ratio:
                continue

            # Pair retenue. Synthèse centerline = milieux appariés.
            # On apparie p1↔p1' et p2↔p2' par proximité projective pour
            # gérer le cas où s_j est dessiné en sens inverse de s_i.
            t_j_p1 = _project_to_line(s_j.p1, s_i)
            t_j_p2 = _project_to_line(s_j.p2, s_i)
            if t_j_p1 < t_j_p2:
                j_a, j_b = s_j.p1, s_j.p2
            else:
                j_a, j_b = s_j.p2, s_j.p1
            center_a = ((s_i.p1[0] + j_a[0]) / 2.0, (s_i.p1[1] + j_a[1]) / 2.0)
            center_b = ((s_i.p2[0] + j_b[0]) / 2.0, (s_i.p2[1] + j_b[1]) / 2.0)

            walls.append(WallCandidate(
                p1=center_a, p2=center_b,
                thickness=d,
                layer=s_i.layer,
                confidence=ratio,
                source=(i, j),
            ))
            used[i] = True
            used[j] = True
            break  # i est marqué, passer au prochain i.

    rejected: List[Dict[str, Any]] = []
    for k, s in enumerate(segments):
        if used[k]:
            continue
        rejected.append({
            "layer": s.layer,
            "p1": list(s.p1),
            "p2": list(s.p2),
            "length_m": round(_segment_length(s), 4),
            "reason": "no parallel pair found within tolerance",
        })

    return walls, rejected


# ----- Centerline fallback (Phase 3 UC1, 2026-05-12) -----------------------
#
# Pour les DXF d'archi schématiques où certaines cloisons sont
# dessinées en **simple-trait** (une seule ligne représentant l'axe du
# mur, pas 2 lignes parallèles encadrant l'épaisseur). Convention
# courante pour les cloisons légères dans les plans Suisse/FR.
#
# Stratégie en 2 étapes après le pair detection :
#  1. **Merge collinéaire** : grouper les segments orphelins par
#     droite portante, fusionner ceux qui sont collinéaires +
#     adjacents avec un gap ≤ `max_gap_m` (typique : 0.20 m pour
#     absorber les portes intérieures qui interrompent la cloison).
#  2. **Filtrage par longueur** : ne retenir que les fusions ≥
#     `min_length_m` (typique : 0.5 m pour exclure les épaulements
#     de fenêtres et les petits artefacts).
#  3. Chaque fusion devient un `WallCandidate` avec une `thickness`
#     par défaut (typique : 0.10 m pour cloison standard).


def _line_portante_key(s: Segment, angle_tol_rad: float) -> Tuple[float, float]:
    """Clé de hash approximative pour identifier la droite portant un segment.

    Renvoie `(angle_normalisé_arrondi, perpendiculaire_signée)`. Deux
    segments collinéaires (même droite) tombent dans la même clé, à la
    tolérance d'angle/distance près.

    Pour le bucket d'angle : on arrondi à la granularité `angle_tol_rad`
    en degrés pour grouper sans drift cumulé. Pour la perpendiculaire :
    on calcule la distance signée de l'origine (0,0) à la droite, ce
    qui est invariant le long de la droite.
    """
    a = _segment_angle_normalized(s)
    a_bin = round(a / max(angle_tol_rad, 1e-9))
    # Distance signée de (0,0) à la droite portant `s`.
    dx = s.p2[0] - s.p1[0]
    dy = s.p2[1] - s.p1[1]
    norm = math.sqrt(dx * dx + dy * dy)
    if norm < 1e-12:
        return (a_bin, 0.0)
    nx = -dy / norm
    ny = dx / norm
    perp = s.p1[0] * nx + s.p1[1] * ny
    # Arrondi à 1 mm pour absorber le bruit numérique sans coller des
    # cloisons proches mais distinctes.
    return (a_bin, round(perp, 3))


def _merge_collinear_segments(
    segments: List[Segment],
    *,
    max_gap_m: float = 0.20,
    angle_tol_rad: float = math.radians(2.0),
) -> List[Segment]:
    """Fusionne les segments collinéaires + adjacents (gap ≤ `max_gap_m`).

    Pour chaque groupe de segments sur la même droite (clé via
    `_line_portante_key`), on les projette sur la droite, on trie par
    position de projection, puis on fusionne les intervalles qui se
    chevauchent ou sont séparés par un gap ≤ `max_gap_m`. Cas typique :
    une cloison de 10 m interrompue par 3 portes de 0.20 m est
    représentée par 4 segments. Avec `max_gap_m=0.20`, ils fusionnent
    en un seul segment de 10 m. Avec `max_gap_m=0.0`, seuls les
    segments contigus exact ou chevauchants fusionnent.

    Layer préservé : on ne fusionne que des segments du même layer.

    Renvoie la nouvelle liste de segments fusionnés (peut être plus
    courte que l'entrée). Les segments isolés (pas de voisin
    collinéaire) sont renvoyés tels quels.
    """
    if not segments:
        return []
    by_key: Dict[Tuple[float, float, str], List[Tuple[int, Segment]]] = {}
    for i, s in enumerate(segments):
        a_bin, perp = _line_portante_key(s, angle_tol_rad)
        by_key.setdefault((a_bin, perp, s.layer), []).append((i, s))

    merged: List[Segment] = []
    for (_, _, layer), group in by_key.items():
        if len(group) == 1:
            merged.append(group[0][1])
            continue
        # Projette chaque segment sur sa droite portante (référence =
        # le premier du groupe), trie par t_min.
        ref = group[0][1]
        intervals: List[Tuple[float, float, Tuple[float, float], Tuple[float, float]]] = []
        for _, s in group:
            t1 = _project_to_line(s.p1, ref)
            t2 = _project_to_line(s.p2, ref)
            if t1 <= t2:
                intervals.append((t1, t2, s.p1, s.p2))
            else:
                intervals.append((t2, t1, s.p2, s.p1))
        intervals.sort(key=lambda x: x[0])

        # Fusion gloutonne des intervalles avec gap ≤ max_gap_m.
        cur_lo, cur_hi = intervals[0][0], intervals[0][1]
        cur_a = intervals[0][2]
        cur_b = intervals[0][3]
        for lo, hi, a, b in intervals[1:]:
            if lo - cur_hi <= max_gap_m:
                # Extend.
                if hi > cur_hi:
                    cur_hi = hi
                    cur_b = b
            else:
                merged.append(Segment(p1=cur_a, p2=cur_b, layer=layer))
                cur_lo, cur_hi = lo, hi
                cur_a, cur_b = a, b
        merged.append(Segment(p1=cur_a, p2=cur_b, layer=layer))
    return merged


def _subtract_pair_shadows(
    candidate: Segment,
    pair_walls: List[WallCandidate],
    *,
    angle_tol_rad: float = math.radians(2.0),
    exclusion_distance_m: float = 0.30,
) -> List[Segment]:
    """Soustrait les zones du `candidate` couvertes par des pair-walls
    quasi-parallèles + proches latéralement. Renvoie la liste de
    sous-segments **non couverts** (résidus).

    Approche : projection 1D sur la droite portante du candidate.
    Pour chaque pair qui passe les filtres (angle + perp_distance),
    on projette ses endpoints sur la candidate, obtient un intervalle
    `[a, b]`, et on soustrait cet intervalle de l'intervalle initial
    du candidate `[0, L]`. Résultat : liste d'intervalles résiduels →
    sous-segments du candidate.

    Cas typique observé en runtime (DXF Projet4) : une cloison interne
    est représentée par une face haute en paire et une face basse en
    simple-trait. Le centerline candidate (face basse + extension)
    chevauche partiellement le pair (face haute) — sans cette
    soustraction, on créerait 2 walls en doublon sur la zone d'overlap.
    Avec soustraction : on garde uniquement le résidu bas (la partie
    qui n'est PAS déjà couverte par le pair).
    """
    cand_angle = _segment_angle_normalized(candidate)
    cand_midpoint = (
        (candidate.p1[0] + candidate.p2[0]) / 2.0,
        (candidate.p1[1] + candidate.p2[1]) / 2.0,
    )
    L = _segment_length(candidate)
    if L < 1e-9:
        return []
    # Pour CHAQUE pair quasi-parallèle proche en perp, on collecte 2
    # intervalles :
    # - L'**ombre clampée** dans [0, L] (pour la soustraction effective).
    # - L'**extent non-clampé** sur la droite portante de la candidate
    #   (pour calculer l'enveloppe totale des pairs proches — utilisé
    #   pour détecter les résidus dans des « trous de fenêtre »).
    shadows: List[Tuple[float, float]] = []
    pair_t_min = None  # min t (non clampé) de tous les pairs proches.
    pair_t_max = None  # max t.
    for w in pair_walls:
        pair_seg = Segment(p1=w.p1, p2=w.p2, layer=w.layer)
        pair_angle = _segment_angle_normalized(pair_seg)
        if not _angle_close(cand_angle, pair_angle, angle_tol_rad):
            continue
        d_mid = _perp_distance(pair_seg, cand_midpoint)
        if d_mid > exclusion_distance_m:
            continue
        # Projection non clampée pour l'enveloppe.
        t1 = _project_to_line(w.p1, candidate)
        t2 = _project_to_line(w.p2, candidate)
        t_lo, t_hi = (t1, t2) if t1 <= t2 else (t2, t1)
        if pair_t_min is None or t_lo < pair_t_min:
            pair_t_min = t_lo
        if pair_t_max is None or t_hi > pair_t_max:
            pair_t_max = t_hi
        # Ombre clampée à [0, L] pour la soustraction.
        lo = max(0.0, t_lo)
        hi = min(L, t_hi)
        if hi > lo:
            shadows.append((lo, hi))

    # Si aucun pair n'est proche en perp, pas de soustraction.
    if pair_t_min is None:
        return [candidate]

    pair_extent_lo = pair_t_min
    pair_extent_hi = pair_t_max

    # Merge des intervalles d'ombre chevauchants (sur les shadows
    # clampés). Si shadows vide (pairs tous hors de la zone candidate),
    # la candidate entière forme un résidu, qui sera ensuite filtré
    # par l'enveloppe non-clampée.
    residuals: List[Tuple[float, float]] = []
    if shadows:
        shadows.sort()
        merged: List[Tuple[float, float]] = [shadows[0]]
        for lo, hi in shadows[1:]:
            last_lo, last_hi = merged[-1]
            if lo <= last_hi:
                merged[-1] = (last_lo, max(last_hi, hi))
            else:
                merged.append((lo, hi))

        # Soustraction : complément de la candidate dans [0, L].
        cursor = 0.0
        for lo, hi in merged:
            if lo > cursor:
                residuals.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < L:
            residuals.append((cursor, L))
    else:
        residuals = [(0.0, L)]

    # Filtre des résidus tombant entièrement dans l'enveloppe des
    # pairs : ils correspondent à des « trous » entre paires
    # adjacentes (fenêtres typiquement). Tolérance epsilon pour
    # absorber le bruit numérique.
    eps = 1e-3
    filtered_residuals: List[Tuple[float, float]] = []
    for r_lo, r_hi in residuals:
        if r_lo >= pair_extent_lo - eps and r_hi <= pair_extent_hi + eps:
            # Entièrement dans l'enveloppe → trou de fenêtre, skip.
            continue
        filtered_residuals.append((r_lo, r_hi))

    # Reconstruction des sous-segments depuis les intervalles résiduels.
    # On interpole p1/p2 le long de la candidate au paramètre t.
    dx = candidate.p2[0] - candidate.p1[0]
    dy = candidate.p2[1] - candidate.p1[1]
    out: List[Segment] = []
    for lo, hi in filtered_residuals:
        u_lo = lo / L
        u_hi = hi / L
        p_lo = (
            candidate.p1[0] + dx * u_lo,
            candidate.p1[1] + dy * u_lo,
        )
        p_hi = (
            candidate.p1[0] + dx * u_hi,
            candidate.p1[1] + dy * u_hi,
        )
        out.append(Segment(p1=p_lo, p2=p_hi, layer=candidate.layer))
    return out


def detect_centerline_walls(
    orphan_segments: List[Segment],
    *,
    thickness_m: float = 0.10,
    min_length_m: float = 0.5,
    max_gap_m: float = 0.20,
    angle_tol_rad: float = math.radians(2.0),
    pair_walls: Optional[List[WallCandidate]] = None,
    exclusion_distance_m: float = 0.30,
) -> Tuple[List[WallCandidate], List[Dict[str, Any]]]:
    """Détection fallback : segments orphelins traités comme centerlines.

    Étapes :
    1. Fusion collinéaire (`_merge_collinear_segments`) pour réunir
       les fragments séparés par des portes / ouvertures de largeur
       ≤ `max_gap_m`.
    2. Filtrage longueur : ≥ `min_length_m` (exclut épaulements de
       fenêtres et autres artefacts courts).
    3. **Filtre anti-doublon** (si `pair_walls` fourni) : rejette les
       fusions qui sont dans l'ombre d'un wall pair-detected — évite
       le bug observé sur DXF de session i (faux centerlines sur les
       façades en plus des paires).
    4. Conversion en `WallCandidate` (`thickness = thickness_m`).

    Renvoie `(walls, rejected)` symétrique à `detect_wall_segments`.
    `confidence` à 0.6 par défaut (centerline = inférence partielle).
    """
    merged = _merge_collinear_segments(
        orphan_segments,
        max_gap_m=max_gap_m,
        angle_tol_rad=angle_tol_rad,
    )
    walls: List[WallCandidate] = []
    rejected: List[Dict[str, Any]] = []
    for s in merged:
        # Soustraction des ombres si pair_walls fourni : chaque zone
        # déjà couverte par un pair-wall quasi-parallèle est retirée
        # de la candidate. Renvoie 0 ou plusieurs sous-segments
        # résiduels. Évite les doublons partiels.
        if pair_walls is not None:
            sub_segments = _subtract_pair_shadows(
                s, pair_walls,
                angle_tol_rad=angle_tol_rad,
                exclusion_distance_m=exclusion_distance_m,
            )
        else:
            sub_segments = [s]

        for sub in sub_segments:
            length = _segment_length(sub)
            if length < min_length_m:
                rejected.append({
                    "layer": sub.layer,
                    "p1": list(sub.p1),
                    "p2": list(sub.p2),
                    "length_m": round(length, 4),
                    "reason": "centerline residual below min_length_m ({} < {})".format(
                        round(length, 3), min_length_m,
                    ),
                })
                continue
            walls.append(WallCandidate(
                p1=sub.p1, p2=sub.p2,
                thickness=thickness_m,
                layer=sub.layer,
                confidence=0.6,
                source=(-1, -1),
            ))
    return walls, rejected


# ----- High-level classification ----------------------------------------


@dataclass
class Classification:
    walls: List[WallCandidate] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    layer_mapping_used: Dict[str, str] = field(default_factory=dict)
    centerline_walls_count: int = 0


def classify(
    entities: List[DwgEntity],
    layer_mapping: Dict[str, str],
    *,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.50,
    angle_tol_rad: float = math.radians(2.0),
    min_overlap_ratio: float = 0.5,
    include_centerline: bool = True,
    centerline_thickness_m: float = 0.10,
    centerline_min_length_m: float = 0.5,
    centerline_max_gap_m: float = 0.20,
) -> Classification:
    """Entrée publique : entités + mapping layer→rôle → Classification.

    `layer_mapping` : dict explicite `{layer_name: "wall" | "door" | …}`
    fourni par le caller (LLM ou utilisateur), typiquement issu de
    `dwg_inspect` après confirmation des suggestions heuristiques.

    **Deux passes** :
    1. **Pair detection** (`detect_wall_segments`) — paires de lignes
       parallèles distantes de [min_thickness_m, max_thickness_m].
       C'est la voie principale, haute confidence (~1.0).
    2. **Centerline fallback** (`detect_centerline_walls`, optionnel,
       `include_centerline=True` par défaut) — sur les segments
       orphelins de la 1ère passe : fusionne les collinéaires
       (absorbe les ouvertures ≤ `centerline_max_gap_m`), filtre par
       longueur min, et synthétise un wall avec
       `thickness = centerline_thickness_m`. Confidence 0.6 (moins
       fiable qu'une vraie paire).

    Phase 1 V0 : seul le rôle `"wall"` est traité ; les autres rôles
    (door, window) seront ajoutés en phase 2.
    """
    wall_layers = [name for name, role in layer_mapping.items() if role == "wall"]
    segments = extract_straight_segments(entities, layer_filter=wall_layers)
    pair_walls, pair_rejected = detect_wall_segments(
        segments,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        angle_tol_rad=angle_tol_rad,
        min_overlap_ratio=min_overlap_ratio,
    )

    centerline_walls: List[WallCandidate] = []
    final_rejected: List[Dict[str, Any]] = list(pair_rejected)

    if include_centerline and pair_rejected:
        # Reconstruit les segments orphelins (depuis `pair_rejected` qui
        # porte les p1/p2/layer).
        orphan_segments = [
            Segment(
                p1=(r["p1"][0], r["p1"][1]),
                p2=(r["p2"][0], r["p2"][1]),
                layer=r["layer"],
            )
            for r in pair_rejected
        ]
        centerline_walls, cl_rejected = detect_centerline_walls(
            orphan_segments,
            thickness_m=centerline_thickness_m,
            min_length_m=centerline_min_length_m,
            max_gap_m=centerline_max_gap_m,
            angle_tol_rad=angle_tol_rad,
            pair_walls=pair_walls,
        )
        # Les rejected du pair detection qui ont été repris en
        # centerline ne sont plus rejected.
        final_rejected = cl_rejected

    all_walls = pair_walls + centerline_walls
    return Classification(
        walls=all_walls,
        rejected=final_rejected,
        layer_mapping_used=dict(layer_mapping),
        centerline_walls_count=len(centerline_walls),
    )
