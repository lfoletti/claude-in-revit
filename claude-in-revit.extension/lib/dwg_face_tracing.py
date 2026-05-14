"""dwg_face_tracing.py — algorithme planar face-tracing pour le contour
extérieur d'un bâtiment depuis ses segments de murs.

Motivation (JOURNAL session w Phase E) : le `dwg_create_floors_many`
utilise actuellement `_convex_hull_2d` pour le boundary des sols. Ça
marche pour P7 rectangle, mais **fail pour plans en L, ailes, retraits,
ou bâtiments avec des saillies**. La hull convexe couvre toujours plus
que le vrai contour.

L'algorithme face-tracing planar identifie le **vrai contour extérieur**
en construisant le graphe planar des centerlines de murs et en suivant
la face non-bornée. C'est la méthode standard en SIG / CAD pour ce
problème.

## Algorithme

1. **Snap endpoints** : fusionne les endpoints proches (< tol) en un
   vertex unique. Tolère les imprécisions DXF (1cm typique).
2. **Split internal intersections** : si deux segments se croisent au
   milieu (X-junction ou T-junction sans endpoint coïncidant), insère
   un vertex au point d'intersection et split les segments. Pas de
   crossing au milieu après cette étape — le graphe est planar pur.
3. **Build half-edges** : chaque segment devient 2 half-edges (un dans
   chaque direction). Au niveau de chaque vertex, les half-edges
   sortants sont ordonnés par angle CCW.
4. **Trace faces** : depuis un half-edge non visité, suit le « next »
   half-edge à chaque vertex (next CCW autour du vertex après le twin
   du half-edge entrant). Boucle jusqu'à revenir au half-edge de départ.
5. **Identify outer face** : la face dont l'aire signée (formule de
   Gauss / shoelace) est **négative** quand on la parcourt dans l'ordre
   tracé est la face non-bornée. On l'inverse pour obtenir un contour
   CCW prêt à passer à Revit.

## Limites V0

- Suppose un graphe connexe : si le DXF contient plusieurs bâtiments
  disjoints, on retourne uniquement le contour du premier (plus grand
  composant). À étendre via décomposition en composantes connexes.
- Tolérance fixe `snap_tol_m=0.01` (1cm) ; suffisant pour Revit AIA
  qui exporte avec précision millimétrique. Pour scans / DXF
  non-précis, augmenter.
- Aucun support des arcs (bulges) — les centerlines sont approximées
  par des segments droits. Suffisant pour 99% des bâtiments
  architecturaux. Caller post-segmente les arcs si besoin.
- Si les murs ne forment pas une boucle fermée (cas dégénéré : un seul
  mur, ou des fragments orphelins), retourne `None`.

Cf. JOURNAL session w Phase E pour validation P7 + plans synthétiques.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]
Segment = Tuple[Point, Point]


def _snap_point(p: Point, vertices: List[Point], tol: float) -> int:
    """Trouve l'index du vertex existant le plus proche de `p` (≤ tol),
    OU ajoute un nouveau vertex et retourne son index. Tolère les
    imprécisions DXF en fusionnant les endpoints proches."""
    for i, v in enumerate(vertices):
        if abs(p[0] - v[0]) < tol and abs(p[1] - v[1]) < tol:
            return i
    vertices.append(p)
    return len(vertices) - 1


def _segment_intersection(
    a1: Point, a2: Point, b1: Point, b2: Point,
    eps: float = 1e-9,
) -> Optional[Tuple[Point, float, float]]:
    """Intersection stricte (au milieu, pas aux endpoints) de deux
    segments [a1, a2] et [b1, b2].

    Retourne `(point, t_a, t_b)` avec t_a ∈ (eps, 1-eps) et idem pour t_b.
    `None` si parallèle, colinéaire, ou intersection AUX endpoints.
    On exclut les endpoints car ils sont déjà gérés par `_snap_point` —
    seules les vraies coupures internes (X / T au milieu) nous intéressent.
    """
    x1, y1 = a1
    x2, y2 = a2
    x3, y3 = b1
    x4, y4 = b2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < eps:
        return None  # parallel or coincident
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    # Strict interior (not at endpoints).
    if t <= eps or t >= 1.0 - eps:
        return None
    if u <= eps or u >= 1.0 - eps:
        return None
    px = x1 + t * (x2 - x1)
    py = y1 + t * (y2 - y1)
    return ((px, py), t, u)


def _build_planar_graph(
    segments: List[Segment],
    snap_tol_m: float = 0.01,
) -> Tuple[List[Point], List[Tuple[int, int]]]:
    """Construit le graphe planar : vertices + edges (paires d'indices).

    Étapes :
    1. Snap endpoints : merge clusters de points proches en un vertex
       unique.
    2. Split internal intersections : pour chaque paire de segments
       qui se croisent au milieu, insère un vertex à l'intersection et
       split les deux segments.
    3. Retourne `(vertices, edges)` où edges[i] = (v_a_idx, v_b_idx).

    Self-loops (vertex à lui-même) sont filtrés. Doublons d'edges
    (mêmes indices, ordre indifférent) sont déduplicés.
    """
    vertices: List[Point] = []
    # Phase 1 : snap endpoints. Each segment devient (idx_a, idx_b).
    indexed_segs: List[Tuple[int, int]] = []
    for s in segments:
        a = _snap_point((float(s[0][0]), float(s[0][1])), vertices, snap_tol_m)
        b = _snap_point((float(s[1][0]), float(s[1][1])), vertices, snap_tol_m)
        if a != b:
            indexed_segs.append((a, b))

    # Phase 2 : split internal intersections. On itère jusqu'à stabilité
    # (un split peut créer une nouvelle intersection à traiter — rare
    # mais possible).
    changed = True
    while changed:
        changed = False
        # Pour chaque paire (i, j), check intersection interne.
        # O(n²) — acceptable pour ≤ quelques centaines de murs.
        new_segs: List[Tuple[int, int]] = []
        skip: set = set()
        i = 0
        while i < len(indexed_segs):
            if i in skip:
                i += 1
                continue
            seg_a = indexed_segs[i]
            split_a: Optional[Tuple[int, int, int]] = None  # (other_idx, new_vertex, t_a_or_b)
            for j in range(i + 1, len(indexed_segs)):
                if j in skip:
                    continue
                seg_b = indexed_segs[j]
                if set(seg_a) & set(seg_b):
                    # Share a vertex — already handled, no internal cross.
                    continue
                a1 = vertices[seg_a[0]]
                a2 = vertices[seg_a[1]]
                b1 = vertices[seg_b[0]]
                b2 = vertices[seg_b[1]]
                hit = _segment_intersection(a1, a2, b1, b2)
                if hit is None:
                    continue
                pt, _t, _u = hit
                # Insert new vertex (or reuse if very close to existing one).
                new_v = _snap_point(pt, vertices, snap_tol_m)
                # Replace seg_a by 2 segments via new_v, same for seg_b.
                new_segs.append((seg_a[0], new_v))
                new_segs.append((new_v, seg_a[1]))
                new_segs.append((seg_b[0], new_v))
                new_segs.append((new_v, seg_b[1]))
                skip.add(j)
                split_a = (j, new_v, 0)
                changed = True
                break
            if split_a is None:
                # No split for seg_a — keep as is.
                new_segs.append(seg_a)
            i += 1
        # Filter self-loops + dedupe.
        deduped: List[Tuple[int, int]] = []
        seen: set = set()
        for a, b in new_segs:
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((a, b))
        indexed_segs = deduped

    return vertices, indexed_segs


def _trace_all_faces(
    vertices: List[Point],
    edges: List[Tuple[int, int]],
) -> List[List[int]]:
    """Trace toutes les faces du graphe planar (DCEL / half-edge approach).

    Retourne une liste de faces, chaque face étant la liste des indices
    de vertex dans l'ordre de traversal. La dernière face (par convention
    de l'algorithme) est typiquement l'outer face (aire signée négative).

    Algo :
    1. Build half-edges : chaque edge (a, b) → 2 half-edges (a→b) et (b→a).
    2. Pour chaque vertex, ordonne ses half-edges sortants par angle CCW.
    3. Pour chaque half-edge h non visité :
       - Démarre une face.
       - À chaque vertex v atteint via h, prend le « next » half-edge
         qui est : dans l'ordre CCW autour de v, après le twin de h
         (i.e. le half-edge entrant inversé).
       - Continue jusqu'à revenir au half-edge de départ.
       - Marque toutes les half-edges visitées.
    """
    # Half-edge representation : tuple (from_idx, to_idx). Twin = (to, from).
    half_edges: List[Tuple[int, int]] = []
    for a, b in edges:
        half_edges.append((a, b))
        half_edges.append((b, a))

    # Index half-edges by their source vertex, sorted CCW by exit angle.
    outgoing_by_vertex: Dict[int, List[Tuple[float, int]]] = {}
    for idx, (a, b) in enumerate(half_edges):
        va = vertices[a]
        vb = vertices[b]
        angle = math.atan2(vb[1] - va[1], vb[0] - va[0])
        outgoing_by_vertex.setdefault(a, []).append((angle, idx))
    for v in outgoing_by_vertex:
        outgoing_by_vertex[v].sort()

    # Map (a, b) → he_index for fast twin lookup.
    he_index: Dict[Tuple[int, int], int] = {
        he: i for i, he in enumerate(half_edges)
    }

    def next_he(h_idx: int) -> int:
        """Next half-edge in the face : from current half-edge h = (a → b),
        the next is the half-edge sortant de b qui est immédiatement APRÈS
        le twin (b → a) dans l'ordre CCW autour de b."""
        a, b = half_edges[h_idx]
        twin_idx = he_index[(b, a)]
        # Trouve la position du twin dans la liste outgoing de b.
        out_list = outgoing_by_vertex.get(b, [])
        twin_pos = next(
            (i for i, (_, idx) in enumerate(out_list) if idx == twin_idx),
            None,
        )
        if twin_pos is None:
            # Should not happen — graphe corrupt.
            return -1
        # Next dans l'ordre CCW : index suivant (wrap autour).
        next_pos = (twin_pos + 1) % len(out_list)
        return out_list[next_pos][1]

    visited = [False] * len(half_edges)
    faces: List[List[int]] = []
    for start in range(len(half_edges)):
        if visited[start]:
            continue
        # Trace face starting from `start`.
        face: List[int] = []
        h = start
        while not visited[h]:
            visited[h] = True
            face.append(half_edges[h][0])
            h = next_he(h)
            if h == -1:
                break  # Corrupt edge — abandon face.
            if h == start:
                break
        if len(face) >= 3:
            faces.append(face)
    return faces


def _signed_area(vertices: List[Point], face: List[int]) -> float:
    """Aire signée du polygone (formule de Gauss). Positive = CCW,
    négative = CW. La face non-bornée (outer face) a une aire signée
    NÉGATIVE quand parcourue dans l'ordre du half-edge tracing."""
    n = len(face)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = vertices[face[i]]
        x2, y2 = vertices[face[(i + 1) % n]]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def trace_outer_boundary_2d(
    wall_segments: List[Segment],
    snap_tol_m: float = 0.01,
    min_face_area_m2: float = 0.5,
) -> Optional[List[Point]]:
    """Trace le contour extérieur d'un bâtiment depuis les centerlines de
    ses murs. Retourne `None` si la topologie ne permet pas (segments
    déconnectés, gaps trop grands entre murs, fragments dangling, etc.).

    Args:
        wall_segments: liste de `((x1, y1), (x2, y2))` en mètres. Chaque
            segment représente le centerline d'un mur (V0 : ignore les
            paires de lignes parallèles, suppose qu'on a déjà extrait
            les centerlines via `dwg_classifier`).
        snap_tol_m: tolérance de fusion d'endpoints proches (défaut 1cm).
            Augmenter si les exports DXF sont imprécis (jusqu'à ~50cm
            pour des plans avec steps géométriques réels).
        min_face_area_m2: aire minimum d'une face pour qu'elle compte
            comme outer candidate (défaut 0.5 m² — filtre les cycles
            dégénérés du parsing).

    Returns:
        Polyligne fermée `[(x, y), ...]` du contour extérieur, en CCW
        (premier point ≠ dernier — Revit veut un CurveLoop ouvert). Ou
        `None` si aucune face fermée valide trouvée. **Le caller doit
        prévoir un fallback** (typiquement convex hull) car face-tracing
        échoue sur des wall sets non-fermés / fragmenté géométriquement.
    """
    if not wall_segments:
        return None
    vertices, edges = _build_planar_graph(wall_segments, snap_tol_m=snap_tol_m)
    if len(edges) < 3:
        return None
    faces = _trace_all_faces(vertices, edges)
    if not faces:
        return None
    # Filtre les faces dégénérées (aire ≈ 0).
    areas = [(f, _signed_area(vertices, f)) for f in faces]
    areas = [(f, a) for f, a in areas if abs(a) >= min_face_area_m2]
    if not areas:
        return None
    # Outer face = aire signée minimale (la plus négative — wrap CW depuis
    # l'intérieur). Si toutes les faces sont positives (graphe non fermé
    # → seules les faces intérieures détectées, pas d'outer face réelle),
    # retourne None pour signaler l'échec au caller.
    outer_face, outer_area = min(areas, key=lambda fa: fa[1])
    if outer_area >= 0:
        return None
    # Convertit en points et inverse pour CCW.
    pts = [vertices[i] for i in outer_face]
    pts = list(reversed(pts))
    return pts


def trace_floor_loops_2d(
    segments: List[Segment],
    snap_tol_m: float = 0.20,
    min_face_area_m2: float = 0.5,
) -> Optional[Dict[str, Any]]:
    """Trace toutes les boucles fermées du graphe planar et les classifie
    en `outer` (contour principal) + `holes` (trous internes).

    Use case : lecture de la géom dalle depuis les LINEs sur layer A-FLOR
    (P2-style export Revit AIA). Le contour outer + les trémies sont
    tous sur le même layer mais comme segments séparés. On reconstruit
    les loops via le graphe planar.

    Stratégie :
    1. Build planar graph (mêmes étapes que `trace_outer_boundary_2d`).
    2. Trace toutes les faces.
    3. Sépare par signe d'aire signée :
       - Aire négative → outer face (non-bornée, wrap CW depuis l'intérieur).
       - Aire positive → inner face (région bornée).
    4. Filtre les faces dégénérées (|aire| < `min_face_area_m2`).
    5. Le **outer** retourné est la face inverse-orientée de la plus
       grande aire positive (= contour de la slab).
       Les **holes** sont les autres inner faces.

    Returns:
        `{"outer": [(x,y), ...], "holes": [[(x,y), ...], ...]}`
        en CCW (premier point ≠ dernier). Ou `None` si pas de loop valide.
    """
    if not segments:
        return None
    vertices, edges = _build_planar_graph(segments, snap_tol_m=snap_tol_m)
    if len(edges) < 3:
        return None
    faces = _trace_all_faces(vertices, edges)
    if not faces:
        return None
    # Classify by signed area.
    faces_with_area = [(f, _signed_area(vertices, f)) for f in faces]
    faces_with_area = [(f, a) for f, a in faces_with_area if abs(a) >= min_face_area_m2]
    if not faces_with_area:
        return None
    # Inner faces (positive area), sorted by area descending.
    inner = [(f, a) for f, a in faces_with_area if a > 0]
    if not inner:
        # Aucune face bornée → seul le outer existe (graphe ouvert). Pas
        # de loop fermée pour dalle.
        return None
    inner.sort(key=lambda fa: -fa[1])
    # Largest inner = slab outline. Others = holes.
    outer_face, _outer_area = inner[0]
    hole_faces = [f for f, _ in inner[1:]]

    outer_pts = [vertices[i] for i in outer_face]
    holes_pts = [[vertices[i] for i in hf] for hf in hole_faces]
    return {"outer": outer_pts, "holes": holes_pts}


def trace_outer_boundary_with_fallback(
    wall_segments: List[Segment],
    fallback: List[Point],
    snap_tol_m: float = 0.05,
) -> Tuple[List[Point], str]:
    """Wrapper hybride : essaie face-tracing avec plusieurs tolérances
    croissantes, puis fallback vers le contour `fallback` (typiquement
    le convex hull) si toutes les tentatives échouent.

    Returns:
        `(boundary, method)` où `method` ∈ {"face_tracing", "convex_hull_fallback"}.
        L'usage caller : log la méthode dans le summary pour visibilité.
    """
    for tol in (snap_tol_m, snap_tol_m * 5, snap_tol_m * 20):
        result = trace_outer_boundary_2d(wall_segments, snap_tol_m=tol)
        if result is not None and len(result) >= 3:
            return result, "face_tracing"
    return list(fallback), "convex_hull_fallback"
