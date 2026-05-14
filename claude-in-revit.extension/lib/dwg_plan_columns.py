"""dwg_plan_columns.py — extraction des colonnes depuis les S-COLS INSERTs
d'un plan DXF.

Convention DXF (cible Revit-AIA, validée sur P2 « Poteaux + dalles ») :

- Layer **`S-COLS`** : poteaux structurels.
- Chaque colonne = un `INSERT` à sa position d'insertion `(x, y)`.
  Rotation est dans `attrs["rotation_deg"]`, scale dans `attrs["scale"]`.
- `block_name` au format Revit export : `<famille> - <type>-<ID>-Niveau N`.
  Exemples P2 : `Poteau HE-A - HEA160-295798-Niveau 0`,
  `Poteau HE-A - HEA160-V4-Niveau 1`.

Pure-Python, pas d'import Revit, pas d'I/O. Consomme les `DwgEntity`
normalisées de `dwg_reader.parse()`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


_LAYER_COLUMNS = "S-COLS"

# Regex : `<famille> - <type>-<instance_id>-Niveau N` où :
# - famille / type sont séparés par ` - ` (avec espaces, obligatoire)
#   pour ne pas capter les `-` *internes* dans des noms comme `HE-A`.
# - type / instance_id sont séparés par `-` simple (sans espaces).
# - instance_id matche `\w+` (V1..V9, ID numérique Revit, etc.).
_COLUMN_BLOCK_NAME_RE = re.compile(
    r"^(.+?)\s+-\s+(.+?)-\w+-Niveau\s+\d+\s*$",
)


def parse_column_block_name(block_name: str) -> Tuple[str, str]:
    """Extrait `(family_name, type_name)` d'un nom de bloc colonne Revit.

    Pattern attendu : ``<famille> - <type>-<instance_id>-Niveau N``.

    Fallback si non reconnu : ``(family_name="DXF_COL", type_name=block_name)``
    (le user peut ensuite mapper manuellement aux familles Revit
    appropriées).

    Args:
        block_name: nom de bloc DXF, e.g. ``"Poteau HE-A - HEA160-V1-Niveau 0"``.

    Returns:
        Tuple ``(family_name, type_name)``. Pour l'exemple ci-dessus :
        ``("Poteau HE-A", "HEA160")``.
    """
    if not block_name:
        return ("DXF_COL", "Unknown")
    m = _COLUMN_BLOCK_NAME_RE.match(block_name)
    if not m:
        return ("DXF_COL", block_name)
    return (m.group(1).strip(), m.group(2).strip())


@dataclass
class ColumnCandidate:
    """Une colonne détectée dans un plan DXF.

    - `position` : `(x_m, y_m)` du point d'insertion (mètres, post-conversion).
    - `family_name` / `type_name` : extraits du `block_name` par
      `parse_column_block_name`.
    - `rotation_deg` : rotation du bloc autour de Z (0 si absent).
    - `block_name` : nom de bloc brut, conservé pour traçabilité.
    - `width_m` / `depth_m` : dimensions du bbox 2D de la définition
      BLOCK (utiles pour set b/h du FamilySymbol Revit dupliqué).
      None si le bloc est vide ou si `dwg_reader` n'a pas pu calculer
      le bbox (DXF lu sans `doc` ou bloc introuvable).
    """
    position: Tuple[float, float]
    family_name: str
    type_name: str
    rotation_deg: float
    block_name: str
    width_m: Optional[float] = None
    depth_m: Optional[float] = None


def extract_columns_from_entities(entities: List[Any]) -> List[ColumnCandidate]:
    """Énumère les colonnes (INSERTs sur layer ``S-COLS``) d'un plan DXF.

    N'inclut PAS les sous-layers ``S-COLS-IDEN``, ``S-COLS-LABEL``, etc.
    (annotations textuelles). Match exact sur ``S-COLS``.

    Args:
        entities: liste de `DwgEntity` issue de `dwg_reader.parse()`.

    Returns:
        Liste de `ColumnCandidate`, triée par position `(y, x)` pour
        stabilité de l'ordre de sortie.
    """
    out: List[ColumnCandidate] = []
    for e in entities:
        if e.layer != _LAYER_COLUMNS or e.kind != "INSERT":
            continue
        if not e.coords:
            continue
        pt = e.coords[0]
        block_name = str(e.attrs.get("block_name") or "")
        family, type_ = parse_column_block_name(block_name)
        rotation = float(e.attrs.get("rotation_deg") or 0.0)
        bbox = e.attrs.get("block_bbox_m")
        width_m = None
        depth_m = None
        if bbox and isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
            width_m = float(bbox[0])
            depth_m = float(bbox[1])
        out.append(ColumnCandidate(
            position=(float(pt[0]), float(pt[1])),
            family_name=family,
            type_name=type_,
            rotation_deg=rotation,
            block_name=block_name,
            width_m=width_m,
            depth_m=depth_m,
        ))
    out.sort(key=lambda c: (c.position[1], c.position[0]))
    return out


@dataclass
class AggregatedColumn:
    """Une colonne (potentiellement aggrégée à travers plusieurs niveaux).

    - `position` : `(x_m, y_m)`.
    - `family_name` / `type_name` : depuis le block_name parsing.
    - `base_level_elev_m` : élévation du niveau base de la colonne.
    - `top_level_elev_m` : élévation du sommet (= élévation du niveau
      suivant ou `base + default_storey_height`).
    - `appearing_levels` : list des élévations où la colonne apparaît
      (pour debug). En mode per-level (défaut), c'est `[base_level_elev_m]`.
    - `block_name_sample` : un block_name représentatif (pour debug).
    - `width_m` / `depth_m` : dimensions du bbox 2D de la définition
      BLOCK (propagées depuis `ColumnCandidate`, utiles pour set b/h
      du FamilySymbol Revit).
    """
    position: Tuple[float, float]
    family_name: str
    type_name: str
    base_level_elev_m: float
    top_level_elev_m: float
    appearing_levels: List[float]
    block_name_sample: str
    width_m: Optional[float] = None
    depth_m: Optional[float] = None


def dedup_columns_within_plans(
    columns_by_level_elev: List[Tuple[float, List[ColumnCandidate]]],
    *,
    position_merge_tol_m: float = 0.05,
    default_storey_height_m: float = 3.0,
) -> List[AggregatedColumn]:
    """**Mode "per-level" (production default)** : pour chaque niveau,
    dédoublonne les candidats par position dans le plan. Pour chaque
    `(level, position_unique)`, crée 1 colonne base=level top=level_suivant
    (ou level + default_storey_height_m si dernier).

    Convention Revit structurelle : 1 colonne par étage (chaque jonction
    physique = un élément distinct). Différent de
    `aggregate_columns_across_plans` qui crée 1 colonne pour toute la
    hauteur du bâtiment.

    Pour P2 (3 niveaux × 30 positions) : 30 + 30 + 30 = 90 colonnes.
    Chaque niveau a sa colonne dédiée allant au niveau suivant (ou +
    default_storey_height pour le top level).

    Note sur l'effet View Range : un plan de niveau N peut avoir 2×
    plus d'INSERTs que prévu (les colonnes de N-1 montent visiblement
    dans le plan N + les colonnes natives à N). La dedup par position
    à l'intérieur du plan retient 1 INSERT par grille-point → 30 par
    niveau dans P2 même si N1 a 60 INSERTs.

    Args:
        columns_by_level_elev: liste de `(elevation_m, [candidates])`.
            Pas trié — la fonction trie elle-même par élévation asc.
        position_merge_tol_m: tolérance pour fusionner positions
            quasi-identiques (drift export). Défaut 5 cm.
        default_storey_height_m: hauteur d'étage pour le top level
            (qui n'a pas de niveau au-dessus). Défaut 3 m.

    Returns:
        Liste de `AggregatedColumn`, triée par (elev asc, y, x).
    """
    if not columns_by_level_elev:
        return []
    sorted_by_elev = sorted(columns_by_level_elev, key=lambda x: x[0])
    all_elevs = [e for e, _ in sorted_by_elev]
    tol = position_merge_tol_m

    out: List[AggregatedColumn] = []
    for level_idx, (elev, cands) in enumerate(sorted_by_elev):
        if not cands:
            continue
        # Bucketize positions au sein de ce niveau.
        buckets: Dict[Tuple[int, int], List[ColumnCandidate]] = {}
        for c in cands:
            bx = round(c.position[0] / tol)
            by = round(c.position[1] / tol)
            buckets.setdefault((bx, by), []).append(c)
        # Top elev pour ce niveau = niveau suivant ou +storey.
        higher_levels = [e for e in all_elevs if e > elev + 1e-6]
        if higher_levels:
            top_elev = higher_levels[0]
        else:
            top_elev = elev + default_storey_height_m
        for (bx, by), instances in buckets.items():
            avg_x = sum(c.position[0] for c in instances) / len(instances)
            avg_y = sum(c.position[1] for c in instances) / len(instances)
            sample = instances[0]
            # Médian des dimensions (parmi celles non-None) pour robustesse
            # — en pratique toutes les instances d'un même (family, type)
            # devraient avoir le même bbox, mais petit drift d'export
            # possible.
            widths = [c.width_m for c in instances if c.width_m is not None]
            depths = [c.depth_m for c in instances if c.depth_m is not None]
            w_m = sorted(widths)[len(widths) // 2] if widths else None
            d_m = sorted(depths)[len(depths) // 2] if depths else None
            out.append(AggregatedColumn(
                position=(round(avg_x, 4), round(avg_y, 4)),
                family_name=sample.family_name,
                type_name=sample.type_name,
                base_level_elev_m=elev,
                top_level_elev_m=top_elev,
                appearing_levels=[elev],
                block_name_sample=sample.block_name,
                width_m=w_m,
                depth_m=d_m,
            ))
    out.sort(key=lambda c: (c.base_level_elev_m, c.position[1], c.position[0]))
    return out


def aggregate_columns_across_plans(
    columns_by_level_elev: List[Tuple[float, List[ColumnCandidate]]],
    *,
    position_merge_tol_m: float = 0.05,
    default_storey_height_m: float = 3.0,
) -> List[AggregatedColumn]:
    """Pour chaque position unique (à `position_merge_tol_m` près),
    crée une colonne aggrégée s'étendant du plus bas niveau d'apparition
    jusqu'au sommet du plus haut niveau d'apparition.

    Stratégie « 1 colonne par grille-point » : si la même position
    apparaît à N0, N1, N2 → 1 colonne base=N0, top=top(N2)=N2+storey.
    Cas typique P2 : 30 positions × 3 niveaux d'apparition → 30 colonnes
    de 6m (N0 à N2+storey).

    Si une position n'apparaît qu'à un seul niveau, on lui assigne
    `default_storey_height_m` de hauteur.

    Args:
        columns_by_level_elev: liste de `(level_elevation_m, [columns])`.
            Pas nécessairement triée — la fonction trie elle-même par
            élévation asc.
        position_merge_tol_m: tolérance pour fusionner des positions
            quasi-identiques (drift d'export DXF). Défaut 5 cm.
        default_storey_height_m: hauteur d'étage par défaut quand une
            colonne n'apparaît qu'à un seul niveau (pas d'info top).
            Défaut 3 m.

    Returns:
        Liste de `AggregatedColumn` triée par (y, x).
    """
    if not columns_by_level_elev:
        return []

    sorted_by_elev = sorted(columns_by_level_elev, key=lambda x: x[0])
    all_elevs = [e for e, _ in sorted_by_elev]

    # Group columns by position. Pour la tolérance de fusion, on
    # bucketize en grid `position_merge_tol_m`. Une colonne à `(x, y)`
    # tombe dans le bucket `(round(x/tol), round(y/tol))`.
    tol = position_merge_tol_m
    buckets: Dict[Tuple[int, int], List[Tuple[float, ColumnCandidate]]] = {}
    for elev, cands in sorted_by_elev:
        for c in cands:
            bx = round(c.position[0] / tol)
            by = round(c.position[1] / tol)
            buckets.setdefault((bx, by), []).append((elev, c))

    out: List[AggregatedColumn] = []
    for (bx, by), instances in buckets.items():
        if not instances:
            continue
        # Position canonique = moyenne des positions des instances.
        avg_x = sum(c.position[0] for _, c in instances) / len(instances)
        avg_y = sum(c.position[1] for _, c in instances) / len(instances)
        # Élévations distinctes où cette position apparaît, triées.
        elevs_set = sorted({e for e, _ in instances})
        base_elev = elevs_set[0]
        top_elev_elev = elevs_set[-1]
        # Le sommet de la colonne = élévation du niveau **au-dessus** du
        # plus haut niveau d'apparition. Si tel niveau existe dans la
        # liste fournie, on l'utilise ; sinon, on ajoute la storey
        # height au top.
        higher_levels = [e for e in all_elevs if e > top_elev_elev + 1e-6]
        if higher_levels:
            top_m = higher_levels[0]
        else:
            top_m = top_elev_elev + default_storey_height_m
        # Family/type : prendre l'instance du plus bas niveau (= base).
        base_instance = next(c for e, c in instances if e == base_elev)
        out.append(AggregatedColumn(
            position=(round(avg_x, 4), round(avg_y, 4)),
            family_name=base_instance.family_name,
            type_name=base_instance.type_name,
            base_level_elev_m=base_elev,
            top_level_elev_m=top_m,
            appearing_levels=elevs_set,
            block_name_sample=base_instance.block_name,
        ))
    out.sort(key=lambda c: (c.position[1], c.position[0]))
    return out
