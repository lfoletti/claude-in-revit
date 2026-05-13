"""dwg_section_reader.py — extraction des niveaux et ouvertures depuis un
DXF de coupe (UC1 Phase 4 — intégration des coupes, voir JOURNAL 2026-05-12
note d'intention et 2026-05-13 entrée coupes).

Consomme les `DwgEntity` produits par `dwg_reader.parse()`. **Aucun import
ezdxf direct** — module pur sur la structure normalisée, testable hors
fichier.

Convention DXF cible (Projet4 et conventions AIA / Revit export standard) :

- Layer `A-FLOR-LEVL` : géométrie des niveaux d'une coupe.
  - LIGNES horizontales = la trace du niveau (Y = élévation × 1000 mm).
  - MTEXT par paires juxtaposées : nom (`Niveau 0`, `Niveau 1`) + valeur
    (`0`, `3.00`, `6.00` en mètres).
  - INSERT de bloc `Niveau - Marqueur de niveau - Point triangulaire` =
    symbole graphique (ignoré ici, sert d'indice de validation).

- Layer `A-GLAZ` : géométrie vitrée. Dans une coupe, les ouvertures
  sont des **INSERT** du même bloc que dans le plan, avec un ID
  numérique partagé (ex `... - Appui en aluminium-255828-Coupe 1` ↔
  `... - Appui en aluminium-255828-Niveau 0`). Le matching coupe ↔
  plan se fait par cet ID.

- Layer `A-AREA-IDEN` : présent uniquement dans les plans (labels
  pièces). Sert à classifier plan vs coupe.

Pas de support DWG ici — le caller utilise `dwg_reader.parse()` qui shell
out via ODA. Le présent module ne sait que lire la structure normalisée.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .dwg_reader import DwgEntity


# ----- Constantes de layer ----------------------------------------------

LAYER_LEVELS = "A-FLOR-LEVL"
LAYER_GLAZING = "A-GLAZ"
LAYER_AREA_LABELS = "A-AREA-IDEN"


# ----- Classification plan vs coupe -------------------------------------


def classify_dxf(layers_meta: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Classifie un DXF en `"plan"`, `"section"` ou `"unknown"` à partir du
    métadata des layers retourné par `dwg_reader.parse()`.

    Heuristique :
    - Présence de `A-FLOR-LEVL` avec ≥ 1 LINE → coupe (signature niveaux).
    - Sinon présence de `A-AREA-IDEN` avec ≥ 1 MTEXT → plan (labels pièces).
    - Sinon : `unknown`. Caller peut forcer via paramètre dédié.

    Args:
        layers_meta: la valeur de `meta["layers"]` retournée par `parse`.

    Returns:
        `(kind, evidence)` où `evidence` détaille ce qui a déclenché la
        classification (utile au LLM pour expliquer son choix).
    """
    levels_layer = next((l for l in layers_meta if l["name"] == LAYER_LEVELS), None)
    area_layer = next((l for l in layers_meta if l["name"] == LAYER_AREA_LABELS), None)

    if levels_layer and levels_layer["kinds"].get("LINE", 0) >= 1:
        return "section", {
            "trigger": "A-FLOR-LEVL with horizontal lines",
            "lines_count": levels_layer["kinds"].get("LINE", 0),
            "mtext_count": levels_layer["kinds"].get("MTEXT", 0),
        }
    if area_layer and area_layer["kinds"].get("MTEXT", 0) >= 1:
        return "plan", {
            "trigger": "A-AREA-IDEN with MTEXT labels",
            "mtext_count": area_layer["kinds"].get("MTEXT", 0),
        }
    return "unknown", {
        "trigger": "no recognized signature",
        "available_layers": [l["name"] for l in layers_meta],
    }


# ----- Block name parsing -----------------------------------------------
#
# Convention observée Projet4 / Revit export DXF :
#
#     "1 Vantail - Droit - 2_00 m x 1_40 m - Appui en aluminium-255828-Niveau 0"
#     "1 Vantail - Droit - 2_00 m x 1_40 m - Appui en aluminium-255828-Coupe 1"
#
# - L'ID numérique apparaît entre le suffixe famille et le suffixe vue
#   (Niveau / Coupe). C'est un identifiant Revit interne stable, partagé
#   entre les vues d'un même Family Type.
# - Les dimensions sont encodées `<W>_<frac> m x <H>_<frac> m` (underscore
#   décimal au lieu de point pour éviter le conflit avec le séparateur
#   de path Windows).

# L'ID Revit peut être numérique (`258141`) ou alphanumérique
# (`V1`, `V2` pour des variants) — observé sur Projet4 re-exporté
# 2026-05-13 (Coupe 2 contient un bloc `-V1-Coupe 2`). On accepte
# `[A-Za-z0-9_]+` mais on borne fermement par `-` + le suffixe vue
# pour éviter de capturer des fragments arbitraires du nom.
_BLOCK_ID_RE = re.compile(
    r"-([A-Za-z0-9_]+)-(?:Niveau|Coupe|Plan|Elevation)\b", re.IGNORECASE,
)
_BLOCK_DIM_RE = re.compile(r"(\d+)_(\d+)\s*m\s*x\s*(\d+)_(\d+)\s*m", re.IGNORECASE)


def parse_block_id(block_name: str) -> Optional[str]:
    """Extrait l'ID numérique commun entre une instance de bloc en plan et
    en coupe. Retourne `None` si le pattern n'est pas reconnu (caller
    bascule sur un matching plus tolérant).
    """
    match = _BLOCK_ID_RE.search(block_name)
    return match.group(1) if match else None


def parse_block_dimensions(block_name: str) -> Optional[Tuple[float, float]]:
    """Extrait `(width_m, height_m)` depuis le nom de bloc.

    `2_00 m x 1_40 m` → `(2.00, 1.40)`. Retourne `None` si non reconnu.
    """
    match = _BLOCK_DIM_RE.search(block_name)
    if not match:
        return None
    w_int, w_frac, h_int, h_frac = match.groups()
    width = float(f"{w_int}.{w_frac}")
    height = float(f"{h_int}.{h_frac}")
    return (width, height)


# ----- Niveaux ----------------------------------------------------------


@dataclass
class Level:
    """Un niveau extrait d'une coupe.

    `elevation_m` : valeur absolue en mètres telle qu'inscrite dans le
    MTEXT (ou déduite de la position Y de la ligne porteuse si le texte
    est absent / illisible). `y_dxf_m` : position Y brute de la ligne
    dans le DXF, en mètres (coords post-conversion d'unités).
    """
    name: str
    elevation_m: float
    y_dxf_m: float
    line_x_range_m: Tuple[float, float]
    source: str  # "mtext_label+value", "mtext_value_only", "line_only_inferred"


# Tolérance verticale pour rapprocher un MTEXT d'une LINE de niveau.
# Sur Projet4 : MTEXTs à Y=321 / Y=951 pour la ligne à Y=0 → tolérance
# 1.5m suffit. On élargit un peu à 2m pour absorber les variantes.
_LEVEL_MTEXT_VERTICAL_TOL_M = 2.0

# Tolérance horizontale : le MTEXT du niveau est à droite de la ligne
# (au-delà de son end X), à quelques mm. On accepte un offset jusqu'à
# 5% de la longueur de la ligne, ou 1m absolu.
_LEVEL_MTEXT_HORIZONTAL_TOL_M = 1.0


def _is_level_value_text(text: str) -> Optional[float]:
    """Reconnaît un texte de valeur de niveau (`0`, `3.00`, `+6.00`,
    `-1.50`) et retourne la valeur en mètres. None si pas un nombre.
    """
    cleaned = text.strip().replace(",", ".").lstrip("+")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _is_level_name_text(text: str) -> bool:
    """Reconnaît un texte de nom de niveau (`Niveau 0`, `RDC`, `Etage 1`,
    `R+1`, etc.). Heuristique large : non-vide et non purement numérique.
    """
    stripped = text.strip()
    if not stripped:
        return False
    return _is_level_value_text(stripped) is None


def read_levels(entities: List[DwgEntity]) -> List[Level]:
    """Extrait les niveaux d'un DXF de coupe.

    1. Collecte les LIGNES horizontales sur `A-FLOR-LEVL`.
    2. Collecte les MTEXT sur le même layer.
    3. Pour chaque ligne, cherche les MTEXT proches verticalement et
       horizontalement, distingue nom vs valeur par contenu.
    4. Si seul un MTEXT « valeur » présent → utilise-le pour l'élévation
       et nomme `Niveau ?`.
    5. Si aucun MTEXT proche → infère `elevation_m = y_dxf_m` (cohérent
       avec la convention `y_mm / 1000 = elevation_m` observée), nom
       `Niveau ?`.

    Retourne la liste triée par élévation croissante.
    """
    lines: List[DwgEntity] = []
    texts: List[DwgEntity] = []
    for e in entities:
        if e.layer != LAYER_LEVELS:
            continue
        if e.kind == "LINE":
            (x1, y1, _), (x2, y2, _) = e.coords
            if abs(y1 - y2) < 1e-3:  # horizontale
                lines.append(e)
        elif e.kind in ("TEXT", "MTEXT"):
            texts.append(e)

    levels: List[Level] = []
    for line in lines:
        (x1, y1, _), (x2, _, _) = line.coords
        y_line = y1
        x_min, x_max = min(x1, x2), max(x1, x2)

        name_text: Optional[str] = None
        value_m: Optional[float] = None
        for t in texts:
            tx, ty, _ = t.coords[0]
            if abs(ty - y_line) > _LEVEL_MTEXT_VERTICAL_TOL_M:
                continue
            if ty < y_line - 0.5:
                # MTEXT en-dessous du niveau → appartient probablement
                # au niveau précédent. On exige que le texte soit
                # au-dessus de la ligne (cohérent avec Projet4).
                continue
            # Horizontalement : juste à droite de la ligne, ou dans
            # son extent. On accepte une marge `tol`.
            if tx < x_min - _LEVEL_MTEXT_HORIZONTAL_TOL_M:
                continue
            content = t.attrs.get("text", "")
            value = _is_level_value_text(content)
            if value is not None:
                if value_m is None:
                    value_m = value
            elif _is_level_name_text(content):
                if name_text is None:
                    name_text = content.strip()

        if name_text and value_m is not None:
            source = "mtext_label+value"
            elev = value_m
            name = name_text
        elif value_m is not None:
            source = "mtext_value_only"
            elev = value_m
            name = f"Niveau {len(levels)}"
        elif name_text is not None:
            # Pas de valeur : infère depuis Y de la ligne.
            source = "mtext_label_only_inferred_elevation"
            elev = round(y_line, 4)
            name = name_text
        else:
            source = "line_only_inferred"
            elev = round(y_line, 4)
            name = f"Niveau {len(levels)}"

        levels.append(Level(
            name=name,
            elevation_m=elev,
            y_dxf_m=round(y_line, 6),
            line_x_range_m=(round(x_min, 4), round(x_max, 4)),
            source=source,
        ))

    levels.sort(key=lambda lv: lv.elevation_m)
    # Re-numérote les noms inférés pour qu'ils soient croissants
    # APRÈS le tri (sinon "Niveau 0" peut se retrouver en haut).
    auto_idx = 0
    for lv in levels:
        if lv.name.startswith("Niveau ") and lv.source.endswith("_only"):
            lv.name = f"Niveau {auto_idx}"
            auto_idx += 1
    return levels


# ----- Ouvertures en coupe ----------------------------------------------


@dataclass
class SectionOpening:
    """Une ouverture (fenêtre / porte vitrée) lue depuis un INSERT en coupe.

    `x_dxf_m` / `y_dxf_m` : position d'insertion du bloc en coupe, en
    mètres (cf. convention DXF, Y monte = élévation).
    `block_id` : ID Revit numérique partagé avec le plan (None si pattern
    non reconnu).
    `width_m`, `height_m` : extraits du nom de bloc (None si absents).
    """
    block_name: str
    block_id: Optional[str]
    x_dxf_m: float
    y_dxf_m: float
    rotation_deg: float
    width_m: Optional[float]
    height_m: Optional[float]


def read_section_openings(entities: List[DwgEntity]) -> List[SectionOpening]:
    """Extrait les INSERT du layer `A-GLAZ` (ouvertures vitrées) d'un DXF
    de coupe ou de plan. Le caller distingue les rôles via `classify_dxf`.
    """
    out: List[SectionOpening] = []
    for e in entities:
        if e.layer != LAYER_GLAZING or e.kind != "INSERT":
            continue
        x, y, _ = e.coords[0]
        block_name = e.attrs.get("block_name", "")
        out.append(SectionOpening(
            block_name=block_name,
            block_id=parse_block_id(block_name),
            x_dxf_m=round(x, 6),
            y_dxf_m=round(y, 6),
            rotation_deg=float(e.attrs.get("rotation_deg", 0.0)),
            width_m=None,
            height_m=None,
        ))
        dims = parse_block_dimensions(block_name)
        if dims is not None:
            out[-1].width_m, out[-1].height_m = dims
    return out


# ----- Matching coupe ↔ plan --------------------------------------------


@dataclass
class OpeningMatch:
    """Une correspondance section ↔ plan pour une ouverture donnée.

    `plan_indices` : index des plan_openings partageant le block_id du
    section_opening (cardinalité > 1 si plusieurs instances dans le
    plan — typique des façades à fenêtres répétées).
    """
    section_index: int
    block_id: str
    plan_indices: List[int]


def match_openings(
    plan_openings: List[SectionOpening],
    section_openings: List[SectionOpening],
) -> Tuple[List[OpeningMatch], List[int], List[int]]:
    """Apparie les ouvertures coupe → plan par `block_id` partagé.

    Retourne :
    - `matches` : un `OpeningMatch` par section_opening qui a au moins un
      pendant dans le plan (potentiellement plusieurs candidats).
    - `unmatched_section` : index des section_openings sans match.
    - `unmatched_plan` : index des plan_openings non référencés par un
      section_opening.

    Le caller décide ensuite comment résoudre les cas N→1 (typiquement
    via géo-ref par pointage user — module à venir).
    """
    plan_by_id: Dict[str, List[int]] = {}
    for i, o in enumerate(plan_openings):
        if o.block_id is None:
            continue
        plan_by_id.setdefault(o.block_id, []).append(i)

    matches: List[OpeningMatch] = []
    matched_plan_indices: set = set()
    unmatched_section: List[int] = []
    for j, s in enumerate(section_openings):
        if s.block_id is None or s.block_id not in plan_by_id:
            unmatched_section.append(j)
            continue
        pis = plan_by_id[s.block_id]
        matches.append(OpeningMatch(
            section_index=j, block_id=s.block_id, plan_indices=list(pis),
        ))
        matched_plan_indices.update(pis)

    unmatched_plan = [
        i for i in range(len(plan_openings)) if i not in matched_plan_indices
    ]
    return matches, unmatched_section, unmatched_plan
