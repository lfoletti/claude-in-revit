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


# ----- High-level classification ----------------------------------------


@dataclass
class Classification:
    walls: List[WallCandidate] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    layer_mapping_used: Dict[str, str] = field(default_factory=dict)


def classify(
    entities: List[DwgEntity],
    layer_mapping: Dict[str, str],
    *,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.50,
    angle_tol_rad: float = math.radians(2.0),
    min_overlap_ratio: float = 0.5,
) -> Classification:
    """Entrée publique : entités + mapping layer→rôle → Classification.

    `layer_mapping` : dict explicite `{layer_name: "wall" | "door" | …}`
    fourni par le caller (LLM ou utilisateur), typiquement issu de
    `dwg_inspect` après confirmation des suggestions heuristiques.

    Phase 1 V0 : seul le rôle `"wall"` est traité ; les autres rôles
    (door, window) seront ajoutés en phase 2. Les layers absents du
    mapping sont ignorés silencieusement.
    """
    wall_layers = [name for name, role in layer_mapping.items() if role == "wall"]
    segments = extract_straight_segments(entities, layer_filter=wall_layers)
    walls, rejected = detect_wall_segments(
        segments,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        angle_tol_rad=angle_tol_rad,
        min_overlap_ratio=min_overlap_ratio,
    )
    return Classification(
        walls=walls,
        rejected=rejected,
        layer_mapping_used=dict(layer_mapping),
    )
