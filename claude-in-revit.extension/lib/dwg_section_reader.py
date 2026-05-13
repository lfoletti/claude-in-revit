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

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .dwg_reader import DwgEntity


# ----- Source detection (Étape 4 Phase 1) -------------------------------
#
# Identifie la convention de nommage des layers d'un DXF, pour appliquer
# le bon dictionnaire de mapping aux étapes suivantes (find walls,
# section markers, levels, etc.).
#
# Conventions supportées V0 :
# - **AIA** (American Institute of Architects, US standard, ce que
#   Revit exporte par défaut) : préfixe `<discipline>-<group>-<modifier>`
#   où discipline ∈ {A, S, M, E, G, C, L, P, Q, T, V, X, Z}.
#   Exemples : `A-WALL`, `A-FLOR-LEVL`, `G-ANNO-SYMB`, `S-COLS`.
# - **ISO 13567** (International standard) : codes alphanumériques
#   structurés courts, sans mots. Format type `A23G---N1` (agent +
#   element + presentation + ...). Difficile à matcher strictement
#   parce que beaucoup d'exports « ISO-style » divergent du standard
#   strict. Heuristique : layer = courte (≤ 10 char), alpha+num
#   uniquement, contient des digits, pas de mot reconnaissable.
#
# Extensible : ajouter une entry à `_SOURCE_DETECTORS` pour d'autres
# conventions (ArchiCAD, BS1192, AllPlan, etc.).

import re as _re

_AIA_DISCIPLINES = "ASMEGCLPQTVXZ"
_AIA_LAYER_RE = _re.compile(
    r"^[" + _AIA_DISCIPLINES + r"]-[A-Z]{3,5}(-[A-Z0-9]+)*$",
    _re.IGNORECASE,
)
# ISO 13567 strict serait `^[A-Z]\d{2}[A-Z]\d{2}[A-Z][A-Z0-9-]+$` mais
# trop restrictif. Heuristique : court, alphanumérique, contient au moins
# 1 digit, et pas un mot anglais/français reconnaissable.
_ISO_LAYER_RE = _re.compile(r"^[A-Z0-9-]{4,10}$")
_ISO_LAYER_HAS_DIGIT = _re.compile(r"\d")
# Mots habituels qui révèlent une convention « parlée » (FR + EN) — pas
# AIA ni ISO, mais une 3e convention « language-based ». Pour V0 on les
# range dans `other` ; on raffinera si une 3e source devient pertinente.
_LANGUAGE_LAYER_PATTERNS = [
    _re.compile(p, _re.IGNORECASE) for p in (
        r"^(mur|wall|cloison|paroi)",
        r"^(fenetre|window|fen[êe]tre|vitrage)",
        r"^(porte|door|ouverture)",
        r"^(sol|floor|dalle|plancher|slab)",
        r"^(plafond|ceiling)",
        r"^(toit|toiture|roof)",
    )
]


def _is_aia_layer(name: str) -> bool:
    """True si le nom suit le pattern AIA `<lettre>-<group>(-<mod>)*`."""
    return bool(_AIA_LAYER_RE.match(name))


def _is_iso_layer(name: str) -> bool:
    """True si le nom ressemble à un code ISO 13567 (alphanumérique court
    avec au moins 1 digit, pas de mot reconnaissable).
    """
    if not _ISO_LAYER_RE.match(name):
        return False
    if not _ISO_LAYER_HAS_DIGIT.search(name):
        return False
    # Exclude layers that match a language pattern (e.g., "M3" alone is
    # OK for ISO, but "MUR3" should not be ISO).
    for pat in _LANGUAGE_LAYER_PATTERNS:
        if pat.match(name):
            return False
    return True


# Set de layers à ignorer pour le score (présents dans tous les DXF, peu
# discriminants).
_LAYER_NOISE = {"0", "Defpoints", "DEFPOINTS"}


def identify_source(layers_meta: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Identifie la convention de nommage des layers du DXF.

    Score = ratio de layers matching chaque convention. Le winner est
    celui qui dépasse 50% (sinon `"other"`). Égalité → AIA prévaut
    (plus précis comme contrainte de pattern).

    Args:
        layers_meta: la valeur de `meta["layers"]` retournée par
            `dwg_reader.parse()`. Doit contenir au moins `name`.

    Returns:
        `{source: str, confidence: float, evidence: dict, layers: list}`
        - `source` ∈ `"aia" | "iso" | "other"`.
        - `confidence` : ratio de layers matching la convention winner.
        - `evidence` : counts par convention + sample de layers
          (caller-facing).
    """
    layer_names = [l["name"] for l in layers_meta if l.get("name")]
    # Exclure les layers bruit pour le scoring.
    significant = [n for n in layer_names if n not in _LAYER_NOISE]
    if not significant:
        return {
            "source": "other",
            "confidence": 0.0,
            "evidence": {
                "aia_count": 0, "iso_count": 0, "language_count": 0,
                "total_significant": 0, "layers": layer_names,
            },
        }

    aia_layers = [n for n in significant if _is_aia_layer(n)]
    iso_layers = [n for n in significant if _is_iso_layer(n) and not _is_aia_layer(n)]
    language_layers = [
        n for n in significant
        if any(pat.match(n) for pat in _LANGUAGE_LAYER_PATTERNS)
    ]

    n = len(significant)
    aia_ratio = len(aia_layers) / n
    iso_ratio = len(iso_layers) / n
    language_ratio = len(language_layers) / n

    # Winner : ratio > 0.5 et > autres.
    if aia_ratio >= 0.5 and aia_ratio >= iso_ratio:
        source = "aia"
        confidence = aia_ratio
    elif iso_ratio >= 0.5 and iso_ratio > aia_ratio:
        source = "iso"
        confidence = iso_ratio
    else:
        source = "other"
        # Confidence est faible quand on ne reconnaît pas — prendre le
        # max des 3 ratios comme indicateur de proximité.
        confidence = max(aia_ratio, iso_ratio, language_ratio)

    return {
        "source": source,
        "confidence": round(confidence, 3),
        "evidence": {
            "aia_count": len(aia_layers),
            "iso_count": len(iso_layers),
            "language_count": len(language_layers),
            "total_significant": n,
            "aia_sample": aia_layers[:5],
            "iso_sample": iso_layers[:5],
            "language_sample": language_layers[:5],
            "all_layers": layer_names,
        },
    }


# ----- Constantes de layer ----------------------------------------------

LAYER_LEVELS = "A-FLOR-LEVL"
LAYER_GLAZING = "A-GLAZ"
LAYER_AREA_LABELS = "A-AREA-IDEN"
LAYER_WALLS = "A-WALL"


# ----- Classification plan vs coupe -------------------------------------


def classify_dxf(
    layers_meta: List[Dict[str, Any]],
    file_name: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Classifie un DXF en `"plan"`, `"section"`, `"elevation"` ou
    `"unknown"`.

    Heuristique (priorité descendante) :
    1. Si `file_name` contient « élévation » / « elevation » AND le
       DXF a des layers section-like (A-FLOR-LEVL) → `"elevation"`.
       Distinction nécessaire car élévations et coupes ont la même
       signature de layers — c'est le filename qui les discrimine.
    2. Présence de `A-AREA-IDEN` avec ≥ 1 MTEXT → `"plan"` (labels
       pièces, uniquement présents dans les plans).
    3. Présence de `A-FLOR-LEVL` avec ≥ 1 LINE → `"section"`.
    4. Sinon : `"unknown"`.

    Args:
        layers_meta: la valeur de `meta["layers"]` retournée par
            `dwg_reader.parse()`.
        file_name: nom du fichier (sans path) ou path complet. Utilisé
            pour la détection élévation. Optionnel — sans, l'élévation
            est classée comme `"section"` (signature layers identique).

    Returns:
        `(kind, evidence)` où `evidence` détaille ce qui a déclenché la
        classification.
    """
    levels_layer = next((l for l in layers_meta if l["name"] == LAYER_LEVELS), None)
    area_layer = next((l for l in layers_meta if l["name"] == LAYER_AREA_LABELS), None)

    has_levels_signature = (
        levels_layer is not None
        and levels_layer["kinds"].get("LINE", 0) >= 1
    )
    has_plan_signature = (
        area_layer is not None
        and area_layer["kinds"].get("MTEXT", 0) >= 1
    )

    file_lower = (file_name or "").lower()

    # 1. Elevation : filename indique élévation + signature de section.
    if has_levels_signature and file_name:
        lname = file_lower
        if any(kw in lname for kw in ("élévation", "elevation", "elévation")):
            # Direction parsing : ORDRE IMPORTANT — Ouest avant Est
            # (sinon "est" matche en substring "ouest").
            direction = None
            for d, aliases in (
                ("Ouest", ("ouest", "west")),
                ("Nord", ("nord", "north")),
                ("Sud", ("sud", "south")),
                ("Est", ("est", "east")),
            ):
                if any(a in lname for a in aliases):
                    direction = d
                    break
            return "elevation", {
                "trigger": "filename contains 'elevation' + A-FLOR-LEVL signature",
                "direction": direction,
                "lines_count": levels_layer["kinds"].get("LINE", 0),
                "mtext_count": levels_layer["kinds"].get("MTEXT", 0),
            }

    # 2. Plan : labels pièces présents OU filename indique "Plan"/"Niveau"
    # sans signature levels (les plans n'ont pas A-FLOR-LEVL en général).
    if has_plan_signature:
        return "plan", {
            "trigger": "A-AREA-IDEN with MTEXT labels",
            "mtext_count": area_layer["kinds"].get("MTEXT", 0),
        }
    if file_name and not has_levels_signature:
        if any(kw in file_lower for kw in (
            "plan d'étage", "plan d'etage", "plan floor", "floor plan",
            "niveau", "level", "rdc", "etage", "étage",
        )):
            return "plan", {
                "trigger": "filename suggests plan + no A-FLOR-LEVL signature",
                "file_keyword": next(
                    (k for k in ("plan", "niveau", "level", "rdc")
                     if k in file_lower), None,
                ),
            }

    # 3. Section.
    if has_levels_signature:
        return "section", {
            "trigger": "A-FLOR-LEVL with horizontal lines",
            "lines_count": levels_layer["kinds"].get("LINE", 0),
            "mtext_count": levels_layer["kinds"].get("MTEXT", 0),
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


# ----- Section markers in plan (Étape 2 Phase 1) ------------------------
#
# Convention observée Projet4 (export Revit AIA) — layer `G-ANNO-SYMB` :
# - LINEs longues : les traits de coupe et lignes d'élévation.
# - INSERTs aux extrémités : blocs symbole. Le NOM du bloc discrimine :
#   - `Coupe - Marque de la ligne de coupe - *`  → trait de coupe.
#   - `Coupe - Extrémité de la ligne de coupe - *`  → tête de coupe.
#   - `Elévation - Flèche *`  → marqueur d'élévation (façade view), pas
#     une coupe.
# - Le reste (MTEXT minimalistes, etc.) est ignoré.
#
# Autres conventions à anticiper : `G-ANNO-SECT*` (AIA pur), variations
# ArchiCAD (à découvrir).

# Layer prefixes habituels pour les annotations de coupe.
SECTION_LAYER_HINTS = ("G-ANNO-SYMB", "G-ANNO-SECT", "G-ANNO")

# Mots-clés FR + EN qui qualifient un bloc d'annotation de coupe.
SECTION_BLOCK_KEYWORDS = ("coupe", "section")
ELEVATION_BLOCK_KEYWORDS = ("elevation", "elévation", "élévation")

# Tolérance pour considérer qu'un INSERT est « au bout » d'une LINE
# (mètres). Sur Projet4 les marqueurs sont posés EXACTEMENT sur les
# endpoints, donc tolérance fine suffit ; on prend 0.5m pour absorber
# d'éventuelles approximations.
_MARKER_ENDPOINT_TOL_M = 0.5

# Longueur min d'une LINE pour qu'elle soit considérée comme trait de
# coupe (mètres). Filtre les petites lignes décoratives.
_MIN_SECTION_LINE_LENGTH_M = 2.0


@dataclass
class SectionMarker:
    """Un trait de coupe ou d'élévation détecté dans un plan.

    `kind` : "section" (vrai trait de coupe traversant le bâtiment) ou
    "elevation" (marqueur de façade — vue élévation Revit). Le caller
    veut typiquement filtrer sur `kind == "section"`.

    `is_vertical` / `is_horizontal` posés par tolérance (1° d'écart) :
    facilite l'inférence de la direction de vue (perpendiculaire au
    trait).

    `inferred_view_dir` : direction de regard inférée depuis la rotation
    du bloc marqueur (None si pas inférable). Convention : block
    `Coupe - Marque/Extrémité` Revit a une orientation par défaut
    "up" (+Y) ; la rotation de l'INSERT (CCW depuis +Y) donne la
    direction du regard. L'agent peut utiliser cette inférence
    directement, en gate-confirmant 1 fois avec l'user pour fiabilité.

    `view_dir_candidates` : 2 options possibles (fallback si l'inférence
    rate ou si l'user veut renverser).

    `associated_blocks` : INSERTs qui ont déclenché la classification —
    utile pour debug et pour que l'agent puisse expliquer son choix.
    """
    kind: str
    p1_m: Tuple[float, float]
    p2_m: Tuple[float, float]
    length_m: float
    is_vertical: bool
    is_horizontal: bool
    view_dir_candidates: List[str]
    associated_blocks: List[Dict[str, Any]]
    source_layer: str
    inferred_view_dir: Optional[str] = None


def _line_is_horizontal(p1: Tuple[float, float], p2: Tuple[float, float], tol: float = 0.01) -> bool:
    return abs(p2[1] - p1[1]) < tol


def _line_is_vertical(p1: Tuple[float, float], p2: Tuple[float, float], tol: float = 0.01) -> bool:
    return abs(p2[0] - p1[0]) < tol


# Convention observée Projet4 (Revit AIA) : les blocs `Coupe - Marque ...`
# et `Coupe - Extrémité ...` sont définis avec une géométrie pointant
# vers +Y (« up ») dans leur repère local. La rotation de l'INSERT
# (en degrés, CCW depuis +Y) donne donc la direction du regard.
#
# Si une autre source d'export utilise une orientation par défaut
# différente, modifier `_MARKER_BLOCK_DEFAULT_DIR_DEG` ou paramétrer.
_MARKER_BLOCK_DEFAULT_DIR_DEG = 90.0  # +Y = 90° en convention math (0° = +X)


def _infer_view_dir_from_marker_rotation(rotation_deg: float) -> Optional[str]:
    """Convertit la rotation d'un INSERT marqueur de coupe en direction
    de regard cardinale.

    Le bloc Revit `Coupe - Marque ...` a une orientation par défaut
    pointant vers `+Y` (« up » en convention plan). La rotation CCW de
    l'INSERT, appliquée à cette orientation, donne la direction réelle
    du regard. On snap au cardinal le plus proche (left / right / up /
    down). Retourne None si le snap est ambigu (rotation à 45° ± tol).

    Args:
        rotation_deg: rotation de l'INSERT en degrés (CCW, comme dans le
            DXF).

    Returns:
        `"left" | "right" | "up" | "down"` ou None.
    """
    import math
    # Default direction = +Y. CCW rotation by rotation_deg :
    rad = math.radians(float(rotation_deg))
    dx = -math.sin(rad)
    dy = math.cos(rad)
    # Snap to nearest cardinal — exiger qu'une composante domine claire-
    # ment l'autre pour éviter les ambiguïtés à 45° (rare en pratique
    # pour des coupes orthogonales).
    abs_dx, abs_dy = abs(dx), abs(dy)
    if abs(abs_dx - abs_dy) < 0.1:
        return None  # rotation oblique, pas de cardinal clair
    if abs_dx > abs_dy:
        return "right" if dx > 0 else "left"
    return "up" if dy > 0 else "down"


def _block_name_matches(name: str, keywords: Tuple[str, ...]) -> bool:
    """Case-insensitive substring match contre une liste de keywords."""
    lname = name.lower()
    return any(kw in lname for kw in keywords)


def _layer_matches_hints(layer: str, hints: Tuple[str, ...]) -> bool:
    """Layer match prefix-style ou contains pour les hints à wildcard."""
    upper = layer.upper()
    for h in hints:
        if h.upper() in upper:
            return True
    return False


def find_section_markers(
    entities: List[DwgEntity],
    *,
    layer_hints: Optional[Tuple[str, ...]] = None,
    section_keywords: Optional[Tuple[str, ...]] = None,
    elevation_keywords: Optional[Tuple[str, ...]] = None,
    min_length_m: float = _MIN_SECTION_LINE_LENGTH_M,
    endpoint_tol_m: float = _MARKER_ENDPOINT_TOL_M,
) -> List[SectionMarker]:
    """Identifie les traits de coupe (et marqueurs d'élévation) dans un plan.

    Algo :
    1. Collecte les LINEs sur les layers matching `layer_hints` (défaut
       `G-ANNO-SYMB / G-ANNO-SECT / G-ANNO`).
    2. Collecte les INSERTs sur les mêmes layers, indexés par position.
    3. Pour chaque LINE de longueur > `min_length_m` :
       a. Cherche les INSERTs à ≤ `endpoint_tol_m` de chaque endpoint.
       b. Classifie chaque INSERT : block name contient un section_keyword
          → "section" ; contient un elevation_keyword → "elevation".
       c. Si au moins 1 INSERT section trouvé → kind="section".
          Sinon si au moins 1 INSERT elevation trouvé → kind="elevation".
          Sinon : LINE non qualifiée, skip.
    4. Calcule l'orientation (vertical/horizontal) et propose les 2
       candidats de direction de vue.

    Retourne la liste triée par longueur décroissante (les vraies coupes
    sont typiquement les plus longues).
    """
    hints = layer_hints if layer_hints is not None else SECTION_LAYER_HINTS
    sect_kws = section_keywords if section_keywords is not None else SECTION_BLOCK_KEYWORDS
    elev_kws = elevation_keywords if elevation_keywords is not None else ELEVATION_BLOCK_KEYWORDS

    lines: List[DwgEntity] = []
    inserts: List[DwgEntity] = []
    for e in entities:
        if not _layer_matches_hints(e.layer, hints):
            continue
        if e.kind == "LINE":
            lines.append(e)
        elif e.kind == "INSERT":
            inserts.append(e)

    markers: List[SectionMarker] = []
    for line in lines:
        (x1, y1, _), (x2, y2, _) = line.coords
        p1 = (round(x1, 4), round(y1, 4))
        p2 = (round(x2, 4), round(y2, 4))
        length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if length < min_length_m:
            continue

        # Search INSERTs near endpoints.
        nearby: List[Tuple[DwgEntity, float, str]] = []  # (insert, distance, which_endpoint)
        for ins in inserts:
            ix, iy, _ = ins.coords[0]
            d1 = ((ix - x1) ** 2 + (iy - y1) ** 2) ** 0.5
            d2 = ((ix - x2) ** 2 + (iy - y2) ** 2) ** 0.5
            if d1 <= endpoint_tol_m:
                nearby.append((ins, d1, "p1"))
            elif d2 <= endpoint_tol_m:
                nearby.append((ins, d2, "p2"))

        # Classify by block name keywords.
        kind: Optional[str] = None
        associated: List[Dict[str, Any]] = []
        for ins, dist, which in nearby:
            block_name = ins.attrs.get("block_name", "")
            if _block_name_matches(block_name, sect_kws):
                kind = "section"  # priority to section over elevation
                associated.append({
                    "block_name": block_name,
                    "x_m": round(ins.coords[0][0], 4),
                    "y_m": round(ins.coords[0][1], 4),
                    "rotation_deg": float(ins.attrs.get("rotation_deg", 0.0)),
                    "endpoint": which,
                    "kind_detected": "section",
                })
            elif _block_name_matches(block_name, elev_kws):
                if kind is None:
                    kind = "elevation"
                associated.append({
                    "block_name": block_name,
                    "x_m": round(ins.coords[0][0], 4),
                    "y_m": round(ins.coords[0][1], 4),
                    "rotation_deg": float(ins.attrs.get("rotation_deg", 0.0)),
                    "endpoint": which,
                    "kind_detected": "elevation",
                })

        if kind is None:
            continue  # LINE not qualified

        is_vert = _line_is_vertical(p1, p2)
        is_horiz = _line_is_horizontal(p1, p2)
        if is_vert:
            candidates = ["left", "right"]
        elif is_horiz:
            candidates = ["up", "down"]
        else:
            candidates = ["left", "right", "up", "down"]  # oblique — ambigu

        # Inférer la direction de regard depuis la rotation du PREMIER
        # marqueur de type "section" (pas elevation). Si aucun match
        # parmi les candidates, retomber à None.
        inferred: Optional[str] = None
        for b in associated:
            if b.get("kind_detected") != "section":
                continue
            cand = _infer_view_dir_from_marker_rotation(b["rotation_deg"])
            if cand is not None and cand in candidates:
                inferred = cand
                break

        source_layer = line.layer

        markers.append(SectionMarker(
            kind=kind,
            p1_m=p1,
            p2_m=p2,
            length_m=round(length, 4),
            is_vertical=is_vert,
            is_horizontal=is_horiz,
            view_dir_candidates=candidates,
            associated_blocks=associated,
            source_layer=source_layer,
            inferred_view_dir=inferred,
        ))

    # Sort by length desc (longest = most likely real section).
    markers.sort(key=lambda m: -m.length_m)
    return markers


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


# ----- Walls observés en coupe (Phase 2 Étape 1) ------------------------
#
# Une coupe DXF révèle les épaisseurs de murs vues "de côté" : pour
# chaque mur traversé par le trait de coupe, on observe une paire de
# segments verticaux parallèles sur le layer A-WALL (les deux faces du
# mur). La distance perpendiculaire entre ces deux faces = épaisseur ;
# l'abscisse X du DXF coupe = position le long du cut (cf. mémoire
# `project-dxf-section-anchor-investigation`).
#
# Algo : on délègue la détection paires-parallèles à
# `dwg_classifier.detect_wall_segments` (qui fait déjà ce travail
# robustement pour les plans), puis on filtre les paires verticales
# (angle ≈ π/2). Chaque paire devient un `SectionWall`.


@dataclass
class SectionWall:
    """Un mur observé dans un DXF de coupe.

    Convention DXF coupe : X = position le long du cut (m), Y =
    élévation (m). Donc un mur coupé est une paire de segments
    parallèles à Y (verticaux).

    Champs :
    - `x_cut_m` : abscisse du centerline (les deux faces ont x ≈ égal).
      Sert de clé pour matcher au plan via la convention DXF anchor.
    - `thickness_m` : distance perpendiculaire entre les 2 faces.
    - `y_bottom_m` / `y_top_m` : extension verticale visible du mur.
      Utile pour distinguer un mur plein d'un linteau ou d'une allège
      (cas N→1 dans le recoupement).
    - `layer` : layer source (typiquement `A-WALL`).
    - `confidence` : héritée du classifier (overlap ratio de la paire).
    """
    x_cut_m: float
    thickness_m: float
    y_bottom_m: float
    y_top_m: float
    layer: str
    confidence: float


def read_section_walls(
    entities: List[DwgEntity],
    *,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    vertical_tol_rad: float = math.radians(5.0),
) -> List[SectionWall]:
    """Extrait les murs visibles dans un DXF de coupe.

    Réutilise `dwg_classifier.detect_wall_segments()` sur les segments
    A-WALL et filtre les paires verticales (centerline parallèle à Y).
    Les paires horizontales (dalles, plafonds, allèges) sont écartées.

    `max_thickness_m=0.60` (vs 0.50 dans le plan) : en coupe on voit
    aussi des murs porteurs extérieurs épais et des doubles cloisons.

    Args:
        entities: les `DwgEntity` parsées du DXF coupe.
        min_thickness_m / max_thickness_m: bornes d'épaisseur (m).
        vertical_tol_rad: tolérance d'angle pour « vertical » (défaut
            5°, large pour absorber le bruit numérique en coupe).

    Returns:
        Liste de `SectionWall`, triée par `x_cut_m` croissant.
    """
    # Import local pour casser le cycle théorique (classifier n'importe
    # pas section_reader, mais autant garder une dépendance dirigée).
    from . import dwg_classifier as _cls

    segments = _cls.extract_straight_segments(
        entities, layer_filter=[LAYER_WALLS],
    )
    walls, _rejected = _cls.detect_wall_segments(
        segments,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
    )

    out: List[SectionWall] = []
    for w in walls:
        # Angle du centerline (mod π, dans [0, π)).
        dx = w.p2[0] - w.p1[0]
        dy = w.p2[1] - w.p1[1]
        angle = math.atan2(dy, dx)
        if angle < 0:
            angle += math.pi
        if angle >= math.pi:
            angle -= math.pi
        # Distance angulaire à π/2 (vertical) — distance circulaire mod π.
        delta = abs(angle - math.pi / 2.0)
        delta = min(delta, math.pi - delta)
        if delta > vertical_tol_rad:
            continue

        x_cut = (w.p1[0] + w.p2[0]) / 2.0
        y_bottom = min(w.p1[1], w.p2[1])
        y_top = max(w.p1[1], w.p2[1])
        out.append(SectionWall(
            x_cut_m=round(x_cut, 6),
            thickness_m=round(w.thickness, 6),
            y_bottom_m=round(y_bottom, 6),
            y_top_m=round(y_top, 6),
            layer=w.layer,
            confidence=round(w.confidence, 3),
        ))

    out.sort(key=lambda sw: sw.x_cut_m)
    return out


# ----- Reconcile niveaux DXF ↔ KG (Étape 5 Phase 1) ---------------------


@dataclass
class LevelReconciliation:
    """Résultat du diff entre niveaux extraits d'une coupe DXF et niveaux
    présents dans le projet KG.

    Champs :
    - `matches` : niveaux qui matchent strictement (nom + élév à ε près).
    - `name_only_matches` : même nom dans DXF + KG mais élévations
      différentes → suggère `modify_elevation`.
    - `elev_only_matches` : même élévation à ε près mais noms différents
      → suggère `rename` (à user de décider, peut être intentionnel).
    - `missing_in_project` : niveaux DXF absents du KG → `create_level`.
    - `extra_in_project` : niveaux KG sans correspondance DXF → `keep`
      par défaut (pas de `delete` automatique, trop destructeur).
    - `suggested_actions` : liste d'actions concrètes que l'agent peut
      enchaîner (avec confirmation user) pour aligner.
    """
    matches: List[Dict[str, Any]]
    name_only_matches: List[Dict[str, Any]]
    elev_only_matches: List[Dict[str, Any]]
    missing_in_project: List[Dict[str, Any]]
    extra_in_project: List[Dict[str, Any]]
    suggested_actions: List[Dict[str, Any]]


def reconcile_levels(
    coupe_levels: List["Level"],
    project_levels: List[Dict[str, Any]],
    *,
    elevation_tol_m: float = 0.01,
) -> LevelReconciliation:
    """Diff entre niveaux DXF et niveaux du projet KG.

    Algo en 2 passes :
    1. Pour chaque coupe level, chercher un project level avec nom +
       élévation matching → "match" parfait.
    2. Pour chaque non-matché, chercher par nom seul → "name_only_match"
       (à modifier elev), ou par élévation seule → "elev_only_match"
       (à renommer potentiellement).
    3. Le reste : `missing_in_project` côté coupe, `extra_in_project`
       côté KG.

    `suggested_actions` formate la suite à exécuter :
    - `create_level`: pour chaque missing_in_project
    - `modify_elevation`: pour chaque name_only_match (avec llm_id du
      project level et la nouvelle élévation)
    - `rename`: pour chaque elev_only_match (informatif — user décide)

    Args:
        coupe_levels: liste de `Level` (output de `read_levels`).
        project_levels: liste de dicts `{llm_id, name, elevation}` (output
            de `catalog_list_levels`).
        elevation_tol_m: tolérance élévation pour matching (défaut 0.01m).

    Returns:
        `LevelReconciliation` instance.
    """
    coupe_remaining = list(range(len(coupe_levels)))
    project_remaining = list(range(len(project_levels)))

    matches: List[Dict[str, Any]] = []
    # Pass 1: exact match name + elevation.
    for ci in list(coupe_remaining):
        cl = coupe_levels[ci]
        for pi in list(project_remaining):
            pl = project_levels[pi]
            if (
                pl.get("name") == cl.name
                and abs(float(pl.get("elevation", 0.0)) - cl.elevation_m) <= elevation_tol_m
            ):
                matches.append({
                    "name": cl.name,
                    "elevation_m": cl.elevation_m,
                    "project_llm_id": pl["llm_id"],
                })
                coupe_remaining.remove(ci)
                project_remaining.remove(pi)
                break

    # Pass 2: name match only (elev mismatch).
    name_only: List[Dict[str, Any]] = []
    for ci in list(coupe_remaining):
        cl = coupe_levels[ci]
        for pi in list(project_remaining):
            pl = project_levels[pi]
            if pl.get("name") == cl.name:
                name_only.append({
                    "name": cl.name,
                    "coupe_elevation_m": cl.elevation_m,
                    "project_elevation_m": float(pl.get("elevation", 0.0)),
                    "project_llm_id": pl["llm_id"],
                    "delta_m": round(
                        cl.elevation_m - float(pl.get("elevation", 0.0)), 4,
                    ),
                })
                coupe_remaining.remove(ci)
                project_remaining.remove(pi)
                break

    # Pass 3: elevation match only (name mismatch).
    elev_only: List[Dict[str, Any]] = []
    for ci in list(coupe_remaining):
        cl = coupe_levels[ci]
        for pi in list(project_remaining):
            pl = project_levels[pi]
            if abs(float(pl.get("elevation", 0.0)) - cl.elevation_m) <= elevation_tol_m:
                elev_only.append({
                    "elevation_m": cl.elevation_m,
                    "coupe_name": cl.name,
                    "project_name": pl.get("name"),
                    "project_llm_id": pl["llm_id"],
                })
                coupe_remaining.remove(ci)
                project_remaining.remove(pi)
                break

    # Remaining.
    missing = [
        {"name": coupe_levels[ci].name, "elevation_m": coupe_levels[ci].elevation_m}
        for ci in coupe_remaining
    ]
    extra = [
        {
            "name": project_levels[pi].get("name"),
            "elevation_m": float(project_levels[pi].get("elevation", 0.0)),
            "project_llm_id": project_levels[pi]["llm_id"],
        }
        for pi in project_remaining
    ]

    # Suggested actions.
    actions: List[Dict[str, Any]] = []
    for m in missing:
        actions.append({
            "action": "create_level",
            "name": m["name"],
            "elevation_m": m["elevation_m"],
            "rationale": "Niveau présent dans la coupe DXF, absent du projet.",
        })
    for n in name_only:
        actions.append({
            "action": "modify_elevation",
            "project_llm_id": n["project_llm_id"],
            "name": n["name"],
            "from_m": n["project_elevation_m"],
            "to_m": n["coupe_elevation_m"],
            "rationale": (
                "Niveau '{}' existe avec une élévation différente "
                "(Δ={:.3f} m). Adapter au DXF ? (confirmation user "
                "requise — peut casser les hôtes existants)".format(
                    n["name"], n["delta_m"],
                )
            ),
        })
    for e in elev_only:
        actions.append({
            "action": "rename_or_keep",
            "project_llm_id": e["project_llm_id"],
            "from_name": e["project_name"],
            "to_name": e["coupe_name"],
            "elevation_m": e["elevation_m"],
            "rationale": (
                "Élévation {} m match mais noms différents : '{}' (KG) "
                "vs '{}' (DXF). Renommer ou garder le nom projet "
                "(souvent intentionnel : RDC vs Niveau 0)."
                .format(e["elevation_m"], e["project_name"], e["coupe_name"])
            ),
        })
    # Pas d'action `delete` automatique sur extra_in_project — listé en
    # info uniquement. Si user veut supprimer, c'est explicite via
    # `levels_delete` (non implémenté V0 d'ailleurs).
    for x in extra:
        actions.append({
            "action": "keep_extra",
            "project_llm_id": x["project_llm_id"],
            "name": x["name"],
            "elevation_m": x["elevation_m"],
            "rationale": (
                "Niveau '{}' présent dans le projet mais absent de la "
                "coupe DXF. Garder par défaut (suppression non automatique)."
                .format(x["name"])
            ),
        })

    return LevelReconciliation(
        matches=matches,
        name_only_matches=name_only,
        elev_only_matches=elev_only,
        missing_in_project=missing,
        extra_in_project=extra,
        suggested_actions=actions,
    )


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
