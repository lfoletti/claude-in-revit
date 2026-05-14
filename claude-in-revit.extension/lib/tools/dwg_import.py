"""tools/dwg_import.py — tools tier-2 pour ingest DWG/DXF (§9 V0 Sem.4-5, UC1).

3 tools chaînables :

1. **`dwg_inspect(file_path)`** — preview : énumère les layers + leur
   `suggested_role` heuristique. L'utilisateur (ou le LLM) confirme le
   mapping avant `dwg_import_walls`.

2. **`dwg_classify(file_path, layer_mapping)`** — preview classified :
   applique le mapping, détecte les paires de lignes parallèles →
   wall segments (avec thickness). Read-only : ne crée rien dans Revit /
   KG. Le LLM peut afficher au user pour validation finale.

3. **`dwg_import_walls(file_path, level_ref, wall_type_ref,
   layer_mapping, dx_m, dy_m, scale_override, max_walls)`** — orchestre
   classify + délègue à `walls_create_many` (qui gère l'atomicité KG+Revit).
   `dx_m` / `dy_m` permettent d'aligner le DXF avec un point d'origine
   Revit ; `scale_override` corrige un `$INSUNITS` manquant.

Tier-2 — chargés via routing keyword `dwg` / `dxf` / `importe` / `plan d'archi`.
Évite de polluer le catalogue par défaut.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import (
    dwg_classifier,
    dwg_coherence,
    dwg_elevation_reader,
    dwg_face_tracing,
    dwg_plan_openings,
    dwg_reader,
    dwg_section_reader,
    dwg_voting,
)
from ..llm_protocol import tool
from ..project_kg import ProjectKG


# ----- 1. Inspect (preview layers) --------------------------------------


@tool(name="dwg_inspect", tier=2)
def inspect(
    kg: ProjectKG,
    file_path: str,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Énumère les layers d'un fichier DXF / DWG et propose un rôle par layer.

    Lit le fichier, applique la conversion d'unités `$INSUNITS`, retourne
    un résumé par layer : nom, couleur, nombre d'entités, distribution
    par kind (LINE, LWPOLYLINE, INSERT, …), et `suggested_role` issu de
    l'heuristique sur le nom (`WALL` / `MUR` → "wall", `DOOR` / `PORTE`
    → "door", etc.).

    DWG nécessite l'ODA File Converter installé (cf. config). DXF est
    lu directement par ezdxf.

    Concepts: dwg, dxf, plan, cad, import, layer, inventaire, inspect
    Phrases: "qu'est-ce qu'il y a dans ce DWG", "liste les layers",
             "inspecte ce plan CAD", "import dxf preview"
    Similar: dwg_classify, dwg_import_walls

    Args:
        file_path: chemin absolu ou relatif du fichier .dxf ou .dwg.
        scale_override: facteur m-per-dxf-unit additionnel (utile si
            $INSUNITS est absent ou faux). Défaut None.

    Returns:
        {"ok": bool, "file": str, "units_code": int | None,
         "units_factor_to_m": float, "total_entities": int,
         "dxf_version": str, "source_format": "dxf" | "dwg",
         "layers": [{"name": str, "entity_count": int,
                     "kinds": {kind: count},
                     "color": int | None,
                     "suggested_role": str | None}, …]}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))

    entities, meta = dwg_reader.parse(path, scale_override=scale_override)
    dwg_classifier.annotate_layers(meta["layers"])

    kind, kind_evidence = dwg_section_reader.classify_dxf(meta["layers"])

    return {
        "ok": True,
        "file": str(path),
        "kind": kind,
        "kind_evidence": kind_evidence,
        "units_code": meta["units_code"],
        "units_factor_to_m": meta["units_factor_to_m"],
        "total_entities": meta["total_entities"],
        "dxf_version": meta["dxf_version"],
        "source_format": meta["source_format"],
        "layers": meta["layers"],
    }


def _refuse_if_section(path: Path) -> None:
    """Raise actionable ValueError si le DXF est une coupe et pas un plan.

    Garde-fou contre le bug runtime 2026-05-13 (session l) : l'agent
    importait les murs de chaque DXF du dossier — y compris les coupes —
    en pensant qu'il s'agissait de plans. Les coupes ont aussi un layer
    `A-WALL` (sections verticales des murs), ce qui produit des « murs »
    bidons offsetés dans le plan Revit + des dépassements géométriques.

    Le check est partagé entre `dwg_classify` et `dwg_import_walls` pour
    couper court dès le preview, pas seulement au commit.
    """
    entities, meta = dwg_reader.parse(path)
    kind, evidence = dwg_section_reader.classify_dxf(meta["layers"])
    if kind == "section":
        raise ValueError(
            "DXF identifié comme COUPE (section), pas plan : {}. "
            "Layers détectés : {}. "
            "Utilise `dwg_inspect_sections` (qui sait lire plans + coupes) "
            "à la place de `dwg_classify` / `dwg_import_walls` pour ce "
            "fichier. Évidence : {}".format(
                path.name,
                [l["name"] for l in meta["layers"]],
                evidence.get("trigger", ""),
            )
        )


# ----- 2. Classify (preview wall candidates) ----------------------------


def _wall_candidate_to_dict(w: dwg_classifier.WallCandidate) -> Dict[str, Any]:
    """Sérialisation compacte d'un WallCandidate pour le tool_result."""
    return {
        "p1": [round(w.p1[0], 4), round(w.p1[1], 4)],
        "p2": [round(w.p2[0], 4), round(w.p2[1], 4)],
        "thickness_m": round(w.thickness, 4),
        "layer": w.layer,
        "confidence": round(w.confidence, 3),
    }


@tool(name="dwg_classify", tier=2)
def classify(
    kg: ProjectKG,
    file_path: str,
    layer_mapping: Dict[str, str],
    scale_override: Optional[float] = None,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.50,
    include_centerline: bool = True,
    centerline_thickness_m: float = 0.10,
    centerline_min_length_m: float = 0.5,
    centerline_max_gap_m: float = 0.20,
) -> Dict[str, Any]:
    """Applique un mapping layer → rôle + détecte les murs (paires parallèles
    + fallback centerline). Read-only — ne crée rien dans Revit / KG.

    Le LLM appelle ce tool *après* `dwg_inspect` pour valider la qualité
    de la détection avant `dwg_import_walls`. Permet d'ajuster
    `layer_mapping` ou les seuils sans engager de mutation.

    **Deux passes** :
    1. **Pair detection** : paires de lignes parallèles dans
       [min_thickness_m, max_thickness_m]. Confidence ~1.0.
    2. **Centerline fallback** (`include_centerline=True` par défaut) :
       sur les segments orphelins après la 1ère passe, fusionne les
       collinéaires (absorbe ouvertures ≤ `centerline_max_gap_m`),
       filtre par longueur min, synthétise des walls avec
       `thickness = centerline_thickness_m`. Confidence 0.6 (inférence
       partielle). Sans ça, les cloisons légères dessinées en
       simple-trait ne sont pas importées.

    Concepts: dwg, dxf, classification, murs, paires, centerline,
              cloison, layer mapping
    Phrases: "preview les murs détectés", "classifie ce DXF",
             "combien de murs trouve-t-on", "essaie d'abord sans créer"
    Similar: dwg_inspect, dwg_import_walls

    Args:
        file_path: chemin du fichier .dxf ou .dwg.
        layer_mapping: `{layer_name: "wall" | "door" | "window" |
            "ignore" | "text"}`. Layers absents ignorés. Seul "wall"
            est traité en V0 phase 1.
        scale_override: voir `dwg_inspect`.
        min_thickness_m: épaisseur min des paires (défaut 0.05 m).
        max_thickness_m: épaisseur max (défaut 0.50 m).
        include_centerline: défaut True. Active la passe centerline
            pour récupérer les cloisons en simple-trait.
        centerline_thickness_m: épaisseur attribuée aux walls
            centerline (défaut 0.10 m — cloison standard FR/CH).
        centerline_min_length_m: longueur min pour qu'un centerline
            devienne mur (défaut 0.5 m — filtre les épaulements de
            fenêtres et autres artefacts courts).
        centerline_max_gap_m: gap max entre fragments collinéaires
            à fusionner (défaut 0.20 m — absorbe les portes
            intérieures qui interrompent une cloison continue).

    Returns:
        {"ok": bool, "walls_count": int, "centerline_walls_count": int,
         "walls": [{"p1", "p2", "thickness_m", "layer", "confidence"}, …],
         "rejected_count": int,
         "rejected_summary": [{"layer", "count", "sample_reason"}, …]}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))

    _refuse_if_section(path)

    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    result = dwg_classifier.classify(
        entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        include_centerline=include_centerline,
        centerline_thickness_m=centerline_thickness_m,
        centerline_min_length_m=centerline_min_length_m,
        centerline_max_gap_m=centerline_max_gap_m,
    )
    walls_dicts = [_wall_candidate_to_dict(w) for w in result.walls]

    # Agrégation des rejets par layer.
    rejected_by_layer: Dict[str, Dict[str, Any]] = {}
    for r in result.rejected:
        bucket = rejected_by_layer.setdefault(
            r["layer"],
            {"layer": r["layer"], "count": 0, "sample_reason": r.get("reason")},
        )
        bucket["count"] += 1

    out: Dict[str, Any] = {
        "ok": True,
        "walls_count": len(walls_dicts),
        "centerline_walls_count": result.centerline_walls_count,
        "rejected_count": len(result.rejected),
        "rejected_summary": list(rejected_by_layer.values()),
    }
    # Le preview liste tous les murs jusqu'à 100 (assez pour la plupart
    # des plans d'archi sans tronquer). Au-delà, on tronque + on rend
    # la note explicite pour éviter la confusion LLM de session h+ qui
    # avait pris "20 of 29" comme "il manque 9" et reconstitué
    # manuellement 9 murs en doublon.
    preview_limit = 100
    if len(walls_dicts) <= preview_limit:
        out["walls"] = walls_dicts
    else:
        out["walls"] = walls_dicts[:preview_limit]
        out["walls_truncated"] = True
        out["note"] = (
            "Preview tronqué à {} sur {} murs détectés (économie tokens). "
            "**IMPORTANT** : `dwg_import_walls` créera la totalité ({}), "
            "PAS seulement les {} affichés ici. Ne reconstitue PAS les "
            "murs manquants manuellement — appelle `dwg_import_walls` qui "
            "porte la classification complète en interne.".format(
                preview_limit, len(walls_dicts), len(walls_dicts), preview_limit,
            )
        )
    return out


# ----- 3. Import walls (commit to Revit + KG) ---------------------------


@tool(name="dwg_import_walls", tier=2)
def import_walls(
    kg: ProjectKG,
    doc: Any,
    file_path: str,
    level_ref: str,
    wall_type_ref: str,
    layer_mapping: Dict[str, str],
    dx_m: float = 0.0,
    dy_m: float = 0.0,
    height_m: Optional[float] = None,
    scale_override: Optional[float] = None,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.50,
    max_walls: int = 500,
    include_centerline: bool = True,
    centerline_thickness_m: float = 0.10,
    centerline_min_length_m: float = 0.5,
    centerline_max_gap_m: float = 0.20,
) -> Dict[str, Any]:
    """Importe les murs d'un fichier DXF / DWG en chaînant classify +
    walls_create_many (atomique KG + Revit).

    Les murs détectés sont translatés de `(dx_m, dy_m)` pour aligner
    l'origine du DXF sur la grille Revit du projet. `scale_override`
    corrige un `$INSUNITS` manquant ou faux. `height_m` impose une
    hauteur uniforme (sinon, hauteur d'étage par défaut comme dans
    `walls_create`).

    **Atomicité** : `walls_create_many` (session b) ouvre une seule Tx
    Revit + une seule Tx KG ; un item invalide → tout rollback. Le
    `bulk_setter`-style summary remonté ici provient directement de
    l'inner tool.

    **Garde-fou `max_walls`** : refus si la classification produit plus
    de N candidats (défaut 500). Évite l'import accidentel d'un layer
    sur-segmenté qui créerait des milliers de murs parasites.

    Concepts: dwg, dxf, import, murs, batch, plan cad, ingest
    Phrases: "importe les murs de ce DWG", "crée les murs depuis ce DXF",
             "ingest plan archi", "dessine les murs du dxf"
    Similar: dwg_classify, walls_create_many

    Args:
        file_path: chemin du fichier .dxf ou .dwg.
        level_ref: llm_id du Level cible.
        wall_type_ref: llm_id du WallType à utiliser pour tous les murs
            créés. (V0 phase 1 : type unique ; phases ultérieures
            mapperont thickness → type.)
        layer_mapping: dict layer → rôle. Cf. `dwg_classify`.
        dx_m: translation X en mètres (défaut 0).
        dy_m: translation Y en mètres (défaut 0).
        height_m: hauteur uniforme en mètres. Défaut None = hauteur
            d'étage déduite côté `walls_create_many`.
        scale_override: facteur additionnel sur `$INSUNITS`.
        min_thickness_m / max_thickness_m: bornes de détection paires.
        max_walls: refus du batch si nombre de candidats > N (défaut 500).

    Returns:
        Réponse compacte mêlant info import + summary walls_create_many :
        {"ok", "walls_imported": int, "rejected_count": int,
         "thickness_distribution": {value: count, …},
         "inner": <walls_create_many response>}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    _refuse_if_section(path)
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if not kg.has_node(wall_type_ref):
        raise ValueError("Unknown wall_type_ref: {}".format(wall_type_ref))

    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    result = dwg_classifier.classify(
        entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        include_centerline=include_centerline,
        centerline_thickness_m=centerline_thickness_m,
        centerline_min_length_m=centerline_min_length_m,
        centerline_max_gap_m=centerline_max_gap_m,
    )
    if len(result.walls) > max_walls:
        raise ValueError(
            "DWG classify produced {} wall candidates (> max_walls={}). "
            "Refine layer_mapping or raise max_walls explicitly if "
            "intentional.".format(len(result.walls), max_walls),
        )
    if not result.walls:
        return {
            "ok": True,
            "walls_imported": 0,
            "rejected_count": len(result.rejected),
            "thickness_distribution": {},
            "inner": None,
            "note": "No wall candidates detected from this layer_mapping.",
        }

    # Construit les items pour walls_create_many. Translation dx/dy
    # appliquée ici, pas dans le classifier (qui reste pur).
    items: List[Dict[str, Any]] = []
    for w in result.walls:
        item: Dict[str, Any] = {
            "level_ref": level_ref,
            "wall_type_ref": wall_type_ref,
            "p1": [w.p1[0] + dx_m, w.p1[1] + dy_m],
            "p2": [w.p2[0] + dx_m, w.p2[1] + dy_m],
        }
        if height_m is not None:
            item["height"] = float(height_m)
        items.append(item)

    # Dispatch direct (pas via dispatch_tool_use — pour la même raison
    # que bulk_apply_to_filter : éviter nested kg.transaction).
    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    entry = registry.get("walls_create_many")
    if entry is None:
        raise RuntimeError(
            "walls_create_many not registered — registry corrupted?"
        )
    inner = entry.fn(kg=kg, doc=doc, items=items)

    # Distribution des épaisseurs détectées — utile au LLM pour reporter
    # à l'utilisateur (porteurs vs cloisons typiquement).
    thickness_dist: Dict[str, int] = {}
    for w in result.walls:
        bucket = "{:.2f}m".format(round(w.thickness, 2))
        thickness_dist[bucket] = thickness_dist.get(bucket, 0) + 1

    return {
        "ok": True,
        "walls_imported": len(result.walls),
        "rejected_count": len(result.rejected),
        "thickness_distribution": thickness_dist,
        "inner": inner,
    }


# ----- 4. Inspect sections (preview plan + coupes) ----------------------


def _level_to_dict(lv: dwg_section_reader.Level) -> Dict[str, Any]:
    return {
        "name": lv.name,
        "elevation_m": round(lv.elevation_m, 4),
        "y_dxf_m": round(lv.y_dxf_m, 4),
        "line_x_range_m": [round(lv.line_x_range_m[0], 4), round(lv.line_x_range_m[1], 4)],
        "source": lv.source,
    }


def _opening_to_dict(o: dwg_section_reader.SectionOpening) -> Dict[str, Any]:
    return {
        "block_id": o.block_id,
        "x_dxf_m": round(o.x_dxf_m, 4),
        "y_dxf_m": round(o.y_dxf_m, 4),
        "rotation_deg": round(o.rotation_deg, 2),
        "width_m": o.width_m,
        "height_m": o.height_m,
    }


@tool(name="dwg_inspect_sections", tier=2)
def inspect_sections(
    kg: ProjectKG,
    file_paths: Optional[List[str]] = None,
    directory: Optional[str] = None,
    scale_override: Optional[float] = None,
    opening_preview_limit: int = 30,
) -> Dict[str, Any]:
    """Inspecte un plan + N coupes DXF. Read-only, sort un rapport JSON.

    Pipeline du chapitre coupes (UC1 Phase 4, voir JOURNAL 2026-05-12 note
    d'intention + entrée 2026-05-13 inventaire Projet4) :

    1. Parse chaque fichier (ou tous les `.dxf` du dossier `directory`).
    2. `classify_dxf` → `plan` | `section` | `unknown`.
    3. Pour les sections : extrait niveaux (layer `A-FLOR-LEVL`) +
       ouvertures (INSERTs sur `A-GLAZ`).
    4. Pour le plan : extrait les ouvertures (INSERTs sur `A-GLAZ`).
    5. Pour chaque section, calcule le matching ouvertures coupe↔plan
       via `block_id` partagé (l'ID Revit inscrit dans le nom de bloc,
       partagé entre `... -258141-Niveau 0` et `... -258141-Coupe 1`).

    Le caller (LLM ou user) utilise ensuite ce rapport pour décider :
    - lesquelles des coupes utiliser pour quel pan du plan (géo-ref via
      pointage user, à venir) ;
    - quels niveaux créer (via `levels_create_many`) ;
    - quelles hauteurs sill/head appliquer aux ouvertures du plan.

    Aucun écrit Revit ou KG ici. Le tool est sûr à appeler plusieurs fois.

    Concepts: dwg, dxf, coupe, section, niveau, level, elevation,
              fenêtre, opening, glazing, plan, géo-ref, inspect, audit,
              projet, dossier, import projet
    Phrases: "importe ce projet", "inspecte les coupes",
             "qu'y a-t-il dans ce dossier projet",
             "extrait les niveaux", "match les fenêtres entre plan et coupe",
             "préview des coupes du projet", "analyse plan + coupes DXF"
    Similar: dwg_inspect, dwg_classify, levels_create_many

    Args:
        file_paths: liste explicite de chemins .dxf. Optionnel si
            `directory` est fourni.
        directory: chemin d'un dossier contenant les `.dxf` du projet
            (plan + coupes). Le tool glob `*.dxf` et trie par nom.
            Use case canonique : « importe ce projet » + chemin dossier.
            Optionnel si `file_paths` est fourni. Exactement l'un des
            deux doit être renseigné.
        scale_override: voir `dwg_inspect`. Appliqué à tous les fichiers.
        opening_preview_limit: nombre max d'ouvertures listées par
            fichier dans le preview (défaut 30). Au-delà, agrégation
            par block_id.

    Returns:
        {"ok": bool, "files": [{...}, ...],
         "section_to_plan_matches": [{section_path, match_count,
                                       unmatched_section_count,
                                       distinct_block_ids: [...]}, ...]}
    """
    # Résolution des entrées : exactement l'une des deux options.
    if directory is not None and file_paths:
        raise ValueError(
            "Provide either `directory` or `file_paths`, not both."
        )
    if directory is not None:
        dir_path = Path(directory)
        if not dir_path.exists() or not dir_path.is_dir():
            raise FileNotFoundError(
                "Directory not found: {}".format(dir_path)
            )
        dxf_files = sorted(dir_path.glob("*.dxf"))
        if not dxf_files:
            raise ValueError(
                "No .dxf files in directory {}".format(dir_path)
            )
        file_paths = [str(p) for p in dxf_files]
    elif not file_paths:
        raise ValueError(
            "Provide either `directory` or `file_paths` (non-empty list)."
        )

    parsed: List[Dict[str, Any]] = []
    plan_index: Optional[int] = None

    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError("File not found: {}".format(path))

        entities, meta = dwg_reader.parse(path, scale_override=scale_override)
        kind, evidence = dwg_section_reader.classify_dxf(
            meta["layers"], file_name=path.name,
        )

        file_record: Dict[str, Any] = {
            "path": str(path),
            "name": path.name,
            "kind": kind,
            "kind_evidence": evidence,
            "units_factor_to_m": meta["units_factor_to_m"],
            "total_entities": meta["total_entities"],
        }

        if kind == "elevation":
            # Elevations partagent la signature des sections (A-FLOR-LEVL +
            # A-GLAZ + A-WALL). Extraction identique. La direction
            # (Est/Nord/Sud/Ouest) est dans `evidence["direction"]`.
            levels = dwg_section_reader.read_levels(entities)
            openings = dwg_section_reader.read_section_openings(entities)
            file_record["levels"] = [_level_to_dict(lv) for lv in levels]
            file_record["openings_count"] = len(openings)
            file_record["openings_with_id_count"] = sum(
                1 for o in openings if o.block_id is not None
            )
            file_record["openings"] = [
                _opening_to_dict(o) for o in openings[:opening_preview_limit]
            ]
            if len(openings) > opening_preview_limit:
                file_record["openings_truncated"] = True
            by_id: Dict[str, int] = {}
            for o in openings:
                key = o.block_id or "<unknown>"
                by_id[key] = by_id.get(key, 0) + 1
            file_record["openings_by_block_id"] = by_id
            file_record["direction"] = evidence.get("direction")

        if kind == "section":
            levels = dwg_section_reader.read_levels(entities)
            openings = dwg_section_reader.read_section_openings(entities)
            file_record["levels"] = [_level_to_dict(lv) for lv in levels]
            file_record["openings_count"] = len(openings)
            file_record["openings_with_id_count"] = sum(
                1 for o in openings if o.block_id is not None
            )
            file_record["openings"] = [
                _opening_to_dict(o) for o in openings[:opening_preview_limit]
            ]
            if len(openings) > opening_preview_limit:
                file_record["openings_truncated"] = True
            # Aggregate by block_id for compact view.
            by_id: Dict[str, int] = {}
            for o in openings:
                key = o.block_id or "<unknown>"
                by_id[key] = by_id.get(key, 0) + 1
            file_record["openings_by_block_id"] = by_id
        elif kind == "plan":
            openings = dwg_section_reader.read_section_openings(entities)
            file_record["openings_count"] = len(openings)
            file_record["openings_with_id_count"] = sum(
                1 for o in openings if o.block_id is not None
            )
            file_record["openings"] = [
                _opening_to_dict(o) for o in openings[:opening_preview_limit]
            ]
            if len(openings) > opening_preview_limit:
                file_record["openings_truncated"] = True
            by_id: Dict[str, int] = {}
            for o in openings:
                key = o.block_id or "<unknown>"
                by_id[key] = by_id.get(key, 0) + 1
            file_record["openings_by_block_id"] = by_id
            if plan_index is None:
                plan_index = len(parsed)
            # Stocke les openings pour le matching post-loop.
            file_record["_openings_internal"] = openings
        else:
            file_record["note"] = (
                "Unknown DXF type — neither A-FLOR-LEVL nor A-AREA-IDEN "
                "signature found. Layers: {}".format(
                    evidence.get("available_layers", []),
                )
            )

        if kind == "section":
            file_record["_openings_internal"] = openings

        parsed.append(file_record)

    # Matching section ↔ plan.
    section_to_plan_matches: List[Dict[str, Any]] = []
    if plan_index is not None:
        plan_openings = parsed[plan_index].get("_openings_internal", [])
        for i, rec in enumerate(parsed):
            if rec.get("kind") != "section":
                continue
            sec_openings = rec.get("_openings_internal", [])
            matches, unmatched_sec, unmatched_plan = (
                dwg_section_reader.match_openings(plan_openings, sec_openings)
            )
            distinct_ids = sorted({m.block_id for m in matches})
            section_to_plan_matches.append({
                "section_path": rec["path"],
                "section_name": rec["name"],
                "match_count": len(matches),
                "unmatched_section_count": len(unmatched_sec),
                "unmatched_plan_count": len(unmatched_plan),
                "distinct_block_ids": distinct_ids,
            })

    # **Cleanup _openings_internal de TOUS les records** (pas seulement
    # plan_index + sections). Fix runtime 2026-05-13 : avec plusieurs
    # plans (N0 + N1), seul le 1er voyait son `_openings_internal`
    # poppé → JSON serialization crash sur les `SectionOpening` du 2e
    # plan.
    for rec in parsed:
        rec.pop("_openings_internal", None)

    return {
        "ok": True,
        "files": parsed,
        "section_to_plan_matches": section_to_plan_matches,
        "note": (
            "Read-only inspection. To actually create Revit elements, use "
            "levels_create_many for niveaux, then dwg_import_walls for the "
            "plan walls. Geo-référencement plan↔coupe (mapping x_coupe → "
            "axe_plan) reste à demander à l'utilisateur."
        ),
    }


# ----- 5. Find section markers (Phase 1 Étape 2) ------------------------


def _section_marker_to_dict(m: dwg_section_reader.SectionMarker) -> Dict[str, Any]:
    return {
        "kind": m.kind,
        "p1_m": list(m.p1_m),
        "p2_m": list(m.p2_m),
        "length_m": round(m.length_m, 4),
        "is_vertical": m.is_vertical,
        "is_horizontal": m.is_horizontal,
        "inferred_view_dir": m.inferred_view_dir,
        "view_dir_candidates": list(m.view_dir_candidates),
        "associated_blocks": list(m.associated_blocks),
        "source_layer": m.source_layer,
    }


@tool(name="dwg_find_section_markers", tier=2)
def find_section_markers(
    kg: ProjectKG,
    file_path: str,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Détecte les traits de coupe (et marqueurs d'élévation) dans un plan DXF.

    Étape 2 de la Phase 1 import projet (cf. spec 2026-05-13). Sort un
    rapport ordonné par longueur décroissante. Le caller (LLM ou user)
    confirme ensuite le mapping coupe ↔ trait, puis appelle
    `dxf_context_register_section_line` pour persister.

    **Algo** : layers `G-ANNO-SYMB` / `G-ANNO-SECT` (Revit AIA export) ;
    LINEs longues + INSERTs aux extrémités. Le nom du bloc INSERT
    distingue coupe (`Coupe - ...`) vs élévation (`Elévation - ...`).
    Les vraies coupes sont les premières dans la liste retournée
    (`kind == "section"`).

    **Ambiguïté direction de vue** : chaque trait expose 2 candidats
    (`view_dir_candidates`) car la rotation du marqueur n'identifie
    pas universellement le sens — l'agent demande à l'user.

    Concepts: dxf, plan, coupe, section, trait de coupe, marker, géo-ref,
              annotation, G-ANNO, élévation, phase 1
    Phrases: "trouve les traits de coupe", "où sont les coupes dans le plan",
             "détecte les marqueurs d'élévation", "section markers"
    Similar: dwg_inspect_sections, dxf_context_register_section_line,
             dwg_verify_section_scale

    Args:
        file_path: chemin du DXF plan (doit être kind='plan' selon
            classify_dxf — pas de vérification stricte ici, le tool
            détecte les markers même sur un DXF unknown).
        scale_override: facteur supplémentaire $INSUNITS si nécessaire.

    Returns:
        {"ok": bool, "file": str, "section_count": int,
         "elevation_count": int, "markers": [{...}, ...]}
        Chaque marker : kind, p1_m, p2_m, length_m, is_vertical,
        is_horizontal, view_dir_candidates, associated_blocks,
        source_layer.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))

    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    markers = dwg_section_reader.find_section_markers(entities)

    markers_dict = [_section_marker_to_dict(m) for m in markers]
    section_count = sum(1 for m in markers if m.kind == "section")
    elevation_count = sum(1 for m in markers if m.kind == "elevation")

    # Aggrégation : pour chaque trait de coupe (section), est-ce que
    # son `inferred_view_dir` est non-None ? Si oui, l'agent peut
    # procéder directement avec cette direction. Si non, demander à
    # l'user pour le marker concerné uniquement.
    section_markers_idx = [
        i for i, m in enumerate(markers_dict) if m["kind"] == "section"
    ]
    needs_user_for_view_dir = [
        i for i in section_markers_idx
        if markers_dict[i]["inferred_view_dir"] is None
    ]
    all_inferred_confidently = (
        section_count > 0 and not needs_user_for_view_dir
    )

    if section_count == 0:
        note = (
            "Aucun trait de coupe détecté. Vérifier que le DXF a été "
            "exporté avec les annotations Coupes visibles (Revit : VG → "
            "Annotations → Coupes coché + configuration export DXF "
            "mapping correct)."
        )
    elif all_inferred_confidently:
        note = (
            "Toutes les directions de regard ont été inférées avec "
            "confiance depuis la rotation des marqueurs (signal clair "
            "dans le DXF). **PROCÉDER DIRECTEMENT** avec "
            "`dxf_context_register_section_line` sans demander "
            "confirmation user — les directions du dessin sont "
            "suffisantes. Ne demander à l'user que si une action "
            "ultérieure est destructrice ou ambiguë."
        )
    else:
        note = (
            "Inférence partielle : {n_amb}/{n_tot} traits ont "
            "`inferred_view_dir=None` (rotation oblique, marqueur "
            "manquant, ou conflit). Demander à l'user UNIQUEMENT pour "
            "les indices {idxs} via `ui_confirm_choices` ; pour les "
            "autres, procéder direct avec `inferred_view_dir`."
        ).format(
            n_amb=len(needs_user_for_view_dir),
            n_tot=section_count,
            idxs=needs_user_for_view_dir,
        )

    return {
        "ok": True,
        "file": str(path),
        "section_count": section_count,
        "elevation_count": elevation_count,
        "all_inferred_confidently": all_inferred_confidently,
        "needs_user_for_view_dir": needs_user_for_view_dir,
        "markers": markers_dict,
        "note": note,
    }


# ----- 5b. Identify source convention (Phase 1 Étape 4) -----------------


@tool(name="dwg_identify_source", tier=2)
def identify_source(
    kg: ProjectKG,
    file_path: str,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Identifie la convention de nommage des layers d'un DXF.

    Étape 4 de la Phase 1 import projet. 2 conventions supportées V0 :

    - **AIA** (American Institute of Architects, US standard) — c'est
      ce que Revit exporte par défaut. Format `<discipline>-<group>-
      <modifier>`, ex `A-WALL`, `G-ANNO-SYMB`.
    - **ISO 13567** (International standard) — codes alphanumériques
      courts, ex `A23G---N1`.
    - **other** — fallback (conventions locales, ArchiCAD français,
      BS1192, etc.). À enrichir plus tard si une 3e source devient
      pertinente.

    Le résultat permet ensuite à l'agent d'appliquer le bon dictionnaire
    de mapping aux étapes 2, 5, etc. (find_section_markers utilise
    actuellement `G-ANNO-SYMB`/`G-ANNO-SECT` qui sont AIA — pour ISO il
    faudra adapter).

    Concepts: dxf, source, nomenclature, convention, AIA, ISO, layer,
              identification, phase 1, mapping
    Phrases: "quelle convention de calques", "identify the layer source",
             "AIA ou ISO ?", "détecte la source d'export"
    Similar: dwg_inspect_sections, dwg_inspect, dwg_find_section_markers

    Args:
        file_path: chemin du DXF à analyser.
        scale_override: voir `dwg_inspect`.

    Returns:
        {"ok": bool, "file": str, "source": str, "confidence": float,
         "evidence": {aia_count, iso_count, language_count, samples, ...}}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    _, meta = dwg_reader.parse(path, scale_override=scale_override)
    detection = dwg_section_reader.identify_source(meta["layers"])
    return {
        "ok": True,
        "file": str(path),
        "source": detection["source"],
        "confidence": detection["confidence"],
        "evidence": detection["evidence"],
    }


# ----- 6. Verify section scale (Phase 1 Étape 3) ------------------------


@tool(name="dwg_verify_section_scale", tier=2)
def verify_section_scale(
    kg: ProjectKG,
    section_line_length_m: float,
    coupe_path: str,
    plan_path: Optional[str] = None,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Vérifie la cohérence d'échelle entre un trait de coupe en plan et le
    DXF de coupe correspondant (Étape 3 Phase 1).

    Sanity check, pas géo-ref exhaustive. Compare :

    1. **Cohérence d'unités** : `$INSUNITS` du plan et de la coupe. Si
       discordance (plan mm, coupe feet, etc.) → warning.
    2. **Extension géométrique** : range des coordonnées de la coupe
       (calculée sur A-WALL ou bbox global) vs longueur du trait dans
       le plan. Ratio > 50% drift → warning, > 100% → erreur.

    Le tool ne touche pas au KG ; le caller persiste éventuellement
    `drift_pct` via `dxf_context_register_section_line(... scale_verified=
    True, drift_pct=X)`.

    **Limitation V0** : la coupe DXF inclut souvent du contexte
    hors-bâtiment (terrain, sky, légendes, cartouche), donc le bbox
    A-WALL surévalue la largeur réelle. Le drift attendu reste élevé
    sur des coupes « complètes » — l'agent doit interpréter les
    seuils comme indicatifs, pas comme un échec strict.

    Concepts: dxf, coupe, échelle, scale, géo-ref, vérification,
              cohérence, drift, phase 1, $INSUNITS
    Phrases: "vérifie l'échelle entre plan et coupe", "scale check",
             "is the section line at the right scale",
             "drift entre plan et coupe"
    Similar: dwg_find_section_markers, dxf_context_register_section_line,
             dwg_inspect_sections

    Args:
        section_line_length_m: longueur du trait de coupe dans le plan,
            en mètres. Typiquement obtenu de `dwg_find_section_markers`.
        coupe_path: chemin du DXF coupe.
        plan_path: chemin du DXF plan, optionnel. Si fourni, le tool
            vérifie aussi la cohérence d'unités plan ↔ coupe.
        scale_override: voir `dwg_inspect`.

    Returns:
        {"ok": bool, "scale_match": bool, "warning": str | None,
         "section_line_length_m": float, "coupe_a_wall_extent_m": float,
         "drift_pct": float, "drift_m": float, "units_consistent": bool,
         "plan_units_factor": float | None, "coupe_units_factor": float}
    """
    coupe_path_obj = Path(coupe_path)
    if not coupe_path_obj.exists():
        raise FileNotFoundError("Coupe file not found: {}".format(coupe_path_obj))
    coupe_entities, coupe_meta = dwg_reader.parse(
        coupe_path_obj, scale_override=scale_override,
    )

    plan_units_factor: Optional[float] = None
    units_consistent = True
    if plan_path is not None:
        plan_path_obj = Path(plan_path)
        if not plan_path_obj.exists():
            raise FileNotFoundError("Plan file not found: {}".format(plan_path_obj))
        _, plan_meta = dwg_reader.parse(plan_path_obj, scale_override=scale_override)
        plan_units_factor = plan_meta["units_factor_to_m"]
        coupe_units_factor = coupe_meta["units_factor_to_m"]
        units_consistent = abs(plan_units_factor - coupe_units_factor) < 1e-6
    else:
        coupe_units_factor = coupe_meta["units_factor_to_m"]

    # Coupe building X extent (in metres post-conversion). A-WALL ∪
    # A-FLOR LINEs : robuste aux coupes sans mur (poteaux-dalles, P2).
    coupe_extent_m = _building_extent_from_entities(coupe_entities)
    if coupe_extent_m is None:
        return {
            "ok": False,
            "scale_match": False,
            "warning": (
                "Coupe contient aucune LINE A-WALL ou A-FLOR — extent "
                "géométrique non calculable."
            ),
            "section_line_length_m": float(section_line_length_m),
            "coupe_a_wall_extent_m": 0.0,
            "drift_pct": float("inf"),
            "drift_m": 0.0,
            "units_consistent": units_consistent,
            "plan_units_factor": plan_units_factor,
            "coupe_units_factor": coupe_units_factor,
        }
    sl_len = float(section_line_length_m)
    drift_m = abs(sl_len - coupe_extent_m)
    # drift_pct relatif à la valeur max (plus stable que relativement à
    # un côté arbitraire).
    denom = max(sl_len, coupe_extent_m, 1e-6)
    drift_pct = 100.0 * drift_m / denom

    # Tolérance pragmatique : la coupe inclut souvent du contexte
    # hors-bâtiment, donc on accepte un drift jusqu'à 50% comme
    # "scale_match" mais avec warning au-delà de 25%.
    scale_match = units_consistent and drift_pct < 50.0
    warning: Optional[str] = None
    if not units_consistent:
        warning = (
            "Unit mismatch: plan factor {} vs coupe factor {} — un des "
            "deux DXF a un $INSUNITS différent. Re-export avec mêmes "
            "unités recommandé.".format(plan_units_factor, coupe_units_factor)
        )
    elif drift_pct >= 50.0:
        warning = (
            "Drift {:.1f}% entre trait de coupe ({:.2f}m) et extent A-WALL "
            "de la coupe ({:.2f}m). Possibles causes : (1) mauvais mapping "
            "trait↔coupe ; (2) coupe inclut un contexte étendu hors-"
            "bâtiment. Vérifier visuellement avant d'enchaîner."
            .format(drift_pct, sl_len, coupe_extent_m)
        )
    elif drift_pct >= 25.0:
        warning = (
            "Drift {:.1f}% notable mais tolérable (la coupe inclut souvent "
            "du contexte hors-bâtiment). Acceptable si le mapping trait↔"
            "coupe a été confirmé par l'utilisateur."
            .format(drift_pct)
        )

    return {
        "ok": True,
        "scale_match": scale_match,
        "warning": warning,
        "section_line_length_m": round(sl_len, 4),
        "coupe_a_wall_extent_m": round(coupe_extent_m, 4),
        "drift_pct": round(drift_pct, 2),
        "drift_m": round(drift_m, 4),
        "units_consistent": units_consistent,
        "plan_units_factor": plan_units_factor,
        "coupe_units_factor": coupe_units_factor,
    }


# ----- 7. Assign coupes to traits (Phase 1 Étape 2.5 — fix swap) -------


# Layers utilisés comme proxy de l'étendue X d'un DXF coupe (= largeur
# du bâtiment vue en coupe). Mapping `layer → allowed entity kinds` :
# - `A-WALL` (LINE) : faces des murs coupés. Base historique.
# - `A-FLOR` (LINE) : faces des dalles. Requis pour les projets dominés
#   par les dalles où une coupe peut ne traverser aucun mur (cas P2
#   Coupe 3 — poteaux + dalles uniquement).
# - `S-COLS` (LINE + INSERT) : poteaux structurels. En coupe, un poteau
#   peut être (a) un rectangle 4-LINEs, (b) un bloc INSERT à son point
#   d'insertion. Inclus pour les projets poteaux-dominants où les
#   colonnes étendent l'enveloppe au-delà de la dalle (P2 Coupe 3 :
#   poteaux à 28m, dalle à 16m).
#
# Exclus : annotations (`G-ANNO-*`, `A-FLOR-LEVL`, `S-COLS-IDEN`,
# `S-GRID`, etc.) qui peuvent dépasser largement le contour réel. Match
# exact sur le nom de layer → `S-COLS-IDEN` ne matche pas `S-COLS`.
#
# `S-BEAM` (poutres) sera ajouté quand on aura un projet fixture pour
# valider le comportement — pour P2/P7 c'est neutre.
_BUILDING_EXTENT_LAYERS: Dict[str, Tuple[str, ...]] = {
    "A-WALL": ("LINE",),
    "A-FLOR": ("LINE",),
    "S-COLS": ("LINE", "INSERT"),
}


def _building_extent_from_entities(entities: List[Any]) -> Optional[float]:
    """X extent (max - min) des entités structurelles d'un DXF coupe,
    en mètres post-conversion. Pour chaque layer dans `_BUILDING_EXTENT_
    LAYERS`, ne prend que les `kind` autorisés (cf. mapping). Retourne
    None si aucune entité pertinente.
    """
    xs: List[float] = []
    for e in entities:
        allowed_kinds = _BUILDING_EXTENT_LAYERS.get(e.layer)
        if not allowed_kinds:
            continue
        if e.kind not in allowed_kinds:
            continue
        for pt in e.coords:
            xs.append(pt[0])
    if not xs:
        return None
    return max(xs) - min(xs)


def _coupe_building_extent_m(path: Path) -> Optional[float]:
    """Helper : retourne l'étendue X du contour bâtiment d'un DXF coupe
    (A-WALL ∪ A-FLOR LINEs), en mètres. None si le fichier n'a aucune
    LINE pertinente.
    """
    entities, _ = dwg_reader.parse(path)
    return _building_extent_from_entities(entities)


@tool(name="dxf_assign_coupes_to_traits", tier=2)
def assign_coupes_to_traits(
    kg: ProjectKG,
    coupe_paths: List[str],
    section_markers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Trouve l'assignment optimal coupe_dxf ↔ section_marker par minimum
    drift total (brute force pour N ≤ 4).

    Use case : après `dwg_find_section_markers`, l'agent a N traits de
    coupe et N fichiers DXF coupe. L'ordre des markers (trié par longueur)
    ne correspond PAS forcément à l'ordre des fichiers (par nom). Sans
    ce tool, l'agent risque de swapper.

    Algo : pour chaque permutation P des traits, calcule la somme des
    drifts (drift = |coupe_extent - trait_length|). L'assignment optimal
    minimise cette somme totale.

    Concepts: dxf, coupe, assignment, match, trait, swap, optimal,
              drift, phase 1
    Phrases: "assigne chaque coupe à son trait", "match dxf coupe au
             bon trait", "optimal section assignment"
    Similar: dwg_find_section_markers, dwg_verify_section_scale,
             dxf_context_register_section_line

    Args:
        coupe_paths: liste de chemins DXF coupe (en général 2-4).
        section_markers: liste de dicts {p1_m, p2_m, length_m, ...} comme
            retournée par `dwg_find_section_markers.markers` filtrée
            sur `kind=='section'`.

    Returns:
        {"ok": bool, "assignment": [{coupe_path, marker_index, drift_m,
            length_m, extent_m}, ...], "total_drift_m": float,
         "alternative_swaps_drift_m": list}
        L'`assignment` est la liste finale optimale. `alternative_swaps_
        drift_m` liste les drifts des autres permutations pour info.
    """
    if not coupe_paths:
        raise ValueError("coupe_paths must be non-empty")
    if not section_markers:
        raise ValueError("section_markers must be non-empty")
    if len(coupe_paths) != len(section_markers):
        raise ValueError(
            "coupe_paths ({}) and section_markers ({}) must have same length"
            .format(len(coupe_paths), len(section_markers))
        )

    # Précalcul des A-WALL extents pour chaque coupe.
    coupe_extents: List[Tuple[str, float]] = []
    for p in coupe_paths:
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError("Coupe not found: {}".format(path))
        ext = _coupe_building_extent_m(path)
        if ext is None:
            raise ValueError(
                "Coupe {} has no A-WALL or A-FLOR LINEs (rien à mesurer "
                "comme contour bâtiment)".format(path.name)
            )
        coupe_extents.append((str(path), ext))

    # Précalcul des longueurs de section markers.
    marker_lengths: List[float] = [
        float(m.get("length_m") or 0.0) for m in section_markers
    ]

    # Brute force toutes les permutations.
    import itertools
    best_perm: Optional[Tuple[int, ...]] = None
    best_total_drift = float("inf")
    perm_drifts: List[Dict[str, Any]] = []
    for perm in itertools.permutations(range(len(section_markers))):
        total = 0.0
        per_pair: List[float] = []
        for ci, mi in enumerate(perm):
            drift = abs(coupe_extents[ci][1] - marker_lengths[mi])
            total += drift
            per_pair.append(round(drift, 4))
        perm_drifts.append({"perm": list(perm), "total_drift_m": round(total, 4)})
        if total < best_total_drift:
            best_total_drift = total
            best_perm = perm

    assert best_perm is not None  # at least one perm exists

    assignment: List[Dict[str, Any]] = []
    for ci, mi in enumerate(best_perm):
        coupe_path, extent = coupe_extents[ci]
        marker = section_markers[mi]
        drift = abs(extent - marker_lengths[mi])
        assignment.append({
            "coupe_path": coupe_path,
            "coupe_name": Path(coupe_path).name,
            "marker_index": mi,
            "marker_length_m": round(marker_lengths[mi], 4),
            "coupe_extent_m": round(extent, 4),
            "drift_m": round(drift, 4),
            "drift_pct": round(
                100.0 * drift / max(marker_lengths[mi], extent, 1e-6), 2
            ),
        })

    return {
        "ok": True,
        "assignment": assignment,
        "total_drift_m": round(best_total_drift, 4),
        "alternative_swaps_drift_m": sorted(
            [p["total_drift_m"] for p in perm_drifts]
        ),
        "note": (
            "Assignment optimal par minimum drift total. Si l'ordre est "
            "**différent** de l'ordre des `coupe_paths` (ex: coupe_paths[0] "
            "→ marker_index=1), c'est un swap évité. L'agent doit "
            "utiliser ce mapping pour `dxf_context_register_section_line` "
            "et `views_link_cad`, pas l'ordre alphabétique des fichiers."
        ),
    }


# ----- 8. Reconcile plan ↔ coupes walls (Phase 2 Étape 1) ----------------


def _section_wall_to_dict(sw: dwg_section_reader.SectionWall) -> Dict[str, Any]:
    return {
        "x_cut_m": round(sw.x_cut_m, 4),
        "thickness_m": round(sw.thickness_m, 4),
        "y_bottom_m": round(sw.y_bottom_m, 4),
        "y_top_m": round(sw.y_top_m, 4),
        "layer": sw.layer,
        "confidence": round(sw.confidence, 3),
    }


def _match_to_dict(m: dwg_coherence.WallSectionMatch) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "plan_wall_index": m.plan_wall_index,
        "plan_thickness_m": round(m.plan_thickness_m, 4),
        "section_line_index": m.section_line_index,
        "section_line_name": m.section_line_name,
        "coupe_path": m.coupe_path,
        "x_cut_expected_m": m.x_cut_expected_m,
        "status": m.status,
    }
    if m.section_wall_index is not None:
        out["section_wall_index"] = m.section_wall_index
    if m.section_thickness_m is not None:
        out["section_thickness_m"] = round(m.section_thickness_m, 4)
    if m.thickness_drift_m is not None:
        out["thickness_drift_m"] = m.thickness_drift_m
    if m.candidate_indices:
        out["candidate_indices"] = m.candidate_indices
    return out


def _section_slab_to_dict(s: dwg_section_reader.SectionFloorSlab) -> Dict[str, Any]:
    return {
        "top_y_m": round(s.top_y_m, 4),
        "bot_y_m": round(s.bot_y_m, 4),
        "thickness_m": round(s.thickness_m, 4),
        "x_min_m": round(s.x_min_m, 4),
        "x_max_m": round(s.x_max_m, 4),
    }


def _floor_match_to_dict(m: dwg_coherence.FloorSectionMatch) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "plan_floor_index": m.plan_floor_index,
        "plan_level_elevation_m": m.plan_level_elevation_m,
        "plan_thickness_m": m.plan_thickness_m,
        "section_line_index": m.section_line_index,
        "section_line_name": m.section_line_name,
        "coupe_path": m.coupe_path,
        "cut_interval_m": list(m.cut_interval_m),
        "status": m.status,
    }
    if m.section_slab_index is not None:
        out["section_slab_index"] = m.section_slab_index
    if m.section_top_y_m is not None:
        out["section_top_y_m"] = m.section_top_y_m
    if m.section_thickness_m is not None:
        out["section_thickness_m"] = m.section_thickness_m
    if m.thickness_drift_m is not None:
        out["thickness_drift_m"] = m.thickness_drift_m
    if m.coverage_ratio is not None:
        out["coverage_ratio"] = m.coverage_ratio
    return out


def _collect_plan_floors_from_kg(kg: ProjectKG) -> List[Dict[str, Any]]:
    """Sérialise les Floor vivants du KG en dicts consommables par
    ``dwg_coherence.reconcile_plan_section_floors`` : résout
    ``level_ref → elevation`` et ``type_ref → total_thickness``.

    Les Floor sans boundary/level/type valides sont skippés silencieusement
    (cas dégénéré — ne devrait pas exister après dwg_create_floors_many).
    """
    plan_floors: List[Dict[str, Any]] = []
    for fid in sorted(kg.find_by_type("Floor")):
        f = kg.get_node(fid)
        if f.get("deleted_at_turn") is not None:
            continue
        boundary = f.get("boundary") or []
        level_ref = f.get("level_ref")
        type_ref = f.get("type_ref")
        if not boundary or not level_ref or not type_ref:
            continue
        try:
            lvl = kg.get_node(level_ref)
            ft = kg.get_node(type_ref)
        except KeyError:
            continue
        plan_floors.append({
            "llm_id": fid,
            "boundary": [list(p) for p in boundary],
            "holes": [[list(p) for p in h] for h in (f.get("holes") or [])],
            "elevation_m": float(lvl.get("elevation", 0.0)),
            "thickness_m": float(ft.get("total_thickness", 0.0)),
            "level_name": lvl.get("name"),
            "type_name": ft.get("name"),
        })
    return plan_floors


@tool(name="dwg_reconcile_plan_section_walls", tier=2)
def reconcile_plan_section_walls(
    kg: ProjectKG,
    plan_path: str,
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    thickness_tol_m: float = 0.02,
    x_cut_tol_m: float = 0.10,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
) -> Dict[str, Any]:
    """Recoupe les murs du plan avec ceux observés dans chaque coupe (Phase 2.1).

    Première étape de la Phase 2 (création BIM depuis les DXF). Vérifie
    la cohérence d'épaisseur plan↔coupe avant tout commit Revit :

    1. Parse le plan → murs via le classifier (paires parallèles +
       centerline fallback). Output : `WallCandidate(p1, p2, thickness)`.
    2. Lit le `DxfImportContext` du KG (ou utilise `section_lines` passé
       en argument) → traits de coupe avec leur DXF coupe associé.
    3. Pour chaque DXF coupe référencé → parse + `read_section_walls`
       (paires verticales sur A-WALL → `(x_cut_m, thickness_m)`).
    4. Délègue à `dwg_coherence.reconcile_plan_section_walls` qui croise
       les deux via la convention DXF anchor (cf. mémoire
       `project-dxf-section-anchor-investigation`).

    **Read-only** : aucun écrit Revit, aucun écrit KG. Le caller décide
    quoi faire des mismatches (typiquement : présenter à l'user via
    `ui_confirm_choices` avant de continuer Phase 2).

    Concepts: dxf, dwg, recoupement, cohérence, walls, plan, coupe,
              section, épaisseur, thickness, mismatch, validation,
              phase 2, audit, vérification
    Phrases: "recoupe les plans et les coupes", "vérifie la cohérence
             des murs", "phase 2 étape 1", "audit walls plan coupe",
             "check plan section consistency"
    Similar: dwg_inspect_sections, dwg_classify, dxf_context_get

    Args:
        plan_path: chemin du DXF plan à analyser.
        layer_mapping: `{layer_name: role}`. Défaut `{"A-WALL": "wall"}`
            (convention AIA/Revit standard). À surcharger pour ISO ou
            ArchiCAD.
        scale_override: facteur m-par-unité-DXF (cf. `dwg_inspect`).
        section_lines: liste de section_lines explicite (cf. format de
            `DxfImportContext.section_lines`). Si absent, lue depuis le
            KG. Permet de passer un sous-ensemble (ex : 1 seul trait à
            vérifier) sans toucher au KG.
        thickness_tol_m: tolérance d'épaisseur pour valider un match
            (défaut 2 cm).
        x_cut_tol_m: tolérance de position le long du cut (défaut 10 cm).
        min_thickness_m / max_thickness_m: bornes d'épaisseur (m).
        include_centerline: active la passe centerline du classifier
            plan (cloisons simple-trait). Défaut True.

    Returns:
        {"ok": bool, "plan_walls_count": int, "section_lines_count": int,
         "section_walls_count_by_coupe": {path: int},
         "summary": {matches_ok, thickness_mismatches, no_section_wall_at_x,
                     ambiguous, plan_walls_total, plan_walls_not_crossed,
                     section_walls_unmatched, section_lines_count},
         "matches": [...],  # tronqué si > 200 (matches_ok uniquement)
         "thickness_mismatches": [...],  # tous
         "ambiguous": [...],  # tous
         "no_section_wall_at_x": [...],  # tous
         "section_walls_unmatched": [...],  # tous
         "walls_plan_not_crossed_indices": [...],  # tronqué si > 50
         "needs_user_decision": bool,
         "note": str}
    """
    path = Path(plan_path)
    if not path.exists():
        raise FileNotFoundError("Plan file not found: {}".format(path))

    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}

    # --- 1. Parse plan + classify walls ---------------------------------
    plan_entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    plan_classified = dwg_classifier.classify(
        plan_entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        include_centerline=include_centerline,
    )
    plan_walls_dict = [_wall_candidate_to_dict(w) for w in plan_classified.walls]

    # --- 2. Récupérer les section_lines ---------------------------------
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext vivant dans le KG et `section_lines` "
                "non fourni. Lance d'abord Phase 1 (dwg_inspect_sections + "
                "dxf_context_register_section_line[_many]) ou passe explicite-"
                "ment `section_lines`."
            )
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))
        if not section_lines:
            raise ValueError(
                "DxfImportContext vivant mais sans section_lines enregistrées. "
                "Lance Phase 1 étape 2 d'abord (dxf_context_register_section_"
                "line[_many]).",
            )

    # --- 3. Parse chaque coupe référencée → section walls ---------------
    section_walls_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    distinct_coupe_paths = sorted(
        {sl.get("coupe_path", "") for sl in section_lines if sl.get("coupe_path")}
    )
    for coupe_path in distinct_coupe_paths:
        cp_path = Path(coupe_path)
        if not cp_path.exists():
            raise FileNotFoundError(
                "Coupe DXF référencé par section_lines introuvable : {}".format(
                    cp_path,
                )
            )
        coupe_entities, _ = dwg_reader.parse(
            cp_path, scale_override=scale_override,
        )
        section_walls = dwg_section_reader.read_section_walls(
            coupe_entities,
            min_thickness_m=min_thickness_m,
            max_thickness_m=max_thickness_m,
        )
        section_walls_by_coupe[coupe_path] = [
            _section_wall_to_dict(sw) for sw in section_walls
        ]

    # --- 4. Reconcile (module pur) --------------------------------------
    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls=plan_walls_dict,
        section_lines=section_lines,
        section_walls_by_coupe=section_walls_by_coupe,
        thickness_tol_m=thickness_tol_m,
        x_cut_tol_m=x_cut_tol_m,
    )

    # --- 5. Sérialiser + filtrer par sévérité ---------------------------
    matches_ok = [_match_to_dict(m) for m in report.matches if m.status == "ok"]
    thickness_mismatches = [
        _match_to_dict(m) for m in report.matches if m.status == "thickness_mismatch"
    ]
    ambiguous = [
        _match_to_dict(m) for m in report.matches
        if m.status == "ambiguous_multiple_candidates"
    ]
    no_section_wall = [
        _match_to_dict(m) for m in report.matches
        if m.status == "no_section_wall_at_x"
    ]

    # matches_ok peut être très volumineux ; tronquer à 200.
    if len(matches_ok) > 200:
        matches_ok_payload = matches_ok[:200]
        matches_ok_truncated = True
    else:
        matches_ok_payload = matches_ok
        matches_ok_truncated = False

    walls_not_crossed = report.walls_plan_not_crossed
    if len(walls_not_crossed) > 50:
        walls_not_crossed_payload = walls_not_crossed[:50]
        walls_not_crossed_truncated = True
    else:
        walls_not_crossed_payload = list(walls_not_crossed)
        walls_not_crossed_truncated = False

    needs_user_decision = bool(
        thickness_mismatches or ambiguous or no_section_wall
        or report.section_walls_unmatched
    )

    if not needs_user_decision:
        note = (
            "**Recoupement OK** : tous les murs plan croisés par un trait "
            "matchent une épaisseur cohérente en coupe (tol={:.0f}mm). Pas de "
            "section wall orphelin. Phase 2.2 (détection épaisseurs uniques) "
            "peut démarrer sans intervention user.".format(thickness_tol_m * 1000)
        )
    else:
        problems: List[str] = []
        if thickness_mismatches:
            problems.append("{} mismatch(es) d'épaisseur".format(
                len(thickness_mismatches)
            ))
        if ambiguous:
            problems.append("{} cas ambigu(s) (plusieurs candidats coupe)".format(
                len(ambiguous)
            ))
        if no_section_wall:
            problems.append(
                "{} mur(s) plan croisé(s) sans mur correspondant en coupe".format(
                    len(no_section_wall),
                )
            )
        if report.section_walls_unmatched:
            problems.append(
                "{} mur(s) en coupe sans pendant au plan".format(
                    len(report.section_walls_unmatched),
                )
            )
        note = (
            "**Recoupement INCOMPLET** — {}. Présenter à l'user via "
            "`ui_confirm_choices` pour décider plan vs coupe sur chaque cas, "
            "ou identifier une erreur d'export DXF à corriger avant Phase 2.2."
            .format(" + ".join(problems))
        )

    return {
        "ok": True,
        "plan_walls_count": len(plan_walls_dict),
        "section_lines_count": len(section_lines),
        "section_walls_count_by_coupe": {
            p: len(ws) for p, ws in section_walls_by_coupe.items()
        },
        "summary": report.summary,
        "matches_ok": matches_ok_payload,
        "matches_ok_truncated": matches_ok_truncated,
        "thickness_mismatches": thickness_mismatches,
        "ambiguous": ambiguous,
        "no_section_wall_at_x": no_section_wall,
        "section_walls_unmatched": report.section_walls_unmatched,
        "walls_plan_not_crossed_indices": walls_not_crossed_payload,
        "walls_plan_not_crossed_truncated": walls_not_crossed_truncated,
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


@tool(name="dwg_reconcile_plan_section_floors", tier=2)
def reconcile_plan_section_floors(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    z_tol_m: float = 0.05,
    thickness_tol_m: float = 0.02,
    coverage_min_ratio: float = 0.80,
) -> Dict[str, Any]:
    """Recoupe les dalles du plan (Floors du KG) avec celles observées
    dans chaque coupe (Phase 2c.1 — symétrique de Phase 2.1 pour les murs).

    Vérifie que chaque dalle créée par ``dwg_create_floors_many`` a un
    pendant cohérent dans les coupes : Z (= élévation du niveau),
    épaisseur (= ``FloorType.total_thickness``), et extent (= la portion
    de coupe à l'intérieur du contour plan doit être couverte par une
    paire top/bot dans la coupe).

    Détecte 5 types d'anomalies :
    - ``thickness_mismatch`` : épaisseur plan ≠ épaisseur coupe.
    - ``extent_partial`` : la paire ne couvre que partiellement la zone
      de coupe (signal : trémie non-détectée en plan OU contour trop
      large).
    - ``no_section_pair_at_z`` : aucune paire en coupe au niveau de la
      dalle (signal fort de dalle plan « fantôme »).
    - ``no_section_pair_at_x`` : paire(s) au bon Z mais sans overlap X.
    - ``section_slabs_unmatched`` : paire en coupe sans pendant plan
      (= dalle potentiellement oubliée en plan).

    **Read-only** : aucun écrit Revit, aucun écrit KG. Le caller décide
    quoi faire (typiquement présenter via ``ui_confirm_choices``).

    Concepts: dxf, dwg, recoupement, cohérence, floors, sols, dalles,
              slabs, plan, coupe, section, épaisseur, thickness,
              élévation, extent, trémie, phase 2c, audit, vérification
    Phrases: "recoupe les dalles plan et coupes",
             "vérifie la cohérence des sols", "phase 2c étape 1",
             "audit floors plan coupe", "check plan section floors"
    Similar: dwg_reconcile_plan_section_walls, dwg_create_floors_many,
             dwg_inspect_sections

    Args:
        scale_override: facteur m-par-unité-DXF (cf. ``dwg_inspect``).
        section_lines: liste de section_lines explicite (cf. format de
            ``DxfImportContext.section_lines``). Si absent, lue depuis
            le KG via ``_find_live_context``.
        z_tol_m: tolérance Z pour match niveau (défaut 5 cm).
        thickness_tol_m: tolérance d'épaisseur (défaut 2 cm).
        coverage_min_ratio: fraction min de l'intervalle de coupe qui
            doit être recouverte par la paire pour ``status="ok"``
            (défaut 0.80). En-dessous : ``extent_partial``.

    Returns:
        ``{"ok": bool, "plan_floors_count": int, "section_lines_count": int,
           "section_slabs_count_by_coupe": {path: int},
           "summary": {matches_ok, thickness_mismatches, extent_partial,
                       no_section_pair_at_z, no_section_pair_at_x,
                       plan_floors_total, plan_floors_not_crossed,
                       section_slabs_unmatched, section_lines_count},
           "matches_ok": [...],
           "thickness_mismatches": [...],
           "extent_partial": [...],
           "no_section_pair_at_z": [...],
           "no_section_pair_at_x": [...],
           "section_slabs_unmatched": [...],
           "floors_plan_not_crossed_indices": [...],
           "needs_user_decision": bool,
           "note": str}``
    """
    # --- 1. Récupérer les Floors vivants du KG --------------------------
    plan_floors = _collect_plan_floors_from_kg(kg)

    # --- 2. Récupérer les section_lines ---------------------------------
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext vivant dans le KG et `section_lines` "
                "non fourni. Lance d'abord Phase 1 (dwg_inspect_sections + "
                "dxf_context_register_section_line[_many]) ou passe explicite-"
                "ment `section_lines`."
            )
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))
        if not section_lines:
            raise ValueError(
                "DxfImportContext vivant mais sans section_lines enregistrées. "
                "Lance Phase 1 étape 2 d'abord (dxf_context_register_section_"
                "line[_many]).",
            )

    # --- 3. Parse chaque coupe référencée → section slabs --------------
    section_slabs_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    distinct_coupe_paths = sorted(
        {sl.get("coupe_path", "") for sl in section_lines if sl.get("coupe_path")}
    )
    for coupe_path in distinct_coupe_paths:
        cp_path = Path(coupe_path)
        if not cp_path.exists():
            raise FileNotFoundError(
                "Coupe DXF référencé par section_lines introuvable : {}".format(
                    cp_path,
                )
            )
        coupe_entities, _ = dwg_reader.parse(
            cp_path, scale_override=scale_override,
        )
        slabs = dwg_section_reader.read_section_floor_slabs(coupe_entities)
        section_slabs_by_coupe[coupe_path] = [
            _section_slab_to_dict(s) for s in slabs
        ]

    # --- 4. Reconcile (module pur) --------------------------------------
    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors=plan_floors,
        section_lines=section_lines,
        section_slabs_by_coupe=section_slabs_by_coupe,
        z_tol_m=z_tol_m,
        thickness_tol_m=thickness_tol_m,
        coverage_min_ratio=coverage_min_ratio,
    )

    # --- 5. Sérialiser + filtrer par sévérité ---------------------------
    matches_ok = [_floor_match_to_dict(m) for m in report.matches if m.status == "ok"]
    thickness_mismatches = [
        _floor_match_to_dict(m) for m in report.matches
        if m.status == "thickness_mismatch"
    ]
    extent_partial = [
        _floor_match_to_dict(m) for m in report.matches
        if m.status == "extent_partial"
    ]
    no_section_pair_at_z = [
        _floor_match_to_dict(m) for m in report.matches
        if m.status == "no_section_pair_at_z"
    ]
    no_section_pair_at_x = [
        _floor_match_to_dict(m) for m in report.matches
        if m.status == "no_section_pair_at_x"
    ]

    needs_user_decision = bool(
        thickness_mismatches or extent_partial
        or no_section_pair_at_z or no_section_pair_at_x
        or report.section_slabs_unmatched
    )

    if not needs_user_decision:
        note = (
            "**Recoupement OK** : toutes les dalles plan croisées par un trait "
            "matchent une paire cohérente en coupe (Z tol={:.0f}mm, "
            "ep tol={:.0f}mm, couverture ≥ {:.0%}). Pas de paire orpheline."
            .format(z_tol_m * 1000, thickness_tol_m * 1000, coverage_min_ratio)
        )
    else:
        problems: List[str] = []
        if thickness_mismatches:
            problems.append("{} mismatch(es) d'épaisseur".format(
                len(thickness_mismatches)
            ))
        if extent_partial:
            problems.append("{} extent(s) partiel(s) (trémie possible)".format(
                len(extent_partial)
            ))
        if no_section_pair_at_z:
            problems.append("{} dalle(s) sans paire au bon Z (fantôme)".format(
                len(no_section_pair_at_z)
            ))
        if no_section_pair_at_x:
            problems.append("{} dalle(s) sans paire au bon X".format(
                len(no_section_pair_at_x)
            ))
        if report.section_slabs_unmatched:
            problems.append("{} paire(s) coupe sans pendant plan (oubli ?)".format(
                len(report.section_slabs_unmatched)
            ))
        note = (
            "**Recoupement INCOMPLET** — {}. Présenter à l'user via "
            "`ui_confirm_choices` pour décider plan vs coupe sur chaque cas, "
            "ou identifier un défaut d'extraction à corriger."
            .format(" + ".join(problems))
        )

    return {
        "ok": True,
        "plan_floors_count": len(plan_floors),
        "section_lines_count": len(section_lines),
        "section_slabs_count_by_coupe": {
            p: len(s) for p, s in section_slabs_by_coupe.items()
        },
        "summary": report.summary,
        "matches_ok": matches_ok,
        "thickness_mismatches": thickness_mismatches,
        "extent_partial": extent_partial,
        "no_section_pair_at_z": no_section_pair_at_z,
        "no_section_pair_at_x": no_section_pair_at_x,
        "section_slabs_unmatched": report.section_slabs_unmatched,
        "floors_plan_not_crossed_indices": list(report.floors_plan_not_crossed),
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


# ----- 8.4. Détection orientation X axis par coupe (P2 mirror fix) ----
#
# Détecte automatiquement, pour chaque coupe, si son DXF X axis suit
# la convention "identity" (= +world axis, défaut P7) ou "reversed"
# (= -world axis, cas P2 longitudinal). Utilisé par
# `views_create_section_many` pour flipper basis_x quand nécessaire.


@tool(name="dwg_detect_section_orientations", tier=2)
def detect_section_orientations(
    kg: ProjectKG,
    plan_path: str,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    scale_override: Optional[float] = None,
    thickness_tol_m: float = 0.02,
    x_cut_tol_m: float = 0.10,
) -> Dict[str, Any]:
    """Détecte la convention X axis de chaque DXF coupe (identity ou
    reversed) par cross-validation murs plan ↔ section walls.

    Use case : sur certains projets (cf. P2 longitudinales), le source
    Revit a `FlipDirection()` la vue section, inversant le BasisX du
    DXF exporté. Sans correction, le XREF DXF apparaît miroité en
    Revit après import. Ce tool détecte ces cas automatiquement par
    cross-validation géométrique.

    Algo : pour chaque section_line, parse la coupe, classify les murs
    plan, et teste les 2 conventions (DXF X = +world axis vs -world
    axis). Celle qui matche le plus de murs (par épaisseur + position)
    gagne.

    **Read-only** : aucun écrit Revit/KG. Le caller stocke le résultat
    dans le DxfImportContext.section_lines pour consommation par
    `views_create_section_many`.

    Concepts: dxf, dwg, coupe, section, orientation, basis x, axis,
              mirror, flip, view range, convention, audit, phase 1
    Phrases: "détecte si les coupes sont miroitées",
             "find section x axis convention", "phase 1 orientation"
    Similar: dwg_reconcile_plan_section_walls,
             dxf_assign_coupes_to_traits

    Args:
        plan_path: chemin du DXF plan (pour classifier les murs).
        section_lines: liste de section_lines (cf. format
            DxfImportContext.section_lines). Si None, lit depuis le KG.
        scale_override / thickness_tol_m / x_cut_tol_m: cf.
            `reconcile_plan_section_walls`.

    Returns:
        ``{"ok": bool, "orientations": [{coupe_path, name, convention,
            matches_identity, matches_reversed, walls_crossed,
            confidence}, ...], "summary": {identity_count,
            reversed_count, ambiguous_count}, "note": str}``
    """
    plan_p = Path(plan_path)
    if not plan_p.exists():
        raise FileNotFoundError("Plan file not found: {}".format(plan_p))

    # 1. Parse plan + classify walls.
    plan_entities, _ = dwg_reader.parse(plan_p, scale_override=scale_override)
    plan_classified = dwg_classifier.classify(
        plan_entities, {"A-WALL": "wall"},
        min_thickness_m=0.05, max_thickness_m=0.60, include_centerline=True,
    )
    plan_walls = [_wall_candidate_to_dict(w) for w in plan_classified.walls]

    # Plan world bbox sur le contenu structurel (A-WALL + A-FLOR
    # + S-COLS) — utilisé en fallback bbox-match si walls_crossed=0.
    plan_xs: List[float] = []
    plan_ys: List[float] = []
    for e in plan_entities:
        if e.layer in ("A-WALL", "A-FLOR") and e.kind == "LINE":
            for pt in e.coords:
                plan_xs.append(pt[0])
                plan_ys.append(pt[1])
        elif e.layer == "S-COLS" and e.kind in ("LINE", "INSERT"):
            for pt in e.coords:
                plan_xs.append(pt[0])
                plan_ys.append(pt[1])
    plan_world_x_bbox = (min(plan_xs), max(plan_xs)) if plan_xs else None
    plan_world_y_bbox = (min(plan_ys), max(plan_ys)) if plan_ys else None

    # 2. Récupérer section_lines.
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext et `section_lines` non fourni."
            )
        ctx = kg.get_node(nid)
        section_lines = list(ctx.get("section_lines", []))
        if not section_lines:
            raise ValueError("Pas de section_lines dans le KG.")

    # 3. Pour chaque coupe, parse + read_section_walls + detect.
    orientations: List[Dict[str, Any]] = []
    identity_count = 0
    reversed_count = 0
    ambiguous_count = 0
    for sl in section_lines:
        coupe_path = sl.get("coupe_path", "")
        if not coupe_path:
            continue
        cp = Path(coupe_path)
        if not cp.exists():
            raise FileNotFoundError("Coupe introuvable : {}".format(cp))
        coupe_entities, _ = dwg_reader.parse(cp, scale_override=scale_override)
        sec_walls_raw = dwg_section_reader.read_section_walls(coupe_entities)
        sec_walls = [_section_wall_to_dict(sw) for sw in sec_walls_raw]

        verdict = dwg_coherence.detect_section_x_axis_convention(
            plan_walls=plan_walls,
            section_line=sl,
            section_walls_in_coupe=sec_walls,
            thickness_tol_m=thickness_tol_m,
            x_cut_tol_m=x_cut_tol_m,
        )

        # Fallback bbox-match si pas de signal mur (P2 Coupe 3 : trait
        # hors-bâtiment, 0 mur croisé, mais coupe contient quand même
        # A-FLOR LINEs des dalles → on compare coupe X bbox à plan
        # Y/X bbox projeté).
        bbox_signal: Optional[str] = None
        final_convention = verdict.convention
        final_source = "walls"
        if verdict.walls_crossed == 0:
            # Plan extent along trait : Y bbox pour vertical, X bbox
            # pour horizontal.
            sp1_y = sl["plan_p1"][1]
            sp2_y = sl["plan_p2"][1]
            sp1_x = sl["plan_p1"][0]
            sp2_x = sl["plan_p2"][0]
            is_vertical_trait = abs(sp2_x - sp1_x) < abs(sp2_y - sp1_y)
            plan_along_trait = (
                plan_world_y_bbox if is_vertical_trait
                else plan_world_x_bbox
            )
            # Coupe X bbox sur A-FLOR + A-WALL LINEs (= bordures de
            # dalles/murs visibles en section, fiables pour le bbox).
            # Exclure S-COLS INSERT qui peut extend hors-bâtiment.
            coupe_xs: List[float] = []
            for e in coupe_entities:
                if e.layer in ("A-FLOR", "A-WALL") and e.kind == "LINE":
                    for pt in e.coords:
                        coupe_xs.append(pt[0])
            coupe_x_bbox = (min(coupe_xs), max(coupe_xs)) if coupe_xs else None
            if plan_along_trait is not None and coupe_x_bbox is not None:
                bbox_signal, _diff = dwg_coherence.detect_x_axis_convention_via_bbox(
                    plan_extent_along_trait=plan_along_trait,
                    coupe_x_extent=coupe_x_bbox,
                )
                if bbox_signal is not None:
                    final_convention = bbox_signal
                    final_source = "bbox"

        orientations.append({
            "coupe_path": coupe_path,
            "name": sl.get("name"),
            "convention": final_convention,
            "matches_identity": verdict.matches_identity,
            "matches_reversed": verdict.matches_reversed,
            "walls_crossed": verdict.walls_crossed,
            "confidence": verdict.confidence,
            "bbox_signal": bbox_signal,
            "source": final_source,
        })
        if (verdict.walls_crossed == 0 and bbox_signal is None):
            ambiguous_count += 1
        elif final_convention == "reversed":
            reversed_count += 1
        else:
            identity_count += 1

    summary = {
        "identity_count": identity_count,
        "reversed_count": reversed_count,
        "ambiguous_count": ambiguous_count,
        "section_lines_count": len(section_lines),
    }
    if reversed_count > 0:
        note = (
            "{} coupe(s) en convention 'reversed' détectée(s) (DXF X = "
            "-world axis). Le caller doit set `x_axis_convention=reversed` "
            "lors de la création de la ViewSection pour corriger le miroir "
            "du XREF.".format(reversed_count)
        )
    elif ambiguous_count > 0:
        note = (
            "{} coupe(s) ambigu(ës) (peu de murs croisés ou égalité). "
            "Convention identity par défaut. Vérifier visuellement.".format(
                ambiguous_count,
            )
        )
    else:
        note = (
            "Toutes les coupes utilisent la convention identity (= "
            "convention P7 standard). Pas de flip nécessaire."
        )

    return {
        "ok": True,
        "plan_path": plan_path,
        "orientations": orientations,
        "summary": summary,
        "note": note,
    }


# ----- 8.5. Validation 3D de l'existence des murs (post-création) ------
#
# Layer over `reconcile_plan_section_walls` + `vote_wall_visible_in_
# elevation` : agrège votes coupes ET élévations par **Wall vivant
# du KG**. Verdict par mur (le 1er match du haut emporte) :
#
# - `confirmed` : ≥ 1 YES (coupe ok/ambig OU élévation YES). Le mur
#   est matérialisé en 3D.
# - `unconfirmed` : ≥ 1 NO (coupe sans match au x_cut attendu OU
#   élévation no overlap) ET 0 YES → **suspect View Range artifact**
#   ou mur fantôme à supprimer.
# - `thickness_mismatch_only` : présent en coupe mais épaisseur off
#   (issue d'épaisseur, pas d'existence).
# - `no_3d_evidence` : aucune vue 3D (ni coupe, ni élévation) ne peut
#   se prononcer (mur isolé sans crossing ni projection dans une
#   élévation).
#
# **Murs de façade** : crucial. Un mur de façade ne croise typiquement
# aucun trait de coupe (les coupes sont à l'intérieur), mais il est
# visible **dans l'élévation** qui regarde sa face extérieure. Sans
# ce check élévation, on classerait à tort `no_crossings`.
#
# **Read-only** : aucune suppression auto. L'user décide.


@tool(name="dwg_validate_walls_3d_existence", tier=2)
def validate_walls_3d_existence(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    thickness_tol_m: float = 0.02,
    x_cut_tol_m: float = 0.10,
) -> Dict[str, Any]:
    """Valide l'existence 3D de chaque Wall vivant du KG en croisant
    avec les coupes (Phase 2a.2 — post-création).

    Use case : après Phase 2a (`dwg_create_continuous_walls_many`),
    certains murs créés peuvent être des **artifacts View Range Revit**
    (le plan d'étage Niveau N exporté montre par défaut les éléments
    des niveaux inférieurs). Sans cross-validation, on ne peut pas
    distinguer un mur légitime (étage habité, mur porteur) d'un
    artifact (mur N-1 visible dans le plan N).

    **Principe** : un mur réel coupé par une coupe doit apparaître
    comme paire verticale A-WALL dans cette coupe, au bon x_cut, à
    la bonne épaisseur (cf. `reconcile_plan_section_walls`). Si un
    mur est croisé par ≥ 1 trait de coupe et 0 confirmation → fort
    signal qu'il est fantôme.

    **Read-only** : aucun écrit Revit, aucun écrit KG. L'user décide
    quoi faire des `walls_unconfirmed_in_3d` (typiquement : les
    supprimer en UI Revit ou via un tool de suppression dédié).

    Concepts: dxf, dwg, recoupement, validation, 3d, existence, mur,
              wall, view range, fantôme, phase 2a, post-creation,
              cross-validation
    Phrases: "valide les murs avec les coupes",
             "cross-validation 3D des murs", "trouve les murs fantômes",
             "phase 2a étape 2"
    Similar: dwg_reconcile_plan_section_walls,
             dwg_validate_floors_3d_existence

    Args:
        scale_override: cf. `dwg_inspect`.
        section_lines: liste explicite (cf. format
            `DxfImportContext.section_lines`). Si absent, lue depuis
            le KG.
        thickness_tol_m / x_cut_tol_m: cf.
            `reconcile_plan_section_walls`.

    Returns:
        ``{"ok": bool, "walls_total": int, "section_lines_count": int,
            "summary": {confirmed, unconfirmed, no_crossings,
                        thickness_mismatch_only, walls_total},
            "walls_confirmed": [{llm_id, crossings, confirmations}, ...],
            "walls_unconfirmed_in_3d": [{llm_id, crossings, p1, p2,
                thickness_m, level_name}, ...],   # SUSPECTS
            "walls_no_3d_evidence": [{llm_id, p1, p2, ...}, ...],
            "walls_thickness_mismatch_only": [{llm_id, ...}, ...],
            "needs_user_decision": bool, "note": str}``
    """
    # 1. Collect KG walls.
    kg_wall_ids: List[str] = sorted(kg.find_by_type("Wall"))
    living_walls: List[Dict[str, Any]] = []
    for wid in kg_wall_ids:
        attrs = kg.get_node(wid)
        if attrs.get("deleted_at_turn") is not None:
            continue
        p1 = attrs.get("p1")
        p2 = attrs.get("p2")
        type_ref = attrs.get("type_ref")
        if not p1 or not p2 or not type_ref:
            continue
        try:
            tnode = kg.get_node(type_ref)
        except KeyError:
            continue
        thickness = float(tnode.get("total_thickness", 0.0))
        level_ref = attrs.get("level_ref")
        level_name = None
        level_elev = 0.0
        if level_ref:
            try:
                lvl_node = kg.get_node(level_ref)
                level_name = lvl_node.get("name")
                level_elev = float(lvl_node.get("elevation", 0.0))
            except KeyError:
                pass
        height_m = float(attrs.get("height", 3.0))
        living_walls.append({
            "llm_id": wid,
            "p1": [float(p1[0]), float(p1[1])],
            "p2": [float(p2[0]), float(p2[1])],
            "thickness_m": thickness,
            "level_name": level_name,
            "level_elev_m": level_elev,
            "height_m": height_m,
        })

    if not living_walls:
        return {
            "ok": True, "walls_total": 0, "section_lines_count": 0,
            "elevations_count": 0,
            "summary": {
                "confirmed": 0, "unconfirmed": 0, "no_3d_evidence": 0,
                "thickness_mismatch_only": 0, "walls_total": 0,
            },
            "walls_confirmed": [], "walls_unconfirmed_in_3d": [],
            "walls_no_3d_evidence": [], "walls_thickness_mismatch_only": [],
            "needs_user_decision": False,
            "note": "Aucun Wall vivant dans le KG — rien à valider.",
        }

    # 2. Section_lines : KG ou explicite.
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext vivant dans le KG et `section_lines` "
                "non fourni. Lance Phase 1 d'abord."
            )
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))
        if not section_lines:
            raise ValueError(
                "DxfImportContext vivant mais sans section_lines enregistrées."
            )

    # 3. Parse chaque coupe → section_walls.
    section_walls_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    distinct_coupes = sorted(
        {sl.get("coupe_path", "") for sl in section_lines if sl.get("coupe_path")}
    )
    for cp in distinct_coupes:
        cp_path = Path(cp)
        if not cp_path.exists():
            raise FileNotFoundError(
                "Coupe DXF introuvable : {}".format(cp_path)
            )
        coupe_entities, _ = dwg_reader.parse(
            cp_path, scale_override=scale_override,
        )
        sec_walls = dwg_section_reader.read_section_walls(coupe_entities)
        section_walls_by_coupe[cp] = [
            _section_wall_to_dict(sw) for sw in sec_walls
        ]

    # 4. Reconcile avec walls KG comme input plan_walls.
    plan_walls_input = [
        {"p1": w["p1"], "p2": w["p2"], "thickness_m": w["thickness_m"]}
        for w in living_walls
    ]
    report = dwg_coherence.reconcile_plan_section_walls(
        plan_walls=plan_walls_input,
        section_lines=section_lines,
        section_walls_by_coupe=section_walls_by_coupe,
        thickness_tol_m=thickness_tol_m,
        x_cut_tol_m=x_cut_tol_m,
    )

    # 5a. Agréger votes coupes par mur.
    per_wall: Dict[int, Dict[str, int]] = {}
    for m in report.matches:
        bucket = per_wall.setdefault(m.plan_wall_index, {
            "crossings": 0, "ok_or_ambig": 0, "thickness_mismatch": 0,
            "no_section_wall_at_x": 0,
        })
        bucket["crossings"] += 1
        if m.status in ("ok", "ambiguous_multiple_candidates"):
            bucket["ok_or_ambig"] += 1
        elif m.status == "thickness_mismatch":
            bucket["thickness_mismatch"] += 1
        elif m.status == "no_section_wall_at_x":
            bucket["no_section_wall_at_x"] += 1

    # 5b. Voter dans les élévations pour chaque mur.
    elevations = _load_elevations_from_kg(kg, scale_override=scale_override)
    per_wall_elev: Dict[int, Dict[str, int]] = {}
    for wi, w in enumerate(living_walls):
        yes = 0
        no = 0
        abst = 0
        for direction, elev_view in elevations.items():
            vote = dwg_elevation_reader.vote_wall_visible_in_elevation(
                wall_p1=tuple(w["p1"]),
                wall_p2=tuple(w["p2"]),
                level_elevation_m=w["level_elev_m"],
                height_m=w["height_m"],
                elevation=elev_view,
                wall_thickness_m=w["thickness_m"],
            )
            if vote.answer is True:
                yes += 1
            elif vote.answer is False:
                no += 1
            else:
                abst += 1
        per_wall_elev[wi] = {"yes": yes, "no": no, "abstain": abst}

    walls_confirmed: List[Dict[str, Any]] = []
    walls_unconfirmed: List[Dict[str, Any]] = []
    walls_no_3d_evidence: List[Dict[str, Any]] = []
    walls_thickness_only: List[Dict[str, Any]] = []
    for wi, w in enumerate(living_walls):
        sec = per_wall.get(wi, {
            "crossings": 0, "ok_or_ambig": 0,
            "thickness_mismatch": 0, "no_section_wall_at_x": 0,
        })
        elev = per_wall_elev[wi]
        # Total YES = confirmations coupes (ok/ambig) + YES élévations.
        # Total NO = no_section_wall_at_x + NO élévations.
        total_yes = sec["ok_or_ambig"] + elev["yes"]
        total_no = sec["no_section_wall_at_x"] + elev["no"]
        thk_mismatches = sec["thickness_mismatch"]
        entry = {
            "llm_id": w["llm_id"],
            "p1": w["p1"], "p2": w["p2"],
            "thickness_m": round(w["thickness_m"], 4),
            "level_name": w["level_name"],
            "level_elev_m": w["level_elev_m"],
            "height_m": w["height_m"],
            "section_crossings": sec["crossings"],
            "section_confirmations": sec["ok_or_ambig"],
            "section_missing": sec["no_section_wall_at_x"],
            "section_thickness_mismatch": thk_mismatches,
            "elevation_yes": elev["yes"],
            "elevation_no": elev["no"],
            "elevation_abstain": elev["abstain"],
            "total_yes": total_yes,
            "total_no": total_no,
        }
        if total_yes >= 1:
            walls_confirmed.append(entry)
        elif total_no >= 1:
            walls_unconfirmed.append(entry)
        elif thk_mismatches > 0 and sec["no_section_wall_at_x"] == 0:
            walls_thickness_only.append(entry)
        else:
            # 0 YES, 0 NO partout → aucune vue 3D ne peut juger.
            walls_no_3d_evidence.append(entry)

    summary = {
        "confirmed": len(walls_confirmed),
        "unconfirmed": len(walls_unconfirmed),
        "no_3d_evidence": len(walls_no_3d_evidence),
        "thickness_mismatch_only": len(walls_thickness_only),
        "walls_total": len(living_walls),
    }
    needs_user_decision = bool(walls_unconfirmed or walls_thickness_only)
    if not needs_user_decision:
        note = (
            "**Validation 3D OK** : tous les murs avec évidence 3D "
            "(coupe ou élévation) sont confirmés. {} mur(s) sans aucune "
            "évidence 3D (ni coupe ni élévation ne les voit).".format(
                len(walls_no_3d_evidence),
            )
        )
    else:
        note = (
            "**Validation 3D INCOMPLÈTE** : {} mur(s) suspects (≥ 1 vue "
            "3D dit explicitement 'absent', 0 confirmation). {} avec "
            "mismatch d'épaisseur uniquement. {} sans évidence 3D. "
            "Inspecter `walls_unconfirmed_in_3d`.".format(
                len(walls_unconfirmed), len(walls_thickness_only),
                len(walls_no_3d_evidence),
            )
        )

    return {
        "ok": True,
        "walls_total": len(living_walls),
        "section_lines_count": len(section_lines),
        "elevations_count": len(elevations),
        "summary": summary,
        "walls_confirmed": walls_confirmed,
        "walls_unconfirmed_in_3d": walls_unconfirmed,
        "walls_no_3d_evidence": walls_no_3d_evidence,
        "walls_thickness_mismatch_only": walls_thickness_only,
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


# ----- 8.6. Validation 3D de l'existence des sols (post-création) ------
#
# Layer over `reconcile_plan_section_floors` : agrège le rapport par
# **Floor vivant du KG**. Verdict par dalle :
#
# - `confirmed` : ≥ 1 intervalle de coupe avec paire correspondante.
# - `unconfirmed` : ≥ 1 intervalle de coupe, mais 0 paire au bon Z
#   (= suspect dalle fantôme dans le plan).
# - `partial_extent` : crossings trouvés en partie seulement (signal
#   trémie non-détectée ou contour plan trop large).
# - `no_crossings` : aucun trait ne traverse cette dalle.


@tool(name="dwg_validate_floors_3d_existence", tier=2)
def validate_floors_3d_existence(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    z_tol_m: float = 0.05,
    thickness_tol_m: float = 0.02,
    coverage_min_ratio: float = 0.80,
) -> Dict[str, Any]:
    """Valide l'existence 3D de chaque Floor vivant du KG en croisant
    avec les coupes (Phase 2c.2 — post-création).

    Use case : après Phase 2c (`dwg_create_floors_many`), valider que
    chaque dalle créée a au moins 1 paire top/bot correspondante dans
    une coupe qui la traverse. Identique en principe à la validation
    walls (Phase 2a.2) : on agrège `reconcile_plan_section_floors`
    par Floor au lieu de par crossing.

    **Read-only** : aucune suppression auto. Le rapport propose les
    suspects, l'user décide.

    Concepts: dxf, dwg, validation, 3d, existence, sol, dalle, floor,
              view range, fantôme, phase 2c, post-creation,
              cross-validation
    Phrases: "valide les sols avec les coupes",
             "cross-validation 3D des dalles", "trouve les dalles fantômes",
             "phase 2c étape 2"
    Similar: dwg_validate_walls_3d_existence,
             dwg_reconcile_plan_section_floors

    Args:
        scale_override / section_lines / z_tol_m / thickness_tol_m /
            coverage_min_ratio: cf. `dwg_reconcile_plan_section_floors`.

    Returns:
        ``{"ok": bool, "floors_total": int, "section_lines_count": int,
            "summary": {confirmed, unconfirmed, partial_extent,
                        no_crossings, floors_total},
            "floors_confirmed": [...],
            "floors_unconfirmed_in_3d": [...],   # SUSPECTS
            "floors_partial_extent": [...],
            "floors_no_crossings": [...],
            "needs_user_decision": bool, "note": str}``
    """
    # 1. Collect KG floors.
    living_floors_full = _collect_plan_floors_from_kg(kg)
    if not living_floors_full:
        return {
            "ok": True, "floors_total": 0, "section_lines_count": 0,
            "summary": {
                "confirmed": 0, "unconfirmed": 0,
                "partial_extent": 0, "no_crossings": 0, "floors_total": 0,
            },
            "floors_confirmed": [], "floors_unconfirmed_in_3d": [],
            "floors_partial_extent": [], "floors_no_crossings": [],
            "needs_user_decision": False,
            "note": "Aucun Floor vivant dans le KG — rien à valider.",
        }

    # 2. Section_lines.
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext vivant dans le KG et `section_lines` "
                "non fourni."
            )
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))
        if not section_lines:
            raise ValueError(
                "DxfImportContext vivant mais sans section_lines enregistrées."
            )

    # 3. Parse chaque coupe → section_slabs.
    section_slabs_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    distinct_coupes = sorted(
        {sl.get("coupe_path", "") for sl in section_lines if sl.get("coupe_path")}
    )
    for cp in distinct_coupes:
        cp_path = Path(cp)
        if not cp_path.exists():
            raise FileNotFoundError(
                "Coupe DXF introuvable : {}".format(cp_path)
            )
        coupe_entities, _ = dwg_reader.parse(
            cp_path, scale_override=scale_override,
        )
        slabs = dwg_section_reader.read_section_floor_slabs(coupe_entities)
        section_slabs_by_coupe[cp] = [
            _section_slab_to_dict(s) for s in slabs
        ]

    # 4. Reconcile avec floors KG.
    report = dwg_coherence.reconcile_plan_section_floors(
        plan_floors=living_floors_full,
        section_lines=section_lines,
        section_slabs_by_coupe=section_slabs_by_coupe,
        z_tol_m=z_tol_m,
        thickness_tol_m=thickness_tol_m,
        coverage_min_ratio=coverage_min_ratio,
    )

    # 5. Agréger par dalle.
    per_floor: Dict[int, Dict[str, int]] = {}
    for m in report.matches:
        bucket = per_floor.setdefault(m.plan_floor_index, {
            "intervals": 0, "ok": 0, "thickness_mismatch": 0,
            "extent_partial": 0, "no_pair_at_z": 0, "no_pair_at_x": 0,
        })
        bucket["intervals"] += 1
        if m.status == "ok":
            bucket["ok"] += 1
        elif m.status == "thickness_mismatch":
            bucket["thickness_mismatch"] += 1
        elif m.status == "extent_partial":
            bucket["extent_partial"] += 1
        elif m.status == "no_section_pair_at_z":
            bucket["no_pair_at_z"] += 1
        elif m.status == "no_section_pair_at_x":
            bucket["no_pair_at_x"] += 1

    not_crossed = set(report.floors_plan_not_crossed)
    floors_confirmed: List[Dict[str, Any]] = []
    floors_unconfirmed: List[Dict[str, Any]] = []
    floors_partial: List[Dict[str, Any]] = []
    floors_no_crossings: List[Dict[str, Any]] = []
    for fi, f in enumerate(living_floors_full):
        bucket = per_floor.get(fi, {
            "intervals": 0, "ok": 0, "thickness_mismatch": 0,
            "extent_partial": 0, "no_pair_at_z": 0, "no_pair_at_x": 0,
        })
        entry = {
            "llm_id": f.get("llm_id"),
            "level_name": f.get("level_name"),
            "elevation_m": f.get("elevation_m"),
            "thickness_m": f.get("thickness_m"),
            "intervals": bucket["intervals"],
            "ok": bucket["ok"],
            "thickness_mismatches": bucket["thickness_mismatch"],
            "extent_partial": bucket["extent_partial"],
            "no_pair_at_z": bucket["no_pair_at_z"],
            "no_pair_at_x": bucket["no_pair_at_x"],
        }
        if fi in not_crossed or bucket["intervals"] == 0:
            floors_no_crossings.append(entry)
        elif bucket["ok"] > 0 and bucket["extent_partial"] == 0:
            floors_confirmed.append(entry)
        elif bucket["extent_partial"] > 0:
            floors_partial.append(entry)
        else:
            # intervals > 0 mais 0 ok → unconfirmed.
            floors_unconfirmed.append(entry)

    summary = {
        "confirmed": len(floors_confirmed),
        "unconfirmed": len(floors_unconfirmed),
        "partial_extent": len(floors_partial),
        "no_crossings": len(floors_no_crossings),
        "floors_total": len(living_floors_full),
    }
    needs_user_decision = bool(floors_unconfirmed or floors_partial)
    if not needs_user_decision:
        note = (
            "**Validation 3D OK** : toutes les dalles croisées par un trait "
            "ont au moins 1 confirmation en coupe. "
            "{} dalle(s) sans crossing.".format(len(floors_no_crossings))
        )
    else:
        note = (
            "**Validation 3D INCOMPLÈTE** : {} dalle(s) sans aucune "
            "confirmation en coupe (suspects fantômes). {} avec extent "
            "partiel (trémie non détectée ?). {} sans crossing. Inspecter "
            "`floors_unconfirmed_in_3d` + `floors_partial_extent`.".format(
                len(floors_unconfirmed), len(floors_partial),
                len(floors_no_crossings),
            )
        )

    return {
        "ok": True,
        "floors_total": len(living_floors_full),
        "section_lines_count": len(section_lines),
        "summary": summary,
        "floors_confirmed": floors_confirmed,
        "floors_unconfirmed_in_3d": floors_unconfirmed,
        "floors_partial_extent": floors_partial,
        "floors_no_crossings": floors_no_crossings,
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


# ----- 8.7. Validation 3D de l'existence des colonnes (post-création) --
#
# Différent du pattern walls/floors : les colonnes sont des **points
# 2D** (pas des segments). Une colonne plan à `(x_col, y_col)` est
# "traversée" par un trait de coupe si la distance point↔segment est
# inférieure à un seuil (typiquement 50cm = ordre de grandeur d'une
# colonne large). Pour chaque crossing, expected x_cut = world Y
# (trait vertical) ou world X (horizontal). On cherche un `SectionColumn`
# dans la coupe à cette abscisse (± tol).


def _distance_point_to_segment(
    pt: Tuple[float, float],
    a: Tuple[float, float], b: Tuple[float, float],
) -> float:
    """Distance euclidienne minimale entre un point et un segment 2D."""
    ax, ay = a
    bx, by = b
    px, py = pt
    abx, aby = bx - ax, by - ay
    ab_len_sq = abx * abx + aby * aby
    if ab_len_sq < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * abx + (py - ay) * aby) / ab_len_sq
    t = max(0.0, min(1.0, t))
    proj_x = ax + t * abx
    proj_y = ay + t * aby
    return math.hypot(px - proj_x, py - proj_y)


@tool(name="dwg_validate_columns_3d_existence", tier=2)
def validate_columns_3d_existence(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    point_on_section_tol_m: float = 0.50,
    x_cut_tol_m: float = 0.30,
) -> Dict[str, Any]:
    """Valide l'existence 3D de chaque Column vivante du KG en croisant
    avec les coupes (Phase 2d.2 — post-création).

    **Détection symétrique mais adaptée aux points** : une colonne plan
    est considérée traversée par un trait de coupe si sa distance au
    trait < `point_on_section_tol_m` (défaut 50cm — couvre les colonnes
    larges et les drift d'export). Pour chaque crossing, on cherche un
    `SectionColumn` (INSERT ou paire LINEs S-COLS) au x_cut attendu
    dans la coupe correspondante.

    Verdict par colonne :
    - `confirmed` : ≥ 1 crossing avec un SectionColumn correspondant.
    - `unconfirmed` : ≥ 1 crossing mais 0 SectionColumn trouvé.
    - `no_crossings` : aucun trait ne traverse cette colonne.

    Concepts: dxf, dwg, validation, 3d, existence, poteau, column,
              view range, fantôme, phase 2d, post-creation,
              cross-validation
    Phrases: "valide les poteaux avec les coupes",
             "cross-validation 3D des colonnes",
             "trouve les poteaux fantômes", "phase 2d étape 2"
    Similar: dwg_validate_walls_3d_existence,
             dwg_validate_floors_3d_existence

    Args:
        scale_override / section_lines: cf. les autres validate tools.
        point_on_section_tol_m: distance max colonne↔trait pour que ça
            compte comme un crossing (défaut 50 cm).
        x_cut_tol_m: tolérance sur la position dans la coupe pour
            matcher un SectionColumn (défaut 30 cm).

    Returns:
        ``{"ok": bool, "columns_total": int, "section_lines_count": int,
            "summary": {confirmed, unconfirmed, no_crossings, columns_total},
            "columns_confirmed": [...],
            "columns_unconfirmed_in_3d": [...],   # SUSPECTS
            "columns_no_crossings": [...],
            "needs_user_decision": bool, "note": str}``
    """
    # 1. Collect KG columns (with position + level).
    living_cols: List[Dict[str, Any]] = []
    for cid in sorted(kg.find_by_type("Column")):
        attrs = kg.get_node(cid)
        if attrs.get("deleted_at_turn") is not None:
            continue
        pos = attrs.get("position")
        if not pos or len(pos) < 2:
            continue
        lvl_ref = attrs.get("level_ref")
        level_name = None
        elev = None
        if lvl_ref:
            try:
                lvl = kg.get_node(lvl_ref)
                level_name = lvl.get("name")
                elev = float(lvl.get("elevation", 0.0))
            except KeyError:
                pass
        living_cols.append({
            "llm_id": cid,
            "position": [float(pos[0]), float(pos[1])],
            "level_name": level_name,
            "base_elev_m": elev,
            "height_m": float(attrs.get("height", 0.0)),
        })

    if not living_cols:
        return {
            "ok": True, "columns_total": 0, "section_lines_count": 0,
            "summary": {
                "confirmed": 0, "unconfirmed": 0, "no_crossings": 0,
                "columns_total": 0,
            },
            "columns_confirmed": [], "columns_unconfirmed_in_3d": [],
            "columns_no_crossings": [],
            "needs_user_decision": False,
            "note": "Aucune Column vivante dans le KG — rien à valider.",
        }

    # 2. Section_lines.
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext vivant et `section_lines` non fourni."
            )
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))
        if not section_lines:
            raise ValueError(
                "DxfImportContext vivant mais sans section_lines."
            )

    # 3. Parse chaque coupe → section_columns.
    section_cols_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    distinct_coupes = sorted(
        {sl.get("coupe_path", "") for sl in section_lines if sl.get("coupe_path")}
    )
    for cp in distinct_coupes:
        cp_path = Path(cp)
        if not cp_path.exists():
            raise FileNotFoundError(
                "Coupe DXF introuvable : {}".format(cp_path)
            )
        coupe_entities, _ = dwg_reader.parse(
            cp_path, scale_override=scale_override,
        )
        sec_cols = dwg_section_reader.read_section_columns(coupe_entities)
        section_cols_by_coupe[cp] = [
            {
                "x_cut_m": sc.x_cut_m,
                "kind": sc.kind,
                "width_m": sc.width_m,
                "block_name": sc.block_name,
            }
            for sc in sec_cols
        ]

    # 4. Per-column validation.
    columns_confirmed: List[Dict[str, Any]] = []
    columns_unconfirmed: List[Dict[str, Any]] = []
    columns_no_crossings: List[Dict[str, Any]] = []
    for c in living_cols:
        pos = (c["position"][0], c["position"][1])
        crossings_total = 0
        crossings_confirmed = 0
        for sl in section_lines:
            sp1 = (float(sl["plan_p1"][0]), float(sl["plan_p1"][1]))
            sp2 = (float(sl["plan_p2"][0]), float(sl["plan_p2"][1]))
            dist = _distance_point_to_segment(pos, sp1, sp2)
            if dist > point_on_section_tol_m:
                continue
            crossings_total += 1
            # Compute expected x_cut.
            trait_dx = sp2[0] - sp1[0]
            trait_dy = sp2[1] - sp1[1]
            if abs(trait_dx) < abs(trait_dy):
                expected_x_cut = pos[1]  # vertical trait → x_cut = world Y
            else:
                expected_x_cut = pos[0]  # horizontal trait → x_cut = world X
            coupe_path = sl.get("coupe_path", "")
            sec_cols = section_cols_by_coupe.get(coupe_path, [])
            for sc in sec_cols:
                if abs(float(sc["x_cut_m"]) - expected_x_cut) <= x_cut_tol_m:
                    crossings_confirmed += 1
                    break  # 1 confirmation suffit par crossing

        entry = {
            "llm_id": c["llm_id"],
            "position": c["position"],
            "level_name": c["level_name"],
            "base_elev_m": c["base_elev_m"],
            "height_m": c["height_m"],
            "crossings": crossings_total,
            "confirmations": crossings_confirmed,
        }
        if crossings_total == 0:
            columns_no_crossings.append(entry)
        elif crossings_confirmed > 0:
            columns_confirmed.append(entry)
        else:
            columns_unconfirmed.append(entry)

    summary = {
        "confirmed": len(columns_confirmed),
        "unconfirmed": len(columns_unconfirmed),
        "no_crossings": len(columns_no_crossings),
        "columns_total": len(living_cols),
    }
    needs_user_decision = bool(columns_unconfirmed)
    if not needs_user_decision:
        note = (
            "**Validation 3D OK** : tous les poteaux croisés par un trait "
            "ont au moins 1 confirmation en coupe. {} poteau(x) sans "
            "crossing.".format(len(columns_no_crossings))
        )
    else:
        note = (
            "**Validation 3D INCOMPLÈTE** : {} poteau(x) sans confirmation "
            "en coupe (suspects fantômes). {} sans crossing. Inspecter "
            "`columns_unconfirmed_in_3d`.".format(
                len(columns_unconfirmed), len(columns_no_crossings),
            )
        )

    return {
        "ok": True,
        "columns_total": len(living_cols),
        "section_lines_count": len(section_lines),
        "summary": summary,
        "columns_confirmed": columns_confirmed,
        "columns_unconfirmed_in_3d": columns_unconfirmed,
        "columns_no_crossings": columns_no_crossings,
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


# ----- 8.75. Validation 3D existence des openings (Phase 2b.2) ---------
#
# Pour chaque Window / Door vivant du KG :
# - Vote sur chaque élévation via `vote_opening_visible_in_elevation`.
#   Élévations = la voie principale pour les fenêtres (cf. user
#   2026-05-14 : « les élévations sont déterminantes pour les fenêtres
#   et les murs de façade »).
# - Vote sur chaque coupe qui traverse le mur hôte : présence d'une
#   `SectionOpening` à la position x_cut attendue + Y range matchant.
# - Verdict consolidé.


@tool(name="dwg_validate_openings_3d_existence", tier=2)
def validate_openings_3d_existence(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    x_cut_tol_m: float = 0.30,
    y_tol_m: float = 0.30,
    default_width_m: float = 1.0,
) -> Dict[str, Any]:
    """Valide l'existence 3D de chaque Window / Door vivant du KG en
    croisant avec les coupes ET les élévations (Phase 2b.2 — post-
    création).

    **Élévations centrales** pour les fenêtres : une fenêtre de façade
    n'est typiquement pas vue par les coupes (qui traversent l'intérieur
    du bâtiment), mais elle laisse un linteau + une allège bien visibles
    dans l'élévation qui regarde la bonne façade. Sans le check
    élévation, on classerait à tort les fenêtres de façade comme
    « pas d'évidence 3D ».

    Verdict par opening :
    - `confirmed` : ≥ 1 YES (coupe OU élévation).
    - `unconfirmed` : ≥ 1 NO partout, 0 YES → suspect.
    - `no_3d_evidence` : 0 YES + 0 NO (toutes les vues abstiennent).

    Concepts: dxf, dwg, validation, 3d, existence, fenêtre, window,
              porte, door, opening, élévation, coupe, view range,
              phase 2b, post-creation, cross-validation
    Phrases: "valide les fenêtres et portes avec coupes et élévations",
             "cross-validation 3D des openings",
             "trouve les fenêtres fantômes"
    Similar: dwg_validate_walls_3d_existence,
             dwg_validate_floors_3d_existence

    Args:
        scale_override / section_lines: cf. les autres validate tools.
        x_cut_tol_m: tolérance en X dans la coupe pour match
            `SectionOpening` (défaut 30 cm).
        y_tol_m: tolérance en Y (sill / head) (défaut 30 cm).
        default_width_m: largeur à supposer pour une opening sans
            dimensions explicites dans son FamilyType (défaut 1m).

    Returns:
        ``{"ok", "openings_total", "section_lines_count", "elevations_count",
            "summary": {confirmed, unconfirmed, no_3d_evidence,
                        windows_total, doors_total, openings_total},
            "openings_confirmed": [...],
            "openings_unconfirmed_in_3d": [...],   # SUSPECTS
            "openings_no_3d_evidence": [...],
            "needs_user_decision", "note"}``
    """
    # 1. Collect KG openings (windows + doors).
    living_openings: List[Dict[str, Any]] = []
    for category in ("Window", "Door"):
        for oid in sorted(kg.find_by_type(category)):
            attrs = kg.get_node(oid)
            if attrs.get("deleted_at_turn") is not None:
                continue
            pos = attrs.get("position")
            host_ref = attrs.get("host_wall_ref")
            type_ref = attrs.get("type_ref")
            if not pos or not host_ref or not type_ref:
                continue
            try:
                host = kg.get_node(host_ref)
            except KeyError:
                continue
            host_p1 = host.get("p1")
            host_p2 = host.get("p2")
            if not host_p1 or not host_p2:
                continue
            level_ref = host.get("level_ref")
            level_elev = 0.0
            level_name = None
            if level_ref:
                try:
                    lvl = kg.get_node(level_ref)
                    level_elev = float(lvl.get("elevation", 0.0))
                    level_name = lvl.get("name")
                except KeyError:
                    pass
            # Largeur depuis FamilyType.dimensions si dispo.
            width_m = default_width_m
            try:
                tnode = kg.get_node(type_ref)
                dims = tnode.get("dimensions") or {}
                if isinstance(dims, dict):
                    w = dims.get("width_m") or dims.get("width")
                    if isinstance(w, (int, float)) and w > 0:
                        width_m = float(w)
            except KeyError:
                pass
            sill = float(attrs.get("sill_height", 0.0))
            head = float(attrs.get("head_height", sill + 2.1))
            height = max(head - sill, 0.1)
            living_openings.append({
                "llm_id": oid,
                "category": category,
                "position": [float(pos[0]), float(pos[1])],
                "host_wall_ref": host_ref,
                "host_p1": [float(host_p1[0]), float(host_p1[1])],
                "host_p2": [float(host_p2[0]), float(host_p2[1])],
                "level_elev_m": level_elev,
                "level_name": level_name,
                "sill_m": sill,
                "head_m": head,
                "height_m": height,
                "width_m": width_m,
            })

    if not living_openings:
        return {
            "ok": True, "openings_total": 0,
            "section_lines_count": 0, "elevations_count": 0,
            "summary": {
                "confirmed": 0, "unconfirmed": 0, "no_3d_evidence": 0,
                "windows_total": 0, "doors_total": 0, "openings_total": 0,
            },
            "openings_confirmed": [], "openings_unconfirmed_in_3d": [],
            "openings_no_3d_evidence": [],
            "needs_user_decision": False,
            "note": "Aucune Window/Door vivante dans le KG.",
        }

    # 2. Section_lines.
    if section_lines is None:
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is None:
            raise ValueError(
                "Pas de DxfImportContext et `section_lines` non fourni."
            )
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))
        if not section_lines:
            raise ValueError(
                "DxfImportContext vivant mais sans section_lines."
            )

    # 3. Lire SectionOpenings dans chaque coupe.
    section_openings_by_coupe: Dict[str, List[Any]] = {}
    distinct_coupes = sorted(
        {sl.get("coupe_path", "") for sl in section_lines if sl.get("coupe_path")}
    )
    for cp in distinct_coupes:
        cp_path = Path(cp)
        if not cp_path.exists():
            raise FileNotFoundError(
                "Coupe DXF introuvable : {}".format(cp_path)
            )
        coupe_entities, _ = dwg_reader.parse(
            cp_path, scale_override=scale_override,
        )
        section_openings_by_coupe[cp] = list(
            dwg_section_reader.read_section_openings(coupe_entities)
        )

    # 4. Charger élévations.
    elevations = _load_elevations_from_kg(kg, scale_override=scale_override)

    # 5. Validation per opening.
    openings_confirmed: List[Dict[str, Any]] = []
    openings_unconfirmed: List[Dict[str, Any]] = []
    openings_no_evidence: List[Dict[str, Any]] = []
    for o in living_openings:
        # 5a. Coupes : intersect host_wall avec chaque section_line,
        # puis chercher SectionOpening à x_cut attendu, Y range matching.
        host_p1 = (o["host_p1"][0], o["host_p1"][1])
        host_p2 = (o["host_p2"][0], o["host_p2"][1])
        sec_yes = 0
        sec_no = 0
        for sl in section_lines:
            sp1 = (float(sl["plan_p1"][0]), float(sl["plan_p1"][1]))
            sp2 = (float(sl["plan_p2"][0]), float(sl["plan_p2"][1]))
            inter = dwg_coherence._segment_intersection_2d(host_p1, host_p2, sp1, sp2)
            if inter is None:
                continue
            ix, iy, _t, _u = inter
            trait_dx = sp2[0] - sp1[0]
            trait_dy = sp2[1] - sp1[1]
            x_cut_expected = iy if abs(trait_dx) < abs(trait_dy) else ix
            sill_y = o["level_elev_m"] + o["sill_m"]
            head_y = o["level_elev_m"] + o["head_m"]
            coupe_path = sl.get("coupe_path", "")
            section_openings = section_openings_by_coupe.get(coupe_path, [])
            found = False
            for so in section_openings:
                if abs(so.x_dxf_m - x_cut_expected) > x_cut_tol_m:
                    continue
                # y_dxf_m du SectionOpening = sill probable, height_m =
                # hauteur d'ouverture. On accepte si la zone projetée
                # de l'opening overlapping le y range plan.
                so_y_bot = so.y_dxf_m
                so_y_top = so.y_dxf_m + (so.height_m or o["height_m"])
                if so_y_top < sill_y - y_tol_m:
                    continue
                if so_y_bot > head_y + y_tol_m:
                    continue
                found = True
                break
            if found:
                sec_yes += 1
            else:
                sec_no += 1
        # 5b. Élévations.
        elev_yes = 0
        elev_no = 0
        for _direction, elev_view in elevations.items():
            vote = dwg_elevation_reader.vote_opening_visible_in_elevation(
                opening_world=(o["position"][0], o["position"][1]),
                level_elevation_m=o["level_elev_m"],
                sill_m=o["sill_m"],
                height_m=o["height_m"],
                width_m=o["width_m"],
                elevation=elev_view,
            )
            if vote.answer is True:
                elev_yes += 1
            elif vote.answer is False:
                elev_no += 1
        total_yes = sec_yes + elev_yes
        total_no = sec_no + elev_no
        entry = {
            "llm_id": o["llm_id"],
            "category": o["category"],
            "position": o["position"],
            "level_name": o["level_name"],
            "level_elev_m": o["level_elev_m"],
            "sill_m": o["sill_m"],
            "head_m": o["head_m"],
            "width_m": o["width_m"],
            "section_yes": sec_yes, "section_no": sec_no,
            "elevation_yes": elev_yes, "elevation_no": elev_no,
            "total_yes": total_yes, "total_no": total_no,
        }
        if total_yes >= 1:
            openings_confirmed.append(entry)
        elif total_no >= 1:
            openings_unconfirmed.append(entry)
        else:
            openings_no_evidence.append(entry)

    windows_total = sum(1 for o in living_openings if o["category"] == "Window")
    doors_total = sum(1 for o in living_openings if o["category"] == "Door")
    summary = {
        "confirmed": len(openings_confirmed),
        "unconfirmed": len(openings_unconfirmed),
        "no_3d_evidence": len(openings_no_evidence),
        "windows_total": windows_total,
        "doors_total": doors_total,
        "openings_total": len(living_openings),
    }
    needs_user_decision = bool(openings_unconfirmed)
    if not needs_user_decision:
        note = (
            "**Validation 3D OK** : toutes les ouvertures avec évidence 3D "
            "sont confirmées. {} sans aucune évidence.".format(
                len(openings_no_evidence),
            )
        )
    else:
        note = (
            "**Validation 3D INCOMPLÈTE** : {} ouverture(s) suspect(s). "
            "{} sans évidence. Inspecter `openings_unconfirmed_in_3d`.".format(
                len(openings_unconfirmed), len(openings_no_evidence),
            )
        )

    return {
        "ok": True,
        "openings_total": len(living_openings),
        "section_lines_count": len(section_lines),
        "elevations_count": len(elevations),
        "summary": summary,
        "openings_confirmed": openings_confirmed,
        "openings_unconfirmed_in_3d": openings_unconfirmed,
        "openings_no_3d_evidence": openings_no_evidence,
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


# ----- 8.8. Meta : validation 3D consolidée (walls + floors + columns) -


@tool(name="dwg_validate_import_3d", tier=2)
def validate_import_3d(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Meta-validation 3D post-import : pour chaque élément BIM créé
    (Wall + Floor + Column), agrège les rapports des 3 tools
    `dwg_validate_*_3d_existence` en un rapport unique.

    Use case canonique : après `dwg_import_project_execute`, l'agent
    appelle ce meta-tool pour identifier les éléments BIM potentiellement
    « fantômes » (créés depuis le plan mais sans correspondant 3D dans
    les coupes — typiquement View Range artifacts). L'user reçoit une
    liste consolidée par catégorie et décide quoi supprimer.

    **Read-only** : aucune suppression auto. Le tool produit un rapport,
    l'user (ou un futur tool de suppression dédié) agit.

    Concepts: dxf, dwg, validation, 3d, existence, mur, sol, poteau,
              wall, floor, column, view range, fantôme, meta, audit,
              post-import, cross-validation
    Phrases: "valide l'import 3D", "audit 3D post-import",
             "trouve les éléments fantômes",
             "validation 3D consolidée"
    Similar: dwg_validate_walls_3d_existence,
             dwg_validate_floors_3d_existence,
             dwg_validate_columns_3d_existence

    Args:
        scale_override / section_lines: passés aux 3 tools sous-jacents.

    Returns:
        ``{"ok": bool, "walls": {...validate_walls payload...},
            "floors": {...}, "columns": {...},
            "summary": {walls_unconfirmed, floors_unconfirmed,
                        columns_unconfirmed, total_suspects, total_elements},
            "needs_user_decision": bool, "note": str}``
    """
    walls = validate_walls_3d_existence(
        kg=kg, scale_override=scale_override, section_lines=section_lines,
    )
    floors = validate_floors_3d_existence(
        kg=kg, scale_override=scale_override, section_lines=section_lines,
    )
    columns = validate_columns_3d_existence(
        kg=kg, scale_override=scale_override, section_lines=section_lines,
    )
    openings = validate_openings_3d_existence(
        kg=kg, scale_override=scale_override, section_lines=section_lines,
    )

    summary = {
        "walls_total": walls["walls_total"],
        "walls_confirmed": walls["summary"]["confirmed"],
        "walls_unconfirmed": walls["summary"]["unconfirmed"],
        "walls_no_3d_evidence": walls["summary"]["no_3d_evidence"],
        "floors_total": floors["floors_total"],
        "floors_confirmed": floors["summary"]["confirmed"],
        "floors_unconfirmed": floors["summary"]["unconfirmed"],
        "floors_partial_extent": floors["summary"]["partial_extent"],
        "floors_no_crossings": floors["summary"]["no_crossings"],
        "columns_total": columns["columns_total"],
        "columns_confirmed": columns["summary"]["confirmed"],
        "columns_unconfirmed": columns["summary"]["unconfirmed"],
        "columns_no_crossings": columns["summary"]["no_crossings"],
        "openings_total": openings["openings_total"],
        "openings_confirmed": openings["summary"]["confirmed"],
        "openings_unconfirmed": openings["summary"]["unconfirmed"],
        "openings_no_3d_evidence": openings["summary"]["no_3d_evidence"],
    }
    total_suspects = (
        summary["walls_unconfirmed"]
        + summary["floors_unconfirmed"]
        + summary["floors_partial_extent"]
        + summary["columns_unconfirmed"]
        + summary["openings_unconfirmed"]
    )
    total_elements = (
        summary["walls_total"] + summary["floors_total"]
        + summary["columns_total"] + summary["openings_total"]
    )
    summary["total_suspects"] = total_suspects
    summary["total_elements"] = total_elements

    needs_user_decision = bool(total_suspects)
    if not needs_user_decision:
        note = (
            "**Validation 3D OK** : aucun élément suspect "
            "({} W + {} F + {} C + {} O confirmés).".format(
                summary["walls_confirmed"], summary["floors_confirmed"],
                summary["columns_confirmed"], summary["openings_confirmed"],
            )
        )
    else:
        note = (
            "**{} élément(s) suspect(s)** sur {} : "
            "{} mur(s) + {} sol(s) ({} extent partiel) + {} poteau(x) "
            "+ {} opening(s). Inspecter les sous-payloads."
            .format(
                total_suspects, total_elements,
                summary["walls_unconfirmed"],
                summary["floors_unconfirmed"], summary["floors_partial_extent"],
                summary["columns_unconfirmed"], summary["openings_unconfirmed"],
            )
        )

    return {
        "ok": True,
        "walls": walls,
        "floors": floors,
        "columns": columns,
        "openings": openings,
        "summary": summary,
        "needs_user_decision": needs_user_decision,
        "note": note,
    }


# ----- 8.9. Flag visuel des suspects 3D (couleur en vue Revit) --------
#
# Combine `dwg_validate_import_3d` + `views_override_element_colors_many`.
# Pour chaque élément suspect (unconfirmed_in_3d) → rouge. Pour chaque
# élément sans évidence 3D (no_3d_evidence) → jaune. L'user voit
# directement en 3D ce qu'il faut inspecter / supprimer.


@tool(name="dwg_flag_3d_suspects_in_view", tier=2)
def flag_3d_suspects_in_view(
    kg: ProjectKG,
    doc: Any,
    view_ref: Optional[str] = None,
    flag_no_evidence: bool = True,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Peint en rouge / jaune les éléments suspects identifiés par
    `dwg_validate_import_3d` dans une vue Revit donnée.

    **Rouge** (`unconfirmed_in_3d`) : ≥ 1 vue 3D dit explicitement
    « cet élément n'est pas là » → suspect fantôme à supprimer.

    **Jaune** (`no_3d_evidence`, optionnel) : aucune vue 3D ne peut
    valider (ni coupe ni élévation ne contient cet élément) → à
    vérifier visuellement.

    Couvre walls + floors + columns + openings. Pas d'auto-suppression :
    juste la mise en évidence visuelle. L'user décide manuellement.

    Concepts: flag, suspect, rouge, jaune, validation, 3d, fantôme,
              couleur, override, view, vue, peinture, visuel, audit,
              post-import
    Phrases: "flag les suspects en rouge", "peins les fantômes",
             "color the unconfirmed elements", "highlight 3d suspects"
    Similar: dwg_validate_import_3d, views_override_element_colors_many,
             views_clear_element_overrides

    Args:
        view_ref: llm_id de la vue cible. Si None, vue active.
        flag_no_evidence: si True (défaut), peint aussi les
            no_3d_evidence en jaune. Mettre False pour ne flagger
            QUE les unconfirmed (= rouge seul, vue plus lisible).
        scale_override / section_lines: cf. `dwg_validate_import_3d`.

    Returns:
        ``{"ok", "view_revit_id", "red_count", "yellow_count",
            "total_flagged", "validation": <full validate_import_3d payload>,
            "note"}``
    """
    # 1. Run validation.
    validation = validate_import_3d(
        kg=kg, scale_override=scale_override, section_lines=section_lines,
    )

    # 2. Collect llm_ids to color.
    red_ids: List[Dict[str, Any]] = []
    yellow_ids: List[Dict[str, Any]] = []

    for w in (validation.get("walls", {}).get("walls_unconfirmed_in_3d") or []):
        red_ids.append({"llm_id": w["llm_id"], "color": "red"})
    for f in (validation.get("floors", {}).get("floors_unconfirmed_in_3d") or []):
        red_ids.append({"llm_id": f["llm_id"], "color": "red"})
    for f in (validation.get("floors", {}).get("floors_partial_extent") or []):
        red_ids.append({"llm_id": f["llm_id"], "color": "red"})  # extent_partial = aussi suspect
    for c in (validation.get("columns", {}).get("columns_unconfirmed_in_3d") or []):
        red_ids.append({"llm_id": c["llm_id"], "color": "red"})
    for o in (validation.get("openings", {}).get("openings_unconfirmed_in_3d") or []):
        red_ids.append({"llm_id": o["llm_id"], "color": "red"})

    if flag_no_evidence:
        for w in (validation.get("walls", {}).get("walls_no_3d_evidence") or []):
            yellow_ids.append({"llm_id": w["llm_id"], "color": "yellow"})
        for f in (validation.get("floors", {}).get("floors_no_crossings") or []):
            yellow_ids.append({"llm_id": f["llm_id"], "color": "yellow"})
        for c in (validation.get("columns", {}).get("columns_no_crossings") or []):
            yellow_ids.append({"llm_id": c["llm_id"], "color": "yellow"})
        for o in (validation.get("openings", {}).get("openings_no_3d_evidence") or []):
            yellow_ids.append({"llm_id": o["llm_id"], "color": "yellow"})

    items = red_ids + yellow_ids
    if not items:
        return {
            "ok": True, "view_revit_id": None,
            "red_count": 0, "yellow_count": 0, "total_flagged": 0,
            "validation": validation,
            "note": "Aucun élément suspect — rien à flagger.",
        }

    # 3. Apply overrides via views_override_element_colors_many.
    from .views import override_element_colors_many as _override
    result = _override(kg=kg, doc=doc, items=items, view_ref=view_ref)

    note = (
        "{} élément(s) en rouge (suspects fantômes) + {} en jaune (sans "
        "évidence 3D) appliqué(s) dans la vue {}. {} skipped.".format(
            len(red_ids), len(yellow_ids),
            result.get("view_revit_id"),
            result.get("skipped_count"),
        )
    )

    return {
        "ok": True,
        "view_revit_id": result.get("view_revit_id"),
        "red_count": len(red_ids),
        "yellow_count": len(yellow_ids),
        "total_flagged": result.get("applied_count"),
        "skipped_count": result.get("skipped_count"),
        "validation": validation,
        "note": note,
    }


# ----- 9. Audit d'intégrité du plan set (gate Phase 1 / Phase 2) --------
#
# Tool d'audit holistique du dossier DXF. Vit en **étape 1 du flow
# d'import**, AVANT toute proposition de modif au modèle Revit
# (création de niveaux, de murs, etc.). User 2026-05-13 : « un audit
# d'intégrité du plan set est livré en 1, avant de proposer des
# changements dans le modèle, notamment les niveaux ».
#
# 4 checks agrégés :
# - source_consistency : tous les fichiers ont la même convention layers ?
# - scale_drift : drift d'échelle plan↔coupes par trait assigné
# - levels_consistency : les coupes déclarent-elles le même jeu de niveaux ?
# - walls_reconciliation : épaisseurs cohérentes plan↔coupes ?
# - openings_matching : ouvertures matchées plan↔coupes ?
#
# Hard gate : si severity=errors, `ok=False`. L'agent ne doit PAS
# enchaîner avec un tool qui mute le modèle (levels_create_many,
# walls_create_many, etc.) tant que les erreurs ne sont pas résolues.


def _level_to_dict_short(lv: dwg_section_reader.Level) -> Dict[str, Any]:
    return {"name": lv.name, "elevation_m": round(lv.elevation_m, 4)}


@tool(name="check_planset_integrity", tier=2)
def check_planset_integrity(
    kg: ProjectKG,
    directory: str,
    scale_override: Optional[float] = None,
    layer_mapping: Optional[Dict[str, str]] = None,
    thickness_tol_m: float = 0.02,
    x_cut_tol_m: float = 0.10,
    elevation_tol_m: float = 0.01,
) -> Dict[str, Any]:
    """Audit holistique d'intégrité d'un dossier DXF (plans/coupes/élévations).

    **Étape 1 du flow d'import** — à appeler AVANT toute proposition de
    modif au modèle Revit (`levels_create_many`, `walls_create_many`,
    `views_create_section_many`, etc.). Si `gate_status == "abort"`,
    l'agent doit stopper et présenter les errors à l'user pour
    résolution (export DXF à corriger, swap de coupes, etc.).

    Checks agrégés :

    1. **source_consistency** : tous les fichiers utilisent-ils la même
       convention layers (AIA / ISO / other) ?
    2. **scale_drift** : pour chaque coupe assignée à un trait, drift
       entre longueur du trait et extent A-WALL de la coupe.
    3. **levels_consistency** : les coupes déclarent-elles le même
       jeu de niveaux (élévations cohérentes à `elevation_tol_m` près) ?
    4. **walls_reconciliation** : recoupement épaisseurs plan↔coupes
       (utilise `dwg_coherence.reconcile_plan_section_walls`).
    5. **openings_matching** : ouvertures matchées plan↔coupes via
       block_id partagé.

    Severity hierarchy : `clean` < `warnings` < `errors`. Gate :
    - `pass` (clean) : enchaîner sans réserve.
    - `needs_user` (warnings) : présenter à l'user via
      `ui_confirm_choices` avant de continuer.
    - `abort` (errors) : `ok=False`, stopper et présenter les errors.

    Concepts: audit, intégrité, integrity, cohérence, coherence, plan set,
              dossier, projet, gate, vérification, phase 1, phase 2,
              source, échelle, scale, drift, niveaux, levels, walls,
              ouvertures, openings
    Phrases: "audit du dossier", "vérifie l'intégrité du projet",
             "check planset", "is the plan set consistent",
             "phase 2 audit"
    Similar: dwg_inspect_sections, dwg_reconcile_plan_section_walls,
             dwg_identify_source, dwg_verify_section_scale,
             dwg_find_section_markers

    Args:
        directory: chemin du dossier contenant les `.dxf` du projet.
        scale_override: facteur m-per-dxf-unit additionnel (cf.
            `dwg_inspect`).
        layer_mapping: pour le classify walls plan. Défaut
            `{"A-WALL": "wall"}`.
        thickness_tol_m: tolérance d'épaisseur pour match (défaut 2cm).
        x_cut_tol_m: tolérance de position le long du cut (défaut 10cm).
        elevation_tol_m: tolérance pour levels_consistency (défaut 1cm).

    Returns:
        {"ok": bool, "severity": "clean"|"warnings"|"errors",
         "gate_status": "pass"|"needs_user"|"abort",
         "files_summary": {plan_count, section_count, elevation_count,
                            unknown_count, files: [{path, name, kind,
                            source}, …]},
         "checks": {check_name: {severity, summary, issues}, ...},
         "errors": [...], "warnings": [...], "note": str}
    """
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        raise FileNotFoundError("Directory not found: {}".format(dir_path))

    dxf_files = sorted(dir_path.glob("*.dxf"))
    if not dxf_files:
        raise ValueError("No .dxf files in directory {}".format(dir_path))

    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}

    # --- 1. Parse + classify + identify_source pour chaque fichier ------
    parsed: Dict[str, Dict[str, Any]] = {}
    plan_paths: List[str] = []
    section_paths: List[str] = []
    elevation_paths: List[str] = []
    unknown_paths: List[str] = []
    source_per_file: Dict[str, str] = {}

    for fp in dxf_files:
        entities, meta = dwg_reader.parse(fp, scale_override=scale_override)
        kind, evidence = dwg_section_reader.classify_dxf(
            meta["layers"], file_name=fp.name,
        )
        src = dwg_section_reader.identify_source(meta["layers"])
        path_str = str(fp)
        parsed[path_str] = {
            "entities": entities,
            "meta": meta,
            "kind": kind,
            "evidence": evidence,
            "name": fp.name,
            "source": src["source"],
            "source_confidence": src["confidence"],
        }
        source_per_file[path_str] = src["source"]
        if kind == "plan":
            plan_paths.append(path_str)
        elif kind == "section":
            section_paths.append(path_str)
        elif kind == "elevation":
            elevation_paths.append(path_str)
        else:
            unknown_paths.append(path_str)

    files_summary: Dict[str, Any] = {
        "plan_count": len(plan_paths),
        "section_count": len(section_paths),
        "elevation_count": len(elevation_paths),
        "unknown_count": len(unknown_paths),
        "files": [
            {
                "path": p,
                "name": parsed[p]["name"],
                "kind": parsed[p]["kind"],
                "source": parsed[p]["source"],
            }
            for p in (
                plan_paths + section_paths + elevation_paths + unknown_paths
            )
        ],
    }

    # Setup : pas de plan = erreur bloquante (rien à valider). Pas de
    # coupe = warning (l'utilisateur peut continuer sans cross-check
    # plan↔coupes, c'est dégradé mais pas bloquant). Unknown files =
    # warning (à signaler mais non bloquant).
    setup_errors: List[Dict[str, Any]] = []
    setup_warnings: List[Dict[str, Any]] = []
    if not plan_paths:
        setup_errors.append({
            "kind": "no_plan_detected",
            "message": (
                "Aucun fichier DXF classé `plan` dans le dossier. Un "
                "audit de plan set nécessite au moins 1 plan d'étage "
                "pour proposer une création de modèle."
            ),
        })
    if not section_paths:
        setup_warnings.append({
            "kind": "no_section_detected",
            "message": (
                "Aucun fichier DXF classé `section` dans le dossier. "
                "Audit dégradé : pas de cross-check plan↔coupes "
                "(épaisseurs, niveaux, ouvertures depuis coupes). La "
                "création modèle reste possible depuis le plan seul "
                "mais sans validation des hauteurs / sill-head."
            ),
        })
    elif len(section_paths) == 1:
        setup_warnings.append({
            "kind": "single_section_only",
            "message": (
                "1 seule coupe disponible. Cohérence inter-coupes (jeu "
                "de niveaux uniforme) non vérifiable. Recoupement walls "
                "et openings limités à cette coupe."
            ),
        })

    # Élévations : tout manque par rapport à la situation idéale (4
    # façades cardinales) est un état dégradé → warning. User 2026-05-13.
    if not elevation_paths:
        setup_warnings.append({
            "kind": "no_elevation_detected",
            "message": (
                "Aucune élévation détectée. Vue 3D possible depuis "
                "plans+coupes uniquement, mais la cross-validation des "
                "façades (positions et hauteurs d'ouvertures vues de "
                "l'extérieur) ne sera pas faite."
            ),
        })
    else:
        # Cardinaux observés (Nord/Sud/Est/Ouest) — extraits de l'evidence
        # de classify_dxf.
        directions_seen = {
            parsed[p]["evidence"].get("direction")
            for p in elevation_paths
        }
        directions_seen.discard(None)
        missing = sorted(
            {"Nord", "Sud", "Est", "Ouest"} - directions_seen
        )
        if missing:
            setup_warnings.append({
                "kind": "incomplete_elevations",
                "directions_present": sorted(directions_seen),
                "directions_missing": missing,
                "message": (
                    "Élévations incomplètes : {} présente(s), {} "
                    "manquante(s). Cross-validation des façades partielle.".format(
                        sorted(directions_seen), missing,
                    )
                ),
            })

    # Fichiers `unknown` (ni plan, ni section, ni élévation) : ignorés
    # silencieusement de l'audit, juste comptés dans `files_summary`. Pas
    # de warning — user 2026-05-13 : « ignore unknown files ».

    # Pas de plan = abort sans tenter le reste.
    if not plan_paths:
        return {
            "ok": False,
            "severity": "errors",
            "gate_status": "abort",
            "files_summary": files_summary,
            "checks": {},
            "errors": [{"check": "setup", **e} for e in setup_errors],
            "warnings": [{"check": "setup", **w} for w in setup_warnings],
            "note": (
                "Audit interrompu : aucun plan d'étage détecté. Sans "
                "plan, impossible de proposer une création de modèle. "
                "Vérifier le dossier et ré-exporter au besoin."
            ),
        }

    # --- 2. Check source consistency ------------------------------------
    source_check = dwg_coherence.check_source_consistency(source_per_file)

    # --- 3. Récupérer ou recalculer les section_lines -------------------
    from .dxf_context import _find_live_context
    section_lines: List[Dict[str, Any]] = []
    nid = _find_live_context(kg)
    if nid is not None:
        ctx_node = kg.get_node(nid)
        section_lines = list(ctx_node.get("section_lines", []))

    # Si pas de section_lines en KG, les calculer pour pouvoir checker
    # scale + walls. On utilise le 1er plan détecté.
    primary_plan = plan_paths[0]
    if not section_lines:
        plan_entities = parsed[primary_plan]["entities"]
        markers = dwg_section_reader.find_section_markers(plan_entities)
        section_markers_only = [m for m in markers if m.kind == "section"]
        # Match coupes ↔ traits (brute force).
        if section_markers_only and len(section_markers_only) == len(section_paths):
            import itertools
            coupe_extents: List[Tuple[str, float]] = []
            for cp in section_paths:
                ents = parsed[cp]["entities"]
                ext = _building_extent_from_entities(ents) or 0.0
                coupe_extents.append((cp, ext))
            best_perm = None
            best_drift = float("inf")
            for perm in itertools.permutations(range(len(section_markers_only))):
                total = sum(
                    abs(coupe_extents[ci][1] - section_markers_only[mi].length_m)
                    for ci, mi in enumerate(perm)
                )
                if total < best_drift:
                    best_drift = total
                    best_perm = perm
            if best_perm is not None:
                for ci, mi in enumerate(best_perm):
                    mk = section_markers_only[mi]
                    view_dir = mk.inferred_view_dir or (
                        mk.view_dir_candidates[0]
                        if mk.view_dir_candidates else "up"
                    )
                    section_lines.append({
                        "coupe_path": coupe_extents[ci][0],
                        "plan_p1": list(mk.p1_m),
                        "plan_p2": list(mk.p2_m),
                        "view_dir": view_dir,
                        "name": parsed[coupe_extents[ci][0]]["name"],
                    })

    # --- 4. Scale drift par trait↔coupe ---------------------------------
    scale_per_coupe: List[Dict[str, Any]] = []
    for sl in section_lines:
        coupe_path = sl.get("coupe_path")
        if not coupe_path or coupe_path not in parsed:
            continue
        p1 = sl.get("plan_p1") or [0, 0]
        p2 = sl.get("plan_p2") or [0, 0]
        marker_length = (
            (p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2
        ) ** 0.5
        ents = parsed[coupe_path]["entities"]
        extent = _building_extent_from_entities(ents) or 0.0
        drift_m = abs(marker_length - extent)
        denom = max(marker_length, extent, 1e-6)
        drift_pct = 100.0 * drift_m / denom
        scale_per_coupe.append({
            "coupe_path": coupe_path,
            "section_line_name": sl.get("name"),
            "marker_length_m": round(marker_length, 4),
            "coupe_extent_m": round(extent, 4),
            "drift_pct": round(drift_pct, 2),
            "drift_m": round(drift_m, 4),
        })

    scale_check = dwg_coherence.check_scale_drift(scale_per_coupe)

    # --- 5. Levels consistency entre coupes -----------------------------
    levels_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    section_openings_by_coupe: Dict[str, List[dwg_section_reader.SectionOpening]] = {}
    section_walls_by_coupe: Dict[str, List[Dict[str, Any]]] = {}
    for cp in section_paths:
        ents = parsed[cp]["entities"]
        levels = dwg_section_reader.read_levels(ents)
        levels_by_coupe[cp] = [_level_to_dict_short(lv) for lv in levels]
        section_openings_by_coupe[cp] = dwg_section_reader.read_section_openings(ents)
        sec_walls = dwg_section_reader.read_section_walls(ents)
        section_walls_by_coupe[cp] = [
            _section_wall_to_dict(sw) for sw in sec_walls
        ]

    levels_check = dwg_coherence.check_levels_consistency_between_coupes(
        levels_by_coupe, elevation_tol_m=elevation_tol_m,
    )

    # --- 6. Walls reconciliation (plan ↔ coupes) ------------------------
    plan_entities = parsed[primary_plan]["entities"]
    plan_classified = dwg_classifier.classify(
        plan_entities, layer_mapping,
    )
    plan_walls_dict = [_wall_candidate_to_dict(w) for w in plan_classified.walls]

    walls_reconcil = dwg_coherence.reconcile_plan_section_walls(
        plan_walls=plan_walls_dict,
        section_lines=section_lines,
        section_walls_by_coupe=section_walls_by_coupe,
        thickness_tol_m=thickness_tol_m,
        x_cut_tol_m=x_cut_tol_m,
    )
    walls_check = dwg_coherence.walls_reconciliation_to_check(
        walls_reconcil, thickness_tol_m=thickness_tol_m,
    )

    # --- 7. Openings matching plan ↔ coupes -----------------------------
    plan_openings_objs = dwg_section_reader.read_section_openings(plan_entities)
    openings_reports: List[Dict[str, Any]] = []
    for cp in section_paths:
        sec_openings = section_openings_by_coupe[cp]
        matches, unmatched_sec, unmatched_plan = (
            dwg_section_reader.match_openings(plan_openings_objs, sec_openings)
        )
        openings_reports.append({
            "section_path": cp,
            "section_name": parsed[cp]["name"],
            "match_count": len(matches),
            "unmatched_section_count": len(unmatched_sec),
            "unmatched_plan_count": len(unmatched_plan),
        })

    openings_check = dwg_coherence.check_openings_matching(openings_reports)

    # --- 7b. Openings plan ↔ élévations (cross-val non positionnelle) ---
    # User 2026-05-13 : un opening présent en plan doit aussi apparaître
    # en élévation, et inversement. Phase 1 fait le check de comptage et
    # de présence par block_id ; Phase 2b complète avec le matching
    # positionnel précis (qui dépend de l'orientation des murs).
    plan_block_ids_all: List[str] = []
    plan_total_inserts = 0
    for pp in plan_paths:
        plan_ents = parsed[pp]["entities"]
        for po in dwg_section_reader.read_plan_opening_inserts(plan_ents):
            plan_total_inserts += 1
            if po.block_id:
                plan_block_ids_all.append(po.block_id)
    elev_block_ids_all: List[str] = []
    elev_total_inserts = 0
    for ep in elevation_paths:
        elev_ents = parsed[ep]["entities"]
        for eo in dwg_section_reader.read_section_openings(elev_ents):
            elev_total_inserts += 1
            if eo.block_id:
                elev_block_ids_all.append(eo.block_id)
    openings_pv_check = dwg_coherence.check_openings_plan_vs_elevation(
        plan_block_ids=plan_block_ids_all,
        elevation_block_ids=elev_block_ids_all,
        plan_total_inserts=plan_total_inserts,
        elevation_total_inserts=elev_total_inserts,
    )

    # --- 8. Agrégation + gate -------------------------------------------
    # Setup check : remonte les warnings de structure (pas de coupe,
    # 1 seule coupe, unknown files). Severity = warnings si non vide.
    setup_check = dwg_coherence.IntegrityCheck(
        name="setup",
        severity="warnings" if setup_warnings else "clean",
        summary={
            "warnings_count": len(setup_warnings),
            "plan_count": len(plan_paths),
            "section_count": len(section_paths),
            "elevation_count": len(elevation_paths),
            "unknown_count": len(unknown_paths),
        },
        issues=list(setup_warnings),
    )
    checks = [
        setup_check, source_check, scale_check, levels_check,
        walls_check, openings_check, openings_pv_check,
    ]
    report = dwg_coherence.aggregate_planset_integrity(
        checks, files_summary,
    )

    note = _build_planset_integrity_note(report)

    # Serialise checks (dataclass → dict).
    checks_serialized: Dict[str, Any] = {}
    for c in checks:
        checks_serialized[c.name] = {
            "severity": c.severity,
            "summary": c.summary,
            "issues": c.issues,
        }

    return {
        "ok": report.ok,
        "severity": report.severity,
        "gate_status": report.gate_status,
        "files_summary": report.files_summary,
        "checks": checks_serialized,
        "errors": report.errors,
        "warnings": report.warnings,
        "note": note,
    }


# ----- 10. Phase 2 : extraction épaisseurs + import typed --------------


@tool(name="dwg_extract_wall_thicknesses", tier=2)
def extract_wall_thicknesses(
    kg: ProjectKG,
    file_path: str,
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    bucket_cm: int = 1,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
) -> Dict[str, Any]:
    """Preview des épaisseurs uniques observées dans un plan DXF (Phase 2.2).

    Classifie le plan (paires parallèles + centerline) et agrège les
    épaisseurs par bucket cm. Sort une distribution `{cm: count}` +
    suggested type names (`DXF_WALL_<cm>cm`). Read-only — aucune
    création.

    Use case : avant de lancer `dwg_import_walls_typed`, l'agent
    présente à l'user la distribution pour validation. Si une bucket
    paraît suspect (1 mur à 5cm = artefact ? mur cloison ?), l'user
    peut ajuster `bucket_cm` ou les bornes avant l'import.

    Concepts: dxf, dwg, plan, épaisseur, thickness, distribution, preview,
              phase 2, walltype, custom
    Phrases: "quelles épaisseurs de murs dans ce plan",
             "preview des types DXF à créer", "distribution des murs"
    Similar: dwg_classify, dwg_import_walls_typed,
             walls_get_or_create_dxf_type_many

    Args:
        file_path: chemin du DXF plan.
        layer_mapping: défaut `{"A-WALL": "wall"}`.
        scale_override: cf. `dwg_inspect`.
        bucket_cm: granularité bucketing (défaut 1cm).
        min_thickness_m / max_thickness_m: bornes (défaut 5cm / 60cm).
        include_centerline: passe centerline du classifier (défaut True).

    Returns:
        {"ok": bool, "walls_count": int,
         "thickness_buckets": [{cm: int, count: int, type_name: str,
                                 wall_indices: [...]}, ...],
         "rejected_count": int}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}
    if bucket_cm < 1:
        raise ValueError("bucket_cm must be >= 1")

    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    classified = dwg_classifier.classify(
        entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        include_centerline=include_centerline,
    )

    # Bucket par cm.
    by_bucket: Dict[int, Dict[str, Any]] = {}
    for i, w in enumerate(classified.walls):
        cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
        bucket = by_bucket.setdefault(cm, {
            "cm": cm,
            "count": 0,
            "type_name": "DXF_WALL_{}cm".format(cm),
            "wall_indices": [],
        })
        bucket["count"] += 1
        bucket["wall_indices"].append(i)

    buckets_sorted = sorted(by_bucket.values(), key=lambda b: b["cm"])
    return {
        "ok": True,
        "walls_count": len(classified.walls),
        "thickness_buckets": buckets_sorted,
        "rejected_count": len(classified.rejected),
        "bucket_cm": bucket_cm,
    }


def _extract_wall_thicknesses_one(
    file_path: str,
    layer_mapping: Dict[str, str],
    scale_override: Optional[float],
    bucket_cm: int,
    min_thickness_m: float,
    max_thickness_m: float,
    include_centerline: bool,
) -> Dict[str, Any]:
    """Helper privé — extraction pour 1 plan. Réutilisé par le tool
    unitaire et le bulk.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    classified = dwg_classifier.classify(
        entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        include_centerline=include_centerline,
    )
    by_bucket: Dict[int, Dict[str, Any]] = {}
    for i, w in enumerate(classified.walls):
        cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
        bucket = by_bucket.setdefault(cm, {
            "cm": cm,
            "count": 0,
            "type_name": "DXF_WALL_{}cm".format(cm),
            "wall_indices": [],
        })
        bucket["count"] += 1
        bucket["wall_indices"].append(i)
    return {
        "file_path": str(path),
        "walls_count": len(classified.walls),
        "thickness_buckets": sorted(by_bucket.values(), key=lambda b: b["cm"]),
        "rejected_count": len(classified.rejected),
    }


@tool(name="dwg_extract_wall_thicknesses_many", tier=2)
def extract_wall_thicknesses_many(
    kg: ProjectKG,
    file_paths: List[str],
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    bucket_cm: int = 1,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
) -> Dict[str, Any]:
    """Extract épaisseurs pour N plans en **un seul** appel.

    Pattern bulk : pour un projet multi-étages (P7 a 2 plans), l'agent
    appelle ce tool 1× au lieu de N. Économie : 1 round-trip API au
    lieu de N. Le résultat inclut une **distribution globale dédupliquée**
    pour faciliter `dwg_import_walls_typed_many` derrière.

    Concepts: dxf, dwg, plans, épaisseurs, distribution, bulk, batch,
              multi-étages, preview, phase 2
    Phrases: "preview les épaisseurs de tous les plans",
             "extract thicknesses many"
    Similar: dwg_extract_wall_thicknesses, dwg_import_walls_typed_many

    Args:
        file_paths: liste de chemins DXF plan (≥ 1).
        layer_mapping / scale_override / bucket_cm / min_thickness_m /
            max_thickness_m / include_centerline: cf.
            `dwg_extract_wall_thicknesses`. Appliqués uniformément à
            tous les plans.

    Returns:
        {"ok": bool, "per_file": [{file_path, walls_count,
            thickness_buckets, rejected_count}, ...],
         "global_distribution": [{cm, total_count, files_count,
            type_name}, ...],
         "distinct_buckets_cm": [cm, cm, ...]}
    """
    if not isinstance(file_paths, list) or not file_paths:
        raise ValueError("file_paths must be a non-empty list")
    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}
    if bucket_cm < 1:
        raise ValueError("bucket_cm must be >= 1")

    per_file: List[Dict[str, Any]] = []
    global_buckets: Dict[int, Dict[str, Any]] = {}
    for fp in file_paths:
        rec = _extract_wall_thicknesses_one(
            fp, layer_mapping, scale_override, bucket_cm,
            min_thickness_m, max_thickness_m, include_centerline,
        )
        per_file.append(rec)
        for b in rec["thickness_buckets"]:
            cm = b["cm"]
            gb = global_buckets.setdefault(cm, {
                "cm": cm,
                "total_count": 0,
                "files_count": 0,
                "type_name": "DXF_WALL_{}cm".format(cm),
            })
            gb["total_count"] += b["count"]
            gb["files_count"] += 1

    global_dist = sorted(global_buckets.values(), key=lambda b: b["cm"])
    return {
        "ok": True,
        "per_file": per_file,
        "global_distribution": global_dist,
        "distinct_buckets_cm": [b["cm"] for b in global_dist],
        "bucket_cm": bucket_cm,
    }


@tool(name="dwg_import_walls_typed", tier=2)
def import_walls_typed(
    kg: ProjectKG,
    doc: Any,
    file_path: str,
    level_ref: str,
    height_m: Optional[float] = None,
    dx_m: float = 0.0,
    dy_m: float = 0.0,
    bucket_cm: int = 1,
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
    base_type_ref: Optional[str] = None,
    max_walls: int = 500,
) -> Dict[str, Any]:
    """Importe les murs d'un DXF en mappant chaque épaisseur à son type
    custom `DXF_WALL_<cm>cm` (Phase 2.3 + 2.4).

    Variante typée de `dwg_import_walls` : au lieu d'utiliser un seul
    `wall_type_ref` pour tous les murs, le tool crée (ou réutilise) un
    WallType custom par bucket d'épaisseur observé dans le plan, puis
    assigne chaque mur à son type.

    **Atomicité** : 2 transactions Revit séparées (1 pour les types via
    `walls_get_or_create_dxf_type_many`, 1 pour les murs via
    `walls_create_many`). Si la création des murs échoue, les types
    déjà créés restent et seront réutilisés au prochain run (idempotent).

    Concepts: dxf, dwg, import, mur, walltype, custom, typed, phase 2,
              épaisseur, batch
    Phrases: "importe les murs du plan avec types DXF",
             "crée les murs typés depuis ce DXF",
             "phase 2 import walls"
    Similar: dwg_extract_wall_thicknesses, dwg_import_walls,
             walls_get_or_create_dxf_type_many, walls_create_many

    Args:
        file_path: chemin du DXF plan.
        level_ref: llm_id du Level cible.
        height_m: hauteur uniforme en m (None = hauteur d'étage).
        dx_m / dy_m: translation pour aligner DXF↔Revit (défaut 0).
        bucket_cm: granularité bucketing épaisseurs (défaut 1cm).
        layer_mapping: défaut `{"A-WALL": "wall"}`.
        scale_override: facteur additionnel $INSUNITS.
        min_thickness_m / max_thickness_m: bornes (défaut 5/60cm).
        include_centerline: passe centerline (défaut True).
        base_type_ref: WallType template à dupliquer. None = auto-find.
        max_walls: garde-fou (défaut 500).

    Returns:
        {"ok": bool, "walls_imported": int, "types_created": int,
         "types_reused": int, "thickness_distribution": {cm: count, ...},
         "types": [...], "inner_walls": <walls_create_many response>}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    _refuse_if_section(path)
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}

    # --- 1. Classify plan -----------------------------------------------
    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    classified = dwg_classifier.classify(
        entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
        include_centerline=include_centerline,
    )
    if len(classified.walls) > max_walls:
        raise ValueError(
            "Classify produced {} walls (> max_walls={}). Refine layer_mapping "
            "or raise max_walls explicitly.".format(
                len(classified.walls), max_walls,
            )
        )
    if not classified.walls:
        return {
            "ok": True,
            "walls_imported": 0,
            "types_created": 0,
            "types_reused": 0,
            "thickness_distribution": {},
            "types": [],
            "inner_walls": None,
            "note": "No wall candidates detected.",
        }

    # --- 2. Bucket thicknesses + get_or_create types --------------------
    unique_thicknesses_m: List[float] = []
    seen_buckets: Set[int] = set()
    for w in classified.walls:
        cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
        if cm not in seen_buckets:
            seen_buckets.add(cm)
            unique_thicknesses_m.append(cm / 100.0)

    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    type_entry = registry.get("walls_get_or_create_dxf_type_many")
    if type_entry is None:
        raise RuntimeError(
            "walls_get_or_create_dxf_type_many not in registry — bug?"
        )
    types_result = type_entry.fn(
        kg=kg, doc=doc,
        thicknesses_m=unique_thicknesses_m,
        bucket_cm=bucket_cm,
        base_type_ref=base_type_ref,
    )

    # Mapping bucket_cm → wall_type_ref (llm_id).
    type_ref_by_cm: Dict[int, str] = {}
    for entry in types_result["types"]:
        cm = int(round(entry["thickness_m"] * 100))
        type_ref_by_cm[cm] = entry["llm_id"]

    # --- 3. Build items + walls_create_many -----------------------------
    items: List[Dict[str, Any]] = []
    thickness_dist: Dict[int, int] = {}
    for w in classified.walls:
        cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
        wall_type_ref = type_ref_by_cm.get(cm)
        if wall_type_ref is None:
            raise RuntimeError(
                "Bucket {}cm has no associated wall_type_ref — bug in "
                "get_or_create_dxf_type_many?".format(cm)
            )
        item: Dict[str, Any] = {
            "level_ref": level_ref,
            "wall_type_ref": wall_type_ref,
            "p1": [w.p1[0] + dx_m, w.p1[1] + dy_m],
            "p2": [w.p2[0] + dx_m, w.p2[1] + dy_m],
        }
        if height_m is not None:
            item["height"] = float(height_m)
        items.append(item)
        thickness_dist[cm] = thickness_dist.get(cm, 0) + 1

    walls_entry = registry.get("walls_create_many")
    inner = walls_entry.fn(kg=kg, doc=doc, items=items)

    return {
        "ok": True,
        "walls_imported": len(items),
        "types_created": types_result["created_count"],
        "types_reused": types_result["reused_count"],
        "thickness_distribution": {
            "{}cm".format(cm): count for cm, count in sorted(thickness_dist.items())
        },
        "types": types_result["types"],
        "inner_walls": inner,
    }


# Tool DEPRECATED tier=3 — remplacé par `dwg_create_continuous_walls_many`
# (Phase 2a) qui inclut fusion via vote élévation + score 3D suspects.
@tool(name="dwg_import_walls_typed_many", tier=3)
def import_walls_typed_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    bucket_cm: int = 1,
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
    base_type_ref: Optional[str] = None,
    max_walls_per_file: int = 500,
) -> Dict[str, Any]:
    """Import typed pour N plans en bulk (Phase 2.4 bulk).

    Pour un projet multi-étages, l'agent passe `items=[{file_path,
    level_ref, height_m?, dx_m?, dy_m?}, ...]` et le tool :

    1. Classifie chaque plan → WallCandidates per file.
    2. **Dédup global** des buckets d'épaisseur entre tous les plans
       (un mur de 20cm dans N0 et N1 → 1 seul WallType `DXF_WALL_20cm`
       partagé).
    3. Crée tous les types manquants en **1 seule** Tx Revit
       (`walls_get_or_create_dxf_type_many`).
    4. Crée tous les murs (tous plans confondus) en **1 seule** Tx Revit
       (`walls_create_many`).

    Gain vs N appels `dwg_import_walls_typed` : 2 Tx Revit au lieu de
    2N, 1 round-trip API au lieu de N, et factorisation des types
    entre niveaux.

    Concepts: dxf, dwg, import, bulk, batch, mur, walltype, custom,
              typed, phase 2, multi-étages, plusieurs plans
    Phrases: "importe les murs de tous les plans avec types DXF",
             "import walls typed many", "phase 2 bulk import"
    Similar: dwg_import_walls_typed, dwg_extract_wall_thicknesses_many,
             walls_get_or_create_dxf_type_many, walls_create_many

    Args:
        items: liste de dicts `{file_path, level_ref, height_m?,
            dx_m?, dy_m?}`. Au moins un item.
        bucket_cm / layer_mapping / scale_override / min_thickness_m /
            max_thickness_m / include_centerline / base_type_ref:
            appliqués uniformément à tous les plans.
        max_walls_per_file: garde-fou par fichier (défaut 500).

    Returns:
        {"ok": bool, "files_count": int,
         "walls_imported_total": int, "walls_per_file": {path: count},
         "types_created": int, "types_reused": int,
         "thickness_distribution_global": {cm: count, ...},
         "types": [...], "inner_walls": <walls_create_many response>}
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}

    # V3.3 : ensure shared param `claude-in-revit:llm_id` est bound
    # avant les créations (cf. `dwg_create_continuous_walls_many`).
    if doc is not None:
        try:
            from .. import revit_primitives as rp
            rp.ensure_shared_param_binding(doc)
        except Exception:  # noqa: BLE001
            pass

    # --- 1. Pre-validate items + parse + classify each plan -------------
    parsed_per_file: List[Tuple[Dict[str, Any], List[Any]]] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError("items[{}] must be a dict".format(i))
        fp = it.get("file_path")
        level_ref = it.get("level_ref")
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("items[{}]: file_path required".format(i))
        if not isinstance(level_ref, str) or not kg.has_node(level_ref):
            raise ValueError(
                "items[{}]: unknown level_ref {!r}".format(i, level_ref)
            )
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError(
                "items[{}]: file not found: {}".format(i, path)
            )
        _refuse_if_section(path)

        entities, _ = dwg_reader.parse(path, scale_override=scale_override)
        classified = dwg_classifier.classify(
            entities, layer_mapping,
            min_thickness_m=min_thickness_m,
            max_thickness_m=max_thickness_m,
            include_centerline=include_centerline,
        )
        if len(classified.walls) > max_walls_per_file:
            raise ValueError(
                "items[{}] ({}): {} walls > max_walls_per_file={}".format(
                    i, path.name, len(classified.walls), max_walls_per_file,
                )
            )
        parsed_per_file.append((it, classified.walls))

    # --- 2. Dédup global des buckets ------------------------------------
    seen_buckets: Set[int] = set()
    unique_thicknesses_m: List[float] = []
    for _it, walls in parsed_per_file:
        for w in walls:
            cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
            if cm not in seen_buckets:
                seen_buckets.add(cm)
                unique_thicknesses_m.append(cm / 100.0)

    if not unique_thicknesses_m:
        return {
            "ok": True,
            "files_count": len(items),
            "walls_imported_total": 0,
            "walls_per_file": {it["file_path"]: 0 for it, _ in parsed_per_file},
            "types_created": 0,
            "types_reused": 0,
            "thickness_distribution_global": {},
            "types": [],
            "inner_walls": None,
            "note": "No wall candidates detected in any plan.",
        }

    # --- 3. get_or_create_dxf_type_many (1 Tx Revit) --------------------
    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    type_entry = registry.get("walls_get_or_create_dxf_type_many")
    types_result = type_entry.fn(
        kg=kg, doc=doc,
        thicknesses_m=unique_thicknesses_m,
        bucket_cm=bucket_cm,
        base_type_ref=base_type_ref,
    )
    type_ref_by_cm: Dict[int, str] = {}
    for entry in types_result["types"]:
        cm = int(round(entry["thickness_m"] * 100))
        type_ref_by_cm[cm] = entry["llm_id"]

    # --- 4. Build items pour walls_create_many global -------------------
    all_wall_items: List[Dict[str, Any]] = []
    walls_per_file: Dict[str, int] = {}
    thickness_dist_global: Dict[int, int] = {}
    for plan_item, walls in parsed_per_file:
        fp = plan_item["file_path"]
        level_ref = plan_item["level_ref"]
        dx_m = float(plan_item.get("dx_m", 0.0))
        dy_m = float(plan_item.get("dy_m", 0.0))
        height_m = plan_item.get("height_m")
        per_file_count = 0
        for w in walls:
            cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
            wall_type_ref = type_ref_by_cm.get(cm)
            if wall_type_ref is None:
                raise RuntimeError(
                    "Bucket {}cm not in type_ref_by_cm — get_or_create bug?".format(cm)
                )
            wall_item: Dict[str, Any] = {
                "level_ref": level_ref,
                "wall_type_ref": wall_type_ref,
                "p1": [w.p1[0] + dx_m, w.p1[1] + dy_m],
                "p2": [w.p2[0] + dx_m, w.p2[1] + dy_m],
            }
            if height_m is not None:
                wall_item["height"] = float(height_m)
            all_wall_items.append(wall_item)
            per_file_count += 1
            thickness_dist_global[cm] = thickness_dist_global.get(cm, 0) + 1
        walls_per_file[fp] = per_file_count

    # --- 5. walls_create_many global (1 Tx Revit) -----------------------
    walls_entry = registry.get("walls_create_many")
    inner = walls_entry.fn(kg=kg, doc=doc, items=all_wall_items)

    return {
        "ok": True,
        "files_count": len(items),
        "walls_imported_total": len(all_wall_items),
        "walls_per_file": walls_per_file,
        "types_created": types_result["created_count"],
        "types_reused": types_result["reused_count"],
        "thickness_distribution_global": {
            "{}cm".format(cm): count
            for cm, count in sorted(thickness_dist_global.items())
        },
        "types": types_result["types"],
        "inner_walls": inner,
    }


@tool(name="dwg_add_openings_to_walls_many", tier=2)
def add_openings_to_walls_many(
    kg: ProjectKG,
    doc: Any,
    scale_override: Optional[float] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    door_family_type_ref: Optional[str] = None,
    window_family_type_ref: Optional[str] = None,
    perp_tol_m: float = 0.30,
) -> Dict[str, Any]:
    """Phase 2b — Ajoute fenêtres/portes sur murs existants du KG via
    énumération depuis le plan + vote orientation par élévation.

    Sources de vérité (user 2026-05-13) :
    - **Localisation + nombre** : plan (énumération INSERT A-GLAZ).
    - **Largeur** : plan (bbox bloc, dim longue).
    - **Hauteur** : élévation (bbox bloc), fallback coupe.
    - **Sill (allège)** : coupe (lookup par block_id, niveau hôte).
    - **Profondeur** (info) : plan (bbox bloc, dim courte).

    Pour chaque opening du plan :
    1. Position 2D + level (depuis plan_file → linked_view → Level).
    2. Vote orientation via les 4 élévations.
    3. Trouve le mur hôte parmi les Wall vivants du KG.
    4. Classify door (sill ≤ 0.15, height ≥ 1.9) ou window.
    5. Crée via `openings_create_many`.

    Args:
        scale_override: cf. dwg_inspect.
        section_lines: optionnel ; sinon lu du DxfImportContext.
        door_family_type_ref / window_family_type_ref: optionnels ;
            sinon auto-détectés.
        perp_tol_m: tolérance perpendiculaire pour matcher opening sur mur.

    Returns:
        {ok, openings_doors_created, openings_windows_created,
         openings_orphan, openings_unmatched, inner_openings, note}
    """
    if doc is not None:
        try:
            from .. import revit_primitives as rp
            rp.ensure_shared_param_binding(doc)
        except Exception:  # noqa: BLE001
            pass

    # 1. Build section_lines.
    if section_lines is None:
        section_lines = []
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is not None:
            ctx = kg.get_node(nid)
            seen = set()
            for sl in ctx.get("section_lines", []):
                key = (sl.get("coupe_path"), tuple(sl.get("plan_p1", [])),
                       tuple(sl.get("plan_p2", [])))
                if key in seen:
                    continue
                seen.add(key)
                section_lines.append(sl)
    if not section_lines:
        raise ValueError(
            "Pas de section_lines. Lance Phase 1 d'abord ou passe "
            "explicitement `section_lines`."
        )

    # 2. Charge élévations.
    elevations = _load_elevations_from_kg(kg, scale_override=scale_override)

    # 3. Build level_elev_by_revit_id (level_ref → elevation) depuis le KG.
    level_elev_by_id: Dict[str, float] = {}
    for nid in kg.find_by_type("Level"):
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is not None:
            continue
        level_elev_by_id[nid] = float(node.get("elevation", 0.0))

    # 4. Énumération **primaire** des openings depuis les **plans**
    #    (user 2026-05-13 : localisation + nombre dérivés du plan, pas
    #    des coupes). Width/depth depuis plan, height depuis élévation
    #    (fallback coupe), sill depuis coupe (fallback 0.9m fenêtre /
    #    0m porte selon height).
    plan_openings = _collect_plan_openings_world(
        kg, section_lines, scale_override,
    )

    # 5. Récupère les murs vivants du KG par level.
    walls_by_level: Dict[float, List[Tuple[str, Dict[str, Any]]]] = {}
    for nid in kg.find_by_type("Wall"):
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is not None:
            continue
        lvl_ref = node.get("level_ref")
        if lvl_ref is None or lvl_ref not in level_elev_by_id:
            continue
        elev = level_elev_by_id[lvl_ref]
        walls_by_level.setdefault(elev, []).append((nid, node))

    # 5b. Index INSERTs A-GLAZ par élévation (pour cross-val nombre +
    # position). Position résolue (INSERT + bbox bloc local).
    elev_inserts_by_dir = _build_elevation_inserts_by_direction(
        kg, scale_override=scale_override,
    )

    # 6. Première passe : pour chaque opening, vote orientation, classify
    # door/window, find host wall, accumule (kind, w, h) pour bulk types.
    pending_openings: List[Dict[str, Any]] = []  # [{co, host_ref, hp1, hp2, kind, w, h}]
    orphan_count = 0
    unmatched_count = 0
    oversize_count = 0  # fenêtre trop large pour son mur (skip pre-création)
    oversize_examples: List[Dict[str, Any]] = []
    orientation_stats = {"EW": 0, "NS": 0, "unknown": 0}
    # Matchs élévation par direction : compte combien de plan-openings
    # ont trouvé un INSERT correspondant.
    elev_match_stats: Dict[str, int] = {"Nord": 0, "Sud": 0, "Est": 0, "Ouest": 0}
    elev_unmatched_count = 0  # opening plan SANS aucun match élév attendu
    # Pour détecter les "orphelins" élévation (INSERT vu en élév sans
    # plan correspondant), on track les inserts consommés.
    consumed_inserts: Dict[str, Set[int]] = {
        d: set() for d in ("Nord", "Sud", "Est", "Ouest")
    }
    for co in plan_openings:
        opening_xy = (co["x_world"], co["y_world"])
        sill_m = co.get("sill_m")
        height_m = co.get("height_m")
        width_m = co.get("width_m") or 0.8
        if sill_m is None or height_m is None:
            unmatched_count += 1
            continue

        orientation, _ev = dwg_plan_openings.vote_opening_orientation_via_elevations(
            opening_xy, sill_m, height_m, width_m, elevations,
        )
        if orientation is None:
            orientation_stats["unknown"] += 1
        else:
            orientation_stats[orientation] += 1

        candidate_walls = walls_by_level.get(co["level_elevation_m"], [])
        best_host = None
        best_perp = float("inf")
        for nid, wnode in candidate_walls:
            p1 = wnode.get("p1")
            p2 = wnode.get("p2")
            if not p1 or not p2:
                continue
            wp1 = (float(p1[0]), float(p1[1]))
            wp2 = (float(p2[0]), float(p2[1]))
            dx, dy = wp2[0] - wp1[0], wp2[1] - wp1[1]
            wall_is_ew = abs(dx) > abs(dy)
            wall_orient = "EW" if wall_is_ew else "NS"
            if orientation is not None and wall_orient != orientation:
                continue
            thick = float(wnode.get("thickness", 0.20))
            perp = dwg_plan_openings._perp_distance_point_to_line(
                opening_xy, wp1, wp2,
            )
            tol = perp_tol_m + thick / 2.0
            if perp > tol:
                continue
            t = dwg_plan_openings._project_param(opening_xy, wp1, wp2)
            if t < -0.05 or t > 1.05:
                continue
            if perp < best_perp:
                best_perp = perp
                best_host = (nid, wp1, wp2, thick)

        if best_host is None:
            orphan_count += 1
            continue
        host_ref, hp1, hp2, _ = best_host
        kind = dwg_plan_openings.classify_opening_kind(sill_m, height_m)
        if kind == "unknown":
            unmatched_count += 1
            continue

        # Garde-fou : la fenêtre tient dans le mur ?
        # Revit refuse de créer une instance qui dépasse les bornes du
        # mur hôte (« Des occurrences de DXF_WIN ne coupent rien »).
        # On reject ici, plutôt que de laisser Revit planter en cours
        # de Tx. Marge 5cm aux extrémités.
        wall_len = math.hypot(hp2[0] - hp1[0], hp2[1] - hp1[1])
        t_proj = dwg_plan_openings._project_param(opening_xy, hp1, hp2)
        pos_on_wall = max(0.0, min(1.0, t_proj)) * wall_len
        half_w = float(width_m) / 2.0
        edge_margin = 0.05
        if (
            pos_on_wall - half_w < edge_margin
            or pos_on_wall + half_w > wall_len - edge_margin
        ):
            oversize_count += 1
            if len(oversize_examples) < 10:
                oversize_examples.append({
                    "block_id": co["block_id"],
                    "level": co["level_elevation_m"],
                    "host_wall_ref": host_ref,
                    "wall_length_m": round(wall_len, 3),
                    "pos_on_wall_m": round(pos_on_wall, 3),
                    "opening_width_m": float(width_m),
                })
            continue

        # Cross-val nombre + position via élévations (user 2026-05-13).
        # L'orientation détermine les 2 élévations cardinales attendues.
        # EW (mur horizontal sur le plan) → visible en Nord et Sud.
        # NS (mur vertical) → visible en Est et Ouest.
        z_world_sill = co["level_elevation_m"] + sill_m
        expected_dirs: List[str] = []
        if orientation == "EW":
            expected_dirs = ["Nord", "Sud"]
        elif orientation == "NS":
            expected_dirs = ["Est", "Ouest"]
        seen_in: List[str] = []
        best_elev_dis_m: Optional[float] = None
        for d in expected_dirs:
            x_elev_exp, _y_elev_exp = (
                dwg_elevation_reader.project_world_to_elevation(
                    co["x_world"], co["y_world"], z_world_sill, d,
                )
            )
            inserts = elev_inserts_by_dir.get(d, [])
            best_ins_idx: Optional[int] = None
            best_ins_dis: float = float("inf")
            for idx, ins in enumerate(inserts):
                if idx in consumed_inserts[d]:
                    continue
                dis = abs(ins.x_dxf_m - x_elev_exp)
                if dis < 0.30 and dis < best_ins_dis:
                    best_ins_idx = idx
                    best_ins_dis = dis
            if best_ins_idx is not None:
                consumed_inserts[d].add(best_ins_idx)
                seen_in.append(d)
                elev_match_stats[d] += 1
                if best_elev_dis_m is None or best_ins_dis < best_elev_dis_m:
                    best_elev_dis_m = best_ins_dis
        if expected_dirs and not seen_in:
            elev_unmatched_count += 1

        co["elev_seen_in"] = seen_in
        co["elev_position_disagreement_cm"] = (
            round(best_elev_dis_m * 100, 1) if best_elev_dis_m is not None else None
        )

        pending_openings.append({
            "co": co,
            "host_ref": host_ref,
            "hp1": hp1, "hp2": hp2,
            "kind": kind,
            "width_m": float(width_m),
            "height_m": float(height_m),
            "sill_m": sill_m,
            "opening_xy": opening_xy,
        })

    # 7. Bulk get_or_create types custom (DXF_WIN_WxH / DXF_DOOR_WxH).
    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    opening_type_entry = registry.get("openings_get_or_create_dxf_type_many")
    types_result = {"types": [], "created_count": 0, "reused_count": 0}
    if pending_openings and opening_type_entry is not None:
        type_items = [
            {"kind": po["kind"], "width_m": po["width_m"], "height_m": po["height_m"]}
            for po in pending_openings
        ]
        types_result = opening_type_entry.fn(
            kg=kg, doc=doc, items=type_items,
        )
    # Build mapping (kind, w_cm, h_cm) → llm_id.
    type_ref_by_dims: Dict[Tuple[str, int, int], str] = {}
    for t in types_result["types"]:
        key = (
            t["kind"],
            int(round(t["width_m"] * 100)),
            int(round(t["height_m"] * 100)),
        )
        type_ref_by_dims[key] = t["llm_id"]

    # Fallback overrides utilisateur (si pas DXF custom dispo).
    if door_family_type_ref is None:
        door_family_type_ref = _find_default_family_type(kg, "Doors")
    if window_family_type_ref is None:
        window_family_type_ref = _find_default_family_type(kg, "Windows")

    # 8. Build opening items avec types custom.
    door_items: List[Dict[str, Any]] = []
    window_items: List[Dict[str, Any]] = []
    for po in pending_openings:
        w_cm = int(round(po["width_m"] * 100))
        h_cm = int(round(po["height_m"] * 100))
        type_ref = type_ref_by_dims.get((po["kind"], w_cm, h_cm))
        if type_ref is None:
            # Fallback générique.
            type_ref = (
                door_family_type_ref if po["kind"] == "door"
                else window_family_type_ref
            )
        if type_ref is None:
            unmatched_count += 1
            continue
        proj_pos = dwg_plan_openings.project_pos_onto_wall_centerline(
            po["opening_xy"], po["hp1"], po["hp2"],
        )
        item = {
            "kind": po["kind"],
            "host_wall_ref": po["host_ref"],
            "family_type_ref": type_ref,
            "position": [proj_pos[0], proj_pos[1]],
            "sill_height": po["sill_m"],
        }
        if po["kind"] == "door":
            door_items.append(item)
        else:
            window_items.append(item)

    # 9. Crée openings via openings_create_many.
    openings_entry = registry.get("openings_create_many")
    all_items = door_items + window_items
    if all_items and openings_entry is not None:
        inner = openings_entry.fn(kg=kg, doc=doc, items=all_items)
    else:
        inner = None

    # Stats cross-validation dimensions plan/élévation/coupe.
    width_disagreements: List[Dict[str, Any]] = []
    sill_disagreements: List[Dict[str, Any]] = []
    head_disagreements: List[Dict[str, Any]] = []
    width_source_stats: Dict[str, int] = {}
    sill_source_stats: Dict[str, int] = {}
    for po in plan_openings:
        wsrc = po.get("width_source") or "unknown"
        ssrc = po.get("sill_source") or "unknown"
        width_source_stats[wsrc] = width_source_stats.get(wsrc, 0) + 1
        sill_source_stats[ssrc] = sill_source_stats.get(ssrc, 0) + 1
        if po.get("width_disagreement_cm") is not None:
            width_disagreements.append({
                "block_id": po["block_id"],
                "level": po["level_elevation_m"],
                "plan_width_m": po["width_m"],
                "disagreement_cm": po["width_disagreement_cm"],
            })
        if po.get("sill_disagreement_cm") is not None:
            sill_disagreements.append({
                "block_id": po["block_id"],
                "level": po["level_elevation_m"],
                "section_sill_m": po["sill_m"],
                "disagreement_cm": po["sill_disagreement_cm"],
            })
        if po.get("head_disagreement_cm") is not None:
            head_disagreements.append({
                "block_id": po["block_id"],
                "level": po["level_elevation_m"],
                "computed_head_m": (po["sill_m"] or 0) + (po["height_m"] or 0),
                "disagreement_cm": po["head_disagreement_cm"],
            })

    # Cross-val nombre + position : orphans élévation (INSERTs non
    # consommés par aucune fenêtre du plan).
    elev_total_per_dir: Dict[str, int] = {}
    elev_orphans_per_dir: Dict[str, int] = {}
    for d, inserts in elev_inserts_by_dir.items():
        elev_total_per_dir[d] = len(inserts)
        elev_orphans_per_dir[d] = len(inserts) - len(consumed_inserts[d])
    elev_position_disagreements: List[Dict[str, Any]] = []
    for po in plan_openings:
        d_cm = po.get("elev_position_disagreement_cm")
        if d_cm is not None and d_cm > 10.0:
            elev_position_disagreements.append({
                "block_id": po["block_id"],
                "level": po["level_elevation_m"],
                "x_world": po["x_world"],
                "y_world": po["y_world"],
                "disagreement_cm": d_cm,
                "seen_in": po.get("elev_seen_in", []),
            })

    note = (
        "Phase 2b : {} portes + {} fenêtres hostées via vote orientation, "
        "{} types DXF custom créés / {} réutilisés. {} orphelins (pas de "
        "mur hôte trouvé), {} non matchés, {} rejetés (trop larges pour "
        "leur mur — évite l'erreur Revit 'ne coupent rien'). Orientations : "
        "EW={}, NS={}, unknown={}. Cross-val dims plan↔élév : largeur "
        "{}match/{}désaccord, "
        "allège {}match/{}désaccord/{}élev-fallback, linteau {}désaccord. "
        "Cross-val position élév : {} plan-openings non vus en élévation, "
        "{} inserts élévation orphelins (somme toutes directions), "
        "{} désaccords position > 10cm."
        .format(
            len(door_items), len(window_items),
            types_result["created_count"], types_result["reused_count"],
            orphan_count, unmatched_count, oversize_count,
            orientation_stats["EW"], orientation_stats["NS"],
            orientation_stats["unknown"],
            width_source_stats.get("plan_elev_match", 0),
            width_source_stats.get("plan_disagrees_with_elev", 0),
            sill_source_stats.get("section_elev_match", 0),
            sill_source_stats.get("section_disagrees_with_elev", 0),
            sill_source_stats.get("elevation", 0),
            len(head_disagreements),
            elev_unmatched_count,
            sum(elev_orphans_per_dir.values()),
            len(elev_position_disagreements),
        )
    )

    return {
        "ok": True,
        "plan_openings_detected": len(plan_openings),
        "openings_doors_created": len(door_items),
        "openings_windows_created": len(window_items),
        "openings_orphan": orphan_count,
        "openings_unmatched": unmatched_count,
        "openings_oversize_for_wall": oversize_count,
        "openings_oversize_examples": oversize_examples,
        "orientation_stats": orientation_stats,
        "width_source_stats": width_source_stats,
        "sill_source_stats": sill_source_stats,
        "width_disagreements": width_disagreements,
        "sill_disagreements": sill_disagreements,
        "head_disagreements": head_disagreements,
        "elevation_match_stats": elev_match_stats,
        "elevation_total_per_direction": elev_total_per_dir,
        "elevation_orphans_per_direction": elev_orphans_per_dir,
        "elevation_unmatched_plan_count": elev_unmatched_count,
        "elevation_position_disagreements": elev_position_disagreements,
        "opening_types_created": types_result["created_count"],
        "opening_types_reused": types_result["reused_count"],
        "opening_types": types_result["types"],
        "inner_openings": inner,
        "note": note,
    }


# ----- 11. Phase 2.5 : walls fusionnés + openings hosted ---------------


def _find_default_family_type(
    kg: ProjectKG, category: str,
) -> Optional[str]:
    """Cherche dans le KG le 1er `FamilyType` vivant de la catégorie
    donnée (`"Doors"` ou `"Windows"`). Retourne son llm_id ou None.
    """
    for nid in kg.find_by_type("FamilyType"):
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is not None:
            continue
        if node.get("category") == category:
            return nid
    return None


class _VirtualWall:
    """Adapter qui imite l'API de `WallCandidate` (p1, p2, thickness,
    layer, confidence) à partir d'un dict d'hypothèse. Utilisé pour
    insérer un mur virtuel dans `walls_current` lors d'une récupération
    d'orphan par vote.
    """
    def __init__(self, hypo: Dict[str, Any]) -> None:
        self.p1 = hypo["p1"]
        self.p2 = hypo["p2"]
        self.thickness = float(hypo["thickness"])
        self.layer = hypo["layer"]
        self.confidence = float(hypo["confidence"])
        self.source_indices: List[int] = []  # virtual = no source fragments


def _try_recover_orphan_via_vote(
    co: Dict[str, Any],
    plan_record: Dict[str, Any],
    elevations: Dict[str, "dwg_elevation_reader.ElevationView"],
    section_lines: List[Dict[str, Any]],
    *,
    min_decision_confidence: float = 0.5,
    height_m_default: float = 3.0,
) -> bool:
    """Essai de récupération d'une opening orphan par construction d'un
    mur virtuel + vote élévations (V2 step 2).

    Algorithme :
    1. Identifie la section_line correspondante.
    2. Construit un mur virtuel passant par l'opening, perpendiculaire
       au trait.
    3. Vote via les 4 élévations (si présentes).
    4. Si `aggregate_votes` retourne yes avec confidence ≥
       `min_decision_confidence`, ajoute le mur virtuel à
       `plan_record["walls_current"]` et host l'opening.

    Returns:
        True si récupération réussie, False sinon.
    """
    if not elevations:
        return False
    sl = next(
        (x for x in section_lines if x.get("coupe_path") == co.get("coupe_path")),
        None,
    )
    if sl is None:
        return False
    sp1 = (float(sl["plan_p1"][0]), float(sl["plan_p1"][1]))
    sp2 = (float(sl["plan_p2"][0]), float(sl["plan_p2"][1]))
    opening_xy = (co["x_world"], co["y_world"])
    hypo = dwg_plan_openings.build_virtual_wall_hypothesis(opening_xy, sp1, sp2)
    # V2.1 fix mur virtuel isolé : snap endpoints vers murs voisins
    # collinéaires (max 5m d'extension). Évite les murs flottants visibles
    # en 3D.
    hypo = dwg_plan_openings.snap_virtual_wall_to_neighbors(
        hypo, plan_record["walls_current"],
    )

    votes = []
    for direction, ev in elevations.items():
        v = dwg_elevation_reader.vote_wall_visible_in_elevation(
            hypo["p1"], hypo["p2"],
            co["level_elevation_m"], height_m_default, ev,
        )
        votes.append(v)
    if not votes:
        return False
    decision = dwg_voting.aggregate_votes(votes, min_voters=1, threshold=0.5)
    if decision.answer is not True:
        return False
    if decision.confidence_score < min_decision_confidence:
        return False

    # Accept : append virtual wall to walls_current and host opening.
    virtual = _VirtualWall(hypo)
    plan_record["walls_current"].append(virtual)
    new_idx = len(plan_record["walls_current"]) - 1
    plan_record["openings_assigned"].append({
        "coupe_opening": co,
        "host_idx": new_idx,
        "virtual_wall": True,
        "vote_decision_confidence": decision.confidence_score,
    })
    return True


def _build_elevation_inserts_by_direction(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
) -> Dict[str, List[Any]]:
    """Énumère les INSERT A-GLAZ par élévation cardinale, avec position
    résolue (INSERT + bbox locale du bloc — même algo que coupe).

    Returns:
        `{direction: [SectionOpening, ...]}` avec direction ∈ {Nord, Sud,
        Est, Ouest}. Chaque opening a `x_dxf_m` (= x_elev_abs) et
        `y_dxf_m` (= y_elev_abs ≈ sill_abs si convention Revit AIA
        respectée).
    """
    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)
    out: Dict[str, List[Any]] = {}
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "elevation":
            continue
        pp = Path(fi.get("path") or "")
        if not pp.exists():
            continue
        name_low = pp.name.lower()
        direction: Optional[str] = None
        for d, aliases in (
            ("Nord", ("nord", "north")),
            ("Sud", ("sud", "south")),
            ("Est", ("est", "east")),
            ("Ouest", ("ouest", "west")),
        ):
            if any(a in name_low for a in aliases):
                direction = d
                break
        if direction is None:
            continue
        try:
            ents, meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        ops = dwg_section_reader.read_section_openings(ents)
        ops = dwg_section_reader.resolve_section_opening_positions(
            pp, ops, units_factor_to_m=meta.get("units_factor_to_m", 1.0),
        )
        out[direction] = ops
    return out


def _load_elevations_from_kg(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
) -> Dict[str, "dwg_elevation_reader.ElevationView"]:
    """Charge les élévations depuis le `DxfImportContext.linked_views`.

    Filtre par mots-clés filename / view_name pour détecter les
    élévations (l'agent passe parfois `view_kind='section'` au lieu de
    `'elevation'` — bug séparé tracking).

    Returns:
        Dict `{direction: ElevationView}` avec `direction` ∈ {Nord, Sud,
        Est, Ouest}. Au plus 1 élévation par direction (la dernière vue
        gagne).
    """
    elevations: Dict[str, "dwg_elevation_reader.ElevationView"] = {}
    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return elevations
    ctx = kg.get_node(nid)
    linked = ctx.get("linked_views", [])
    seen_paths: Set[str] = set()
    for lv in linked:
        fp = lv.get("file_path", "")
        if not fp or fp in seen_paths:
            continue
        seen_paths.add(fp)
        name_low = (lv.get("view_name") or "").lower()
        path_low = fp.lower()
        is_elev = any(
            kw in name_low or kw in path_low
            for kw in ("élévation", "elévation", "elevation")
        )
        if not is_elev:
            continue
        direction: Optional[str] = None
        # Ordre important : Ouest avant Est (subset substring).
        for d, aliases in (
            ("Ouest", ("ouest", "west")),
            ("Nord", ("nord", "north")),
            ("Sud", ("sud", "south")),
            ("Est", ("est", "east")),
        ):
            if any(a in name_low or a in path_low for a in aliases):
                direction = d
                break
        if direction is None:
            continue
        cp = Path(fp)
        if not cp.exists():
            continue
        try:
            ents, _ = dwg_reader.parse(cp, scale_override=scale_override)
            elevations[direction] = dwg_elevation_reader.parse_elevation(
                ents, direction,
            )
        except Exception:  # noqa: BLE001 — élévation illisible n'arrête pas l'import.
            continue
    return elevations


def _build_plan_dims_index(
    kg: ProjectKG,
    scale_override: Optional[float],
) -> Dict[str, Dict[str, float]]:
    """Mapping `{block_id → {"width_m", "depth_m"}}` depuis tous les plans
    du DxfImportContext.

    En plan, la bbox du bloc A-GLAZ a dimension longue = largeur, dim
    courte = profondeur (= épaisseur mur traversé). Agrégation max() en
    cas de divergence inter-plan (Niveau 0 vs Niveau 1 même block_id).
    """
    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)
    index: Dict[str, Dict[str, float]] = {}
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "plan":
            continue
        pp = Path(fi.get("path") or "")
        if not pp.exists():
            continue
        try:
            _, meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        plan_dims = dwg_section_reader.read_plan_opening_dims_by_block_id(
            pp, units_factor_to_m=meta.get("units_factor_to_m", 0.001),
        )
        for bid, dims in plan_dims.items():
            cur = index.get(bid)
            if cur is None or dims["width_m"] > cur["width_m"]:
                index[bid] = {
                    "width_m": dims["width_m"],
                    "depth_m": dims["depth_m"],
                }
    return index


def _build_plan_width_index(
    kg: ProjectKG,
    scale_override: Optional[float],
) -> Dict[str, float]:
    """Compat wrapper. Préférer `_build_plan_dims_index` pour aussi depth_m."""
    return {bid: d["width_m"] for bid, d in _build_plan_dims_index(kg, scale_override).items()}


def _build_elevation_dims_index(
    kg: ProjectKG,
    scale_override: Optional[float],
) -> Dict[str, Dict[str, float]]:
    """Mapping `{block_id → {"width_m", "height_m"}}` depuis toutes les
    élévations du DxfImportContext.

    La **hauteur** est l'info utile (cross-check avec coupe ; prioritaire
    selon décision user 2026-05-13). La largeur permet un cross-check
    avec le plan.
    """
    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)
    index: Dict[str, Dict[str, float]] = {}
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "elevation":
            continue
        pp = Path(fi.get("path") or "")
        if not pp.exists():
            continue
        try:
            _, meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        elev_dims = dwg_section_reader.read_elevation_opening_dims_by_block_id(
            pp, units_factor_to_m=meta.get("units_factor_to_m", 0.001),
        )
        for bid, dims in elev_dims.items():
            cur = index.get(bid)
            if cur is None or dims["height_m"] > cur["height_m"]:
                index[bid] = dict(dims)
    return index


def _build_sill_index_from_coupes(
    kg: ProjectKG,
    section_lines: List[Dict[str, Any]],
    scale_override: Optional[float],
) -> Dict[Tuple[str, float], Dict[str, float]]:
    """Mapping `{(block_id, level_elevation_m) → {"sill_m", "height_m"}}`
    depuis toutes les coupes référencées.

    Le sill_m (hauteur d'allège relative au niveau hôte) n'est lisible
    que depuis la coupe : on regarde y_dxf - level_y. La height_m
    extraite ici sert de fallback à `_build_elevation_dims_index`.
    """
    out: Dict[Tuple[str, float], Dict[str, float]] = {}
    for sl in section_lines:
        coupe_path = sl.get("coupe_path")
        if not coupe_path:
            continue
        cp = Path(coupe_path)
        if not cp.exists():
            continue
        try:
            ents, meta = dwg_reader.parse(cp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        levels = sorted(
            dwg_section_reader.read_levels(ents),
            key=lambda l: l.elevation_m,
        )
        sec_openings = dwg_section_reader.read_section_openings(ents)
        sec_openings = dwg_section_reader.resolve_section_opening_positions(
            cp, sec_openings,
            units_factor_to_m=meta.get("units_factor_to_m", 1.0),
        )
        for so in sec_openings:
            if so.block_id is None:
                continue
            host_lvl_elev: Optional[float] = None
            for lv in levels:
                if lv.elevation_m <= so.y_dxf_m + 1e-3:
                    host_lvl_elev = lv.elevation_m
            if host_lvl_elev is None:
                continue
            sill = round(so.y_dxf_m - host_lvl_elev, 4)
            key = (so.block_id, round(host_lvl_elev, 3))
            cur = out.get(key)
            entry = {"sill_m": sill, "height_m": so.height_m}
            if cur is None:
                out[key] = entry
            # Sinon on garde le premier — multi-coupes même opening = même valeurs.
    return out


def _plan_path_to_level_elev(
    kg: ProjectKG,
) -> Dict[str, float]:
    """Mapping `{plan_file_path → level_elevation_m}` via le DxfImportContext.

    Stratégie : pour chaque linked_view de view_kind="plan", le `view_name`
    matche un Level node (par `name`). Robuste à l'ordre/numérotation des
    levels. Fallback : si view_name absent ou pas de Level matching,
    parser le nom de fichier `Niveau N` et matcher au N-ième Level trié.
    """
    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)

    levels_by_name: Dict[str, float] = {}
    sorted_levels: List[Tuple[str, float]] = []
    for lid in kg.find_by_type("Level"):
        node = kg.get_node(lid)
        if node.get("deleted_at_turn") is not None:
            continue
        name = node.get("name") or ""
        elev = float(node.get("elevation", 0.0))
        levels_by_name[name] = elev
        sorted_levels.append((name, elev))
    sorted_levels.sort(key=lambda kv: kv[1])

    out: Dict[str, float] = {}
    for lv in ctx.get("linked_views") or []:
        if lv.get("view_kind") != "plan":
            continue
        fp = lv.get("file_path")
        if not fp:
            continue
        vname = lv.get("view_name") or ""
        if vname in levels_by_name:
            out[fp] = levels_by_name[vname]
            continue
        # Fallback : parse N depuis "Niveau N" et matcher au N-ième level trié.
        m = re.search(r"(?:Niveau|Level)\s+(\d+)", vname, re.IGNORECASE)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(sorted_levels):
                out[fp] = sorted_levels[idx][1]
    return out


def _collect_plan_openings_world(
    kg: ProjectKG,
    section_lines: List[Dict[str, Any]],
    scale_override: Optional[float],
) -> List[Dict[str, Any]]:
    """Énumération **primaire** des openings depuis les plans (source de
    référence pour la localisation et le nombre — user 2026-05-13).

    Pipeline :
    1. Pour chaque plan du DxfImportContext, lit les INSERT A-GLAZ
       (position 2D + block_id).
    2. Détermine le level via `_plan_path_to_level_elev`.
    3. Enrichit avec :
       - `width_m`, `depth_m` ← plan (bbox bloc, déjà extrait).
       - `height_m` ← élévation (bbox), fallback coupe.
       - `sill_m` ← coupe (lookup par block_id), fallback 0.9m (fenêtre) / 0.0m (porte).

    Returns:
        Liste de dicts `{block_id, block_name, x_world, y_world, sill_m,
        height_m, width_m, depth_m, level_elevation_m, plan_path,
        width_source, height_source, sill_source}`.
    """
    plan_dims = _build_plan_dims_index(kg, scale_override=scale_override)
    elev_dims = _build_elevation_dims_index(kg, scale_override=scale_override)
    sill_idx = _build_sill_index_from_coupes(kg, section_lines, scale_override)
    plan_levels = _plan_path_to_level_elev(kg)

    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return []
    ctx = kg.get_node(nid)

    raw: List[Dict[str, Any]] = []
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "plan":
            continue
        plan_path = fi.get("path")
        if not plan_path:
            continue
        pp = Path(plan_path)
        if not pp.exists():
            continue
        level_elev = plan_levels.get(plan_path)
        if level_elev is None:
            continue
        try:
            ents, meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        plan_openings = dwg_section_reader.read_plan_opening_inserts(ents)
        for po in plan_openings:
            # `dwg_reader.parse` retourne déjà les coords en mètres.
            x_world = po.x_dxf_m
            y_world = po.y_dxf_m
            bid = po.block_id

            # Width/depth via plan index.
            pd = plan_dims.get(bid) if bid else None
            width_m = pd["width_m"] if pd else None
            depth_m = pd["depth_m"] if pd else None
            width_source = "plan" if pd else None

            # Height via élévation, fallback coupe (par sill_idx au même level).
            ed = elev_dims.get(bid) if bid else None
            # Garde-fou : la bbox élévation peut être dégénérée (~17mm)
            # pour des blocs où la géométrie GLAZ est partiellement
            # encapsulée dans des sous-INSERTs. On accepte la hauteur
            # élévation seulement si elle est plausible (≥ 0.30m), sinon
            # fallback coupe.
            ed_height_plausible = (
                ed is not None and ed["height_m"] >= 0.30
            )
            height_m: Optional[float] = (
                ed["height_m"] if ed_height_plausible else None
            )
            height_source = "elevation" if ed_height_plausible else None

            # Cross-validation largeur plan ↔ élévation (user 2026-05-13).
            # Tolérance 5cm. Le plan reste source primaire.
            width_disagreement_cm: Optional[float] = None
            if width_m is not None and ed is not None and ed["width_m"] >= 0.30:
                w_elev = ed["width_m"]
                disagreement_cm = abs(width_m - w_elev) * 100.0
                if disagreement_cm <= 5.0:
                    width_source = "plan_elev_match"
                else:
                    width_disagreement_cm = round(disagreement_cm, 1)
                    width_source = "plan_disagrees_with_elev"
            elif width_m is not None and bid is not None:
                width_source = "plan_only"
            sill_entry = (
                sill_idx.get((bid, round(level_elev, 3))) if bid else None
            )
            if height_m is None and sill_entry is not None:
                height_m = sill_entry.get("height_m")
                height_source = "section"

            # Sill via coupe au même level.
            sill_m: Optional[float] = (
                sill_entry.get("sill_m") if sill_entry is not None else None
            )
            sill_source = "section" if sill_m is not None else None

            # Cross-validation sill (allège) + head (linteau) ↔ élévation
            # (user 2026-05-13). Sill local du bloc en élévation =
            # sill relatif au level (INSERT placé à (x_elev, level_y)).
            # Head local = sill + height. Tolérance 5cm.
            sill_disagreement_cm: Optional[float] = None
            head_disagreement_cm: Optional[float] = None
            if (
                ed is not None
                and ed.get("sill_local_m") is not None
                and ed["height_m"] >= 0.30  # bbox plausible
            ):
                sill_elev = ed["sill_local_m"]
                head_elev = ed["head_local_m"]
                if sill_m is not None:
                    dis = abs(sill_m - sill_elev) * 100.0
                    if dis <= 5.0:
                        sill_source = "section_elev_match"
                    else:
                        sill_disagreement_cm = round(dis, 1)
                        sill_source = "section_disagrees_with_elev"
                else:
                    # Coupe muette pour cet opening → sill depuis élévation.
                    sill_m = sill_elev
                    sill_source = "elevation"
                # Cross-val head si height connue (= linteau attendu).
                if height_m is not None and sill_m is not None:
                    head_expected = sill_m + height_m
                    dis_h = abs(head_expected - head_elev) * 100.0
                    if dis_h > 5.0:
                        head_disagreement_cm = round(dis_h, 1)

            # Fallback dimensions depuis le nom de bloc.
            if width_m is None or height_m is None:
                dims_from_name = dwg_section_reader.parse_block_dimensions(
                    po.block_name,
                )
                if dims_from_name is not None:
                    nw, nh = dims_from_name
                    if width_m is None:
                        width_m = nw
                        width_source = "block_name"
                    if height_m is None:
                        height_m = nh
                        height_source = "block_name"

            if width_m is None or height_m is None:
                # Pas de dims utilisables — skip (signalera dans le tool).
                continue

            # Sill par défaut : 0.9m si height suggère fenêtre, 0.0m si porte.
            if sill_m is None:
                # Convention user : sill ≤ 0.15 + height ≥ 1.9 → door, else window.
                if height_m >= 1.9:
                    sill_m = 0.0
                    sill_source = "default_door"
                else:
                    sill_m = 0.9
                    sill_source = "default_window"

            raw.append({
                "block_id": bid,
                "block_name": po.block_name,
                "x_world": round(x_world, 4),
                "y_world": round(y_world, 4),
                "sill_m": sill_m,
                "height_m": height_m,
                "width_m": width_m,
                "depth_m": depth_m,
                "level_elevation_m": level_elev,
                "plan_path": plan_path,
                "width_source": width_source,
                "height_source": height_source,
                "sill_source": sill_source,
                "width_disagreement_cm": width_disagreement_cm,
                "sill_disagreement_cm": sill_disagreement_cm,
                "head_disagreement_cm": head_disagreement_cm,
            })

    # Dédup par (block_id, round position, level) — multi-plans même opening = 1.
    seen: Dict[Tuple[Any, float, float, float], Dict[str, Any]] = {}
    for co in raw:
        key = (
            co["block_id"] or co["block_name"],
            round(co["x_world"], 2),
            round(co["y_world"], 2),
            round(co["level_elevation_m"], 3),
        )
        if key not in seen:
            seen[key] = co
    return list(seen.values())


def _collect_coupe_openings_world(
    kg: ProjectKG,
    section_lines: List[Dict[str, Any]],
    scale_override: Optional[float],
    level_elev_by_ref: Dict[str, float],
    level_match_tol_m: float = 0.05,
) -> List[Dict[str, Any]]:
    """Lit les openings de chaque coupe référencée, projette en world plan,
    déterminé leur niveau hôte, déduplique par block_id+position.

    La **largeur** (`width_m`) est lue **depuis le plan** (bbox du bloc,
    dimension longue) via `_build_plan_width_index`, pas depuis la coupe :
    en coupe la bbox = tranche de profil, pas la vraie largeur.

    Returns:
        Liste de dicts `{block_id, block_name, x_world, y_world, sill_m,
        height_m, width_m, level_elevation_m, coupe_path,
        section_line_index}`.
    """
    plan_widths = _build_plan_width_index(kg, scale_override=scale_override)
    raw: List[Dict[str, Any]] = []
    for sl_idx, sl in enumerate(section_lines):
        coupe_path = sl.get("coupe_path")
        if not coupe_path:
            continue
        cp = Path(coupe_path)
        if not cp.exists():
            continue
        ents, meta = dwg_reader.parse(cp, scale_override=scale_override)
        levels = sorted(
            dwg_section_reader.read_levels(ents),
            key=lambda l: l.elevation_m,
        )
        sec_openings = dwg_section_reader.read_section_openings(ents)
        # Résout la VRAIE position de chaque opening en lisant la
        # BLOCK_DEFINITION référencée — Revit DXF met l'INSERT à
        # `(0, level_y)` et la géométrie réelle dans le bloc. Sans cette
        # résolution, toutes les openings se projettent au même point
        # (= sur le trait) et restent orphelines. Bug runtime P7 session s.
        sec_openings = dwg_section_reader.resolve_section_opening_positions(
            cp, sec_openings,
            units_factor_to_m=meta.get("units_factor_to_m", 1.0),
        )
        sp1 = (float(sl["plan_p1"][0]), float(sl["plan_p1"][1]))
        sp2 = (float(sl["plan_p2"][0]), float(sl["plan_p2"][1]))
        for so in sec_openings:
            # Niveau hôte = le plus haut niveau ≤ y_dxf de l'opening.
            host_lvl_elev: Optional[float] = None
            for lv in levels:
                if lv.elevation_m <= so.y_dxf_m + 1e-3:
                    host_lvl_elev = lv.elevation_m
            if host_lvl_elev is None:
                continue
            sill = round(so.y_dxf_m - host_lvl_elev, 4)
            x_w, y_w = dwg_plan_openings.project_section_opening_to_world(
                so.x_dxf_m, sp1, sp2,
            )
            # `width_m` prioritaire depuis le plan (bbox bloc, dim longue) ;
            # fallback sur la valeur extraite de la coupe (bbox de profil)
            # si block_id absent ou non indexé.
            width_from_plan = (
                plan_widths.get(so.block_id) if so.block_id else None
            )
            raw.append({
                "block_id": so.block_id,
                "block_name": so.block_name,
                "x_world": x_w,
                "y_world": y_w,
                "sill_m": sill,
                "height_m": so.height_m,
                "width_m": width_from_plan if width_from_plan is not None else so.width_m,
                "width_source": "plan" if width_from_plan is not None else "section",
                "level_elevation_m": host_lvl_elev,
                "coupe_path": str(cp),
                "section_line_index": sl_idx,
            })

    # Dédup par (block_id, round position, level_elevation).
    # Un même opening peut être vu depuis 2 coupes (même block_id) à la
    # même position world → 1 seule fenêtre à créer.
    seen: Dict[Tuple[Any, float, float, float], Dict[str, Any]] = {}
    for co in raw:
        key = (
            co["block_id"] or co["block_name"],
            round(co["x_world"], 2),
            round(co["y_world"], 2),
            round(co["level_elevation_m"], 3),
        )
        if key not in seen:
            seen[key] = co
    return list(seen.values())


# Tool DEPRECATED (tier=3 = pas exposé au LLM par défaut) — remplacé
# par le pipeline décomposé `dwg_create_continuous_walls_many` (Phase 2a)
# + `dwg_add_openings_to_walls_many` (Phase 2b). Runtime P7 a montré
# que l'agent l'utilisait à tort APRÈS Phase 2a, recréant 25 murs
# doublons → chaos visuel.
@tool(name="dwg_import_walls_and_openings_typed_many", tier=3)
def import_walls_and_openings_typed_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    bucket_cm: int = 1,
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
    base_type_ref: Optional[str] = None,
    door_family_type_ref: Optional[str] = None,
    window_family_type_ref: Optional[str] = None,
    coupe_paths: Optional[List[str]] = None,
    section_lines: Optional[List[Dict[str, Any]]] = None,
    max_walls_per_file: int = 500,
) -> Dict[str, Any]:
    """Phase 2 complète V1 : import bulk multi-plans avec fusion fragments
    + openings hosted, **piloté par les coupes** (source primaire).

    **V1 — pipeline coupe-first** (refonte session r post-runtime P7,
    walls_merged=0 sur V0 plan-only) :

    1. Lit les `section_lines` (KG `DxfImportContext` ou argument).
    2. Pour chaque coupe référencée : lit les openings (`block_id`,
       `x_cut_m`, `sill_m`, `height_m`), les projette en world plan via
       `project_section_opening_to_world` (convention DXF anchor).
       Dédup par `(block_id, position arrondie, niveau)` pour éviter
       les doublons quand une fenêtre apparaît dans 2 coupes.
    3. Classify les walls de chaque plan (paires parallèles, **sans**
       fusion via INSERT A-GLAZ plan — la V0 plan-only échouait là).
    4. Pour chaque coupe_opening world : trouve le plan dont level
       matche, puis utilise `merge_fragments_around_opening` qui
       fusionne 2 fragments encadrants (collinéaires + gap ≤ 3m + opening
       projeté dans le gap). Pas de fusion accidentelle parce que la
       position de l'opening vient de la coupe (fiable, pas du plan).
    5. Pre-flight : `project_pos_onto_wall_centerline` clamp la position
       à la centerline du mur hôte (±5cm des extrémités). Évite l'erreur
       Revit `ArgumentException: ne coupent rien` si la position est
       légèrement off-curve.
    6. Crée types DXF_WALL_*cm (1 Tx), murs (1 Tx), openings (1 Tx).
       Classify door/window selon `sill_m ≤ 0.15 et height_m ≥ 1.9`.

    **Limites V1** :
    - Un opening absent des coupes ne sera pas créé (orphelin compté).
    - Un mur en plan sans opening n'est pas fusionné (intact).
    - Pour les openings sans match, prévoir post-traitement manuel.

    Concepts: dxf, import, mur, fenêtre, porte, opening, fusion,
              continuité, plan, coupe, phase 2.5, walls openings hosted
    Phrases: "importe les murs et les ouvertures",
             "crée les murs continus avec fenêtres et portes",
             "import walls and openings"
    Similar: dwg_import_walls_typed_many,
             dwg_extract_wall_thicknesses_many,
             openings_create_many, walls_get_or_create_dxf_type_many

    Args:
        items: liste de dicts `{file_path, level_ref, height_m?,
            dx_m?, dy_m?}`. Au moins un item.
        bucket_cm / layer_mapping / scale_override / min_thickness_m /
            max_thickness_m / include_centerline / base_type_ref / max_walls_per_file:
            cf. `dwg_import_walls_typed_many`.
        door_family_type_ref / window_family_type_ref: llm_ids des
            FamilyType Doors/Windows à utiliser. Si None, auto-détecté
            (1er FamilyType de la catégorie dans le KG).
        coupe_paths: chemins des DXF coupes pour matching block_id →
            sill/height. Si None, lus depuis `DxfImportContext.
            section_lines` du KG.

    Returns:
        {"ok": bool, "files_count": int,
         "walls_imported_total": int, "walls_per_file": {path: count},
         "walls_merged_count": int,  # nb de fusions effectuées
         "types_created": int, "types_reused": int, "types": [...],
         "openings_doors_created": int, "openings_windows_created": int,
         "openings_unmatched_count": int,
         "openings_orphan_count": int,  # pas hostable (pas de mur trouvé)
         "thickness_distribution_global": {cm: count},
         "inner_walls": ..., "inner_openings": ...,
         "note": str}
    """
    # --- V1 — pipeline coupe-first --------------------------------------
    # 1. Lire section_lines (KG ou explicite via coupe_paths).
    # 2. Lire les openings de chaque coupe, projeter en world plan.
    # 3. Classify walls de chaque plan (sans fusion plan-side).
    # 4. Pour chaque opening world, déterminer son level + plan hôte,
    #    fusionner les fragments mur si besoin, puis assigner host_wall.
    # 5. Créer types DXF_WALL_*cm + walls + openings.
    # 6. Pre-flight project_pos_onto_wall_centerline pour éviter
    #    l'erreur Revit « ne coupent rien ».
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}

    # V3.3 : ensure shared param `claude-in-revit:llm_id` est bound
    # avant les créations (cf. `dwg_create_continuous_walls_many`).
    if doc is not None:
        try:
            from .. import revit_primitives as rp
            rp.ensure_shared_param_binding(doc)
        except Exception:  # noqa: BLE001
            pass

    # --- Build section_lines (paramètre explicite ou KG) --------------
    if section_lines is None:
        section_lines = []
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is not None:
            ctx = kg.get_node(nid)
            section_lines = list(ctx.get("section_lines", []))
    if not section_lines:
        raise ValueError(
            "Pas de section_lines (ni argument explicite, ni "
            "DxfImportContext en KG). Lance d'abord Phase 1 "
            "(dwg_inspect_sections + dxf_context_register_section_line_many) "
            "ou passe explicitement `section_lines=[{coupe_path, plan_p1, "
            "plan_p2, view_dir}, ...]`."
        )

    # --- Pré-valider items + classifier les walls de chaque plan -------
    plan_records: List[Dict[str, Any]] = []
    level_elev_by_ref: Dict[str, float] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError("items[{}] must be a dict".format(i))
        fp = it.get("file_path")
        level_ref = it.get("level_ref")
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("items[{}]: file_path required".format(i))
        if not isinstance(level_ref, str) or not kg.has_node(level_ref):
            raise ValueError(
                "items[{}]: unknown level_ref {!r}".format(i, level_ref)
            )
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError(
                "items[{}]: file not found: {}".format(i, path)
            )
        _refuse_if_section(path)
        # Niveau du plan (élévation, pour matcher avec coupe_openings).
        lvl_node = kg.get_node(level_ref)
        level_elev_by_ref[level_ref] = float(lvl_node.get("elevation", 0.0))

        entities, _ = dwg_reader.parse(path, scale_override=scale_override)
        classified = dwg_classifier.classify(
            entities, layer_mapping,
            min_thickness_m=min_thickness_m,
            max_thickness_m=max_thickness_m,
            include_centerline=include_centerline,
        )
        if len(classified.walls) > max_walls_per_file:
            raise ValueError(
                "items[{}] ({}): {} walls > max_walls_per_file={}".format(
                    i, path.name, len(classified.walls), max_walls_per_file,
                )
            )
        # V2.1 fix fragments persistants : fusion des collinéaires
        # adjacents (gap ≤ 50cm). Réduit le nombre de murs créés et
        # produit des murs visuellement continus en 3D.
        walls_pre_merged = dwg_plan_openings.merge_collinear_walls(
            list(classified.walls),
        )
        plan_records.append({
            "item": it,
            "walls_initial": walls_pre_merged,
            "walls_current": list(walls_pre_merged),  # muté pendant la fusion via openings
            "openings_assigned": [],  # liste de (coupe_opening, host_idx_in_current)
        })

    # --- Lire les coupe_openings, projet world, dédup -------------------
    coupe_openings = _collect_coupe_openings_world(
        kg, section_lines, scale_override, level_elev_by_ref,
    )

    # --- V2 : charger élévations pour vote multi-sources ----------------
    elevations = _load_elevations_from_kg(kg, scale_override=scale_override)

    # --- Distribuer chaque coupe_opening sur son plan + fusion fragments --
    openings_orphan_count = 0
    openings_recovered_via_vote = 0
    for co in coupe_openings:
        # Trouve le(s) plan(s) dont level matche l'élévation de l'opening.
        # En P7 typique : 1 plan par niveau, donc 0 ou 1 match.
        host_plan_idx: Optional[int] = None
        for pidx, pr in enumerate(plan_records):
            level_elev = level_elev_by_ref[pr["item"]["level_ref"]]
            if abs(level_elev - co["level_elevation_m"]) <= 0.05:
                host_plan_idx = pidx
                break
        if host_plan_idx is None:
            openings_orphan_count += 1
            continue
        pr = plan_records[host_plan_idx]
        dx_m = float(pr["item"].get("dx_m", 0.0))
        dy_m = float(pr["item"].get("dy_m", 0.0))
        # Position en world du plan = (co.x_world + dx, co.y_world + dy).
        # Mais : les murs DXF ne sont PAS encore translatés (on translate
        # seulement à la création Revit). Donc on travaille en coords DXF
        # ici (les murs sont dans le repère DXF, l'opening aussi).
        opening_xy = (co["x_world"], co["y_world"])
        new_walls, host_idx = dwg_plan_openings.merge_fragments_around_opening(
            pr["walls_current"], opening_xy,
        )
        pr["walls_current"] = new_walls
        if host_idx is None:
            # V2 fallback : essai mur virtuel + vote élévations.
            recovered = _try_recover_orphan_via_vote(
                co, pr, elevations, section_lines,
            )
            if recovered:
                openings_recovered_via_vote += 1
                continue
            openings_orphan_count += 1
            continue
        pr["openings_assigned"].append({"coupe_opening": co, "host_idx": host_idx})

    # --- Dédup global thicknesses + get_or_create types -----------------
    seen_buckets: Set[int] = set()
    unique_thicknesses_m: List[float] = []
    for pr in plan_records:
        for w in pr["walls_current"]:
            cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
            if cm not in seen_buckets:
                seen_buckets.add(cm)
                unique_thicknesses_m.append(cm / 100.0)

    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    type_entry = registry.get("walls_get_or_create_dxf_type_many")
    if unique_thicknesses_m:
        types_result = type_entry.fn(
            kg=kg, doc=doc,
            thicknesses_m=unique_thicknesses_m,
            bucket_cm=bucket_cm,
            base_type_ref=base_type_ref,
        )
    else:
        types_result = {"types": [], "created_count": 0, "reused_count": 0}
    type_ref_by_cm: Dict[int, str] = {}
    for entry in types_result["types"]:
        cm = int(round(entry["thickness_m"] * 100))
        type_ref_by_cm[cm] = entry["llm_id"]

    # --- Build wall items + walls_create_many ---------------------------
    all_wall_items: List[Dict[str, Any]] = []
    walls_per_file: Dict[str, int] = {}
    walls_merged_count = 0
    thickness_dist: Dict[int, int] = {}
    # Mapping (plan_idx, local_wall_idx_in_walls_current) → global_idx
    # dans all_wall_items.
    wall_global_index: Dict[Tuple[int, int], int] = {}
    for plan_idx, pr in enumerate(plan_records):
        plan_item = pr["item"]
        fp = plan_item["file_path"]
        level_ref = plan_item["level_ref"]
        dx_m = float(plan_item.get("dx_m", 0.0))
        dy_m = float(plan_item.get("dy_m", 0.0))
        height_m = plan_item.get("height_m")
        per_file_count = 0
        for local_idx, w in enumerate(pr["walls_current"]):
            # MergedWall ou WallCandidate : tester la présence de
            # `source_indices` (MergedWall a au moins 2 indices si fusion).
            if hasattr(w, "source_indices") and len(w.source_indices) > 1:
                walls_merged_count += 1
            cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
            wall_type_ref = type_ref_by_cm.get(cm)
            if wall_type_ref is None:
                raise RuntimeError(
                    "Bucket {}cm not in type_ref_by_cm — bug?".format(cm)
                )
            wall_item: Dict[str, Any] = {
                "level_ref": level_ref,
                "wall_type_ref": wall_type_ref,
                "p1": [w.p1[0] + dx_m, w.p1[1] + dy_m],
                "p2": [w.p2[0] + dx_m, w.p2[1] + dy_m],
            }
            if height_m is not None:
                wall_item["height"] = float(height_m)
            wall_global_index[(plan_idx, local_idx)] = len(all_wall_items)
            all_wall_items.append(wall_item)
            per_file_count += 1
            thickness_dist[cm] = thickness_dist.get(cm, 0) + 1
        walls_per_file[fp] = per_file_count

    walls_entry = registry.get("walls_create_many")
    if all_wall_items:
        inner_walls = walls_entry.fn(kg=kg, doc=doc, items=all_wall_items)
    else:
        inner_walls = None

    # Récupérer les wall_llm_ids dans l'ordre de création.
    wall_llm_ids: List[str] = []
    if inner_walls is not None:
        if "llm_ids" in inner_walls:
            wall_llm_ids = list(inner_walls["llm_ids"])
        elif inner_walls.get("contiguous") and "first_llm_id" in inner_walls:
            first = inner_walls["first_llm_id"]
            last = inner_walls["last_llm_id"]
            prefix = first.rsplit("_", 1)[0]
            start = int(first.rsplit("_", 1)[1])
            end = int(last.rsplit("_", 1)[1])
            wall_llm_ids = [
                "{}_{:03d}".format(prefix, n) for n in range(start, end + 1)
            ]

    # --- Préparer les opening items + pre-flight projection --------------
    if door_family_type_ref is None:
        door_family_type_ref = _find_default_family_type(kg, "Doors")
    if window_family_type_ref is None:
        window_family_type_ref = _find_default_family_type(kg, "Windows")

    door_items: List[Dict[str, Any]] = []
    window_items: List[Dict[str, Any]] = []
    openings_unmatched_count = 0
    for plan_idx, pr in enumerate(plan_records):
        plan_item = pr["item"]
        dx_m = float(plan_item.get("dx_m", 0.0))
        dy_m = float(plan_item.get("dy_m", 0.0))
        for assigned in pr["openings_assigned"]:
            co = assigned["coupe_opening"]
            host_idx_local = assigned["host_idx"]
            sill_m = co["sill_m"]
            height_m = co["height_m"]
            if sill_m is None or height_m is None:
                openings_unmatched_count += 1
                continue
            kind = dwg_plan_openings.classify_opening_kind(sill_m, height_m)
            if kind == "unknown":
                openings_unmatched_count += 1
                continue
            global_idx = wall_global_index.get((plan_idx, host_idx_local))
            if global_idx is None or global_idx >= len(wall_llm_ids):
                openings_orphan_count += 1
                continue
            host_wall_ref = wall_llm_ids[global_idx]
            host_wall_item = all_wall_items[global_idx]
            # Pre-flight : projeter la position sur la centerline du mur
            # (clamp ±5cm des extrémités). Évite l'erreur Revit
            # « ouverture ne coupe rien » si la position est légèrement
            # off-curve.
            wall_p1 = (host_wall_item["p1"][0], host_wall_item["p1"][1])
            wall_p2 = (host_wall_item["p2"][0], host_wall_item["p2"][1])
            opening_pos = (co["x_world"] + dx_m, co["y_world"] + dy_m)
            projected = dwg_plan_openings.project_pos_onto_wall_centerline(
                opening_pos, wall_p1, wall_p2,
            )
            family_ref = (
                door_family_type_ref if kind == "door"
                else window_family_type_ref
            )
            if family_ref is None:
                openings_unmatched_count += 1
                continue
            opening_item = {
                "kind": kind,
                "host_wall_ref": host_wall_ref,
                "family_type_ref": family_ref,
                "position": [projected[0], projected[1]],
                "sill_height": sill_m,
            }
            if kind == "door":
                door_items.append(opening_item)
            else:
                window_items.append(opening_item)

    # --- openings_create_many (1 Tx mixte door+window) ------------------
    openings_entry = registry.get("openings_create_many")
    all_opening_create = door_items + window_items
    if all_opening_create and openings_entry is not None:
        inner_openings = openings_entry.fn(
            kg=kg, doc=doc, items=all_opening_create,
        )
    else:
        inner_openings = None

    note = _build_walls_openings_note(
        walls_imported=len(all_wall_items),
        walls_merged=walls_merged_count,
        doors_count=len(door_items),
        windows_count=len(window_items),
        unmatched=openings_unmatched_count,
        orphan=openings_orphan_count,
    )

    return {
        "ok": True,
        "files_count": len(items),
        "walls_imported_total": len(all_wall_items),
        "walls_per_file": walls_per_file,
        "walls_merged_count": walls_merged_count,
        "types_created": types_result["created_count"],
        "types_reused": types_result["reused_count"],
        "types": types_result["types"],
        "coupe_openings_detected": len(coupe_openings),
        "openings_doors_created": len(door_items),
        "openings_windows_created": len(window_items),
        "openings_unmatched_count": openings_unmatched_count,
        "openings_orphan_count": openings_orphan_count,
        "openings_recovered_via_vote": openings_recovered_via_vote,
        "elevations_loaded": list(elevations.keys()),
        "thickness_distribution_global": {
            "{}cm".format(cm): count
            for cm, count in sorted(thickness_dist.items())
        },
        "inner_walls": inner_walls,
        "inner_openings": inner_openings,
        "note": note,
    }


def _build_walls_openings_note(
    *, walls_imported: int, walls_merged: int,
    doors_count: int, windows_count: int,
    unmatched: int, orphan: int,
) -> str:
    """Note actionnable pour le rapport final."""
    parts = [
        "Phase 2.5 livrée : {} murs créés (dont {} fusionnés depuis "
        "fragments via A-GLAZ) + {} portes + {} fenêtres hostées.".format(
            walls_imported, walls_merged, doors_count, windows_count,
        )
    ]
    if unmatched > 0:
        parts.append(
            "{} ouverture(s) non créées : sill/height inconnu (pas de "
            "match coupe ou FamilyType manquant). À résoudre manuellement.".format(
                unmatched,
            )
        )
    if orphan > 0:
        parts.append(
            "{} ouverture(s) orphelines : pas de mur hôte détecté.".format(orphan)
        )
    return " ".join(parts)


@tool(name="dwg_create_continuous_walls_many", tier=2)
def create_continuous_walls_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    bucket_cm: int = 1,
    layer_mapping: Optional[Dict[str, str]] = None,
    scale_override: Optional[float] = None,
    min_thickness_m: float = 0.05,
    max_thickness_m: float = 0.60,
    include_centerline: bool = True,
    base_type_ref: Optional[str] = None,
    max_walls_per_file: int = 500,
    fusion_max_gap_m: float = 4.0,
    section_lines: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """V3 — Crée uniquement des murs **continus** depuis les plans DXF
    (sans openings). Étape 1 d'un pipeline décomposé (user 2026-05-13).

    Pipeline focalisé sur la qualité des murs avant d'ajouter les
    openings. L'agent / user valide visuellement le résultat avant
    d'enchaîner `dwg_add_openings_to_walls_many` (à venir).

    Pipeline :

    1. Classify walls de chaque plan (paires parallèles + centerlines).
    2. `merge_collinear_walls(max_gap=0.5m)` : fusion des fragments
       collés ou séparés par des joints / portes intérieures courtes.
    3. **`merge_fragments_via_elevation_vote(max_gap=4m)`** : pour les
       fragments collinéaires séparés par des gaps de 1-4m (typique :
       fenêtres / portes invisible dans les coupes), check via vote
       élévation si une bande A-WALL continue chevauche le gap →
       fusion si majorité yes. Sinon fragments distincts.
    4. Dédup global thicknesses + `walls_get_or_create_dxf_type_many`.
    5. `walls_create_many` (1 Tx Revit).

    Pas de création d'openings. La récupération des orphans via vote
    et la création des fenêtres/portes seront dans le tool suivant.

    Concepts: dxf, dwg, murs, continus, fusion, vote, élévation, phase 2,
              walls-only, sans openings, étape 1
    Phrases: "crée les murs continus", "import walls only",
             "phase 2 étape 1 murs"
    Similar: dwg_import_walls_typed_many,
             dwg_import_walls_and_openings_typed_many,
             dwg_create_continuous_walls_many

    Args:
        items: liste de dicts `{file_path, level_ref, height_m?,
            dx_m?, dy_m?}`.
        bucket_cm / layer_mapping / scale_override / min_thickness_m /
            max_thickness_m / include_centerline / base_type_ref /
            max_walls_per_file: cf. `dwg_import_walls_typed_many`.
        fusion_max_gap_m: gap max pour fusion via élévation (défaut 4m).

    Returns:
        {"ok": bool, "files_count": int,
         "walls_imported_total": int, "walls_per_file": {path: count},
         "fusion_events": int,  # nb de fusions confirmées par vote
         "types_created": int, "types_reused": int, "types": [...],
         "elevations_loaded": [direction, ...],
         "thickness_distribution_global": {cm: count},
         "inner_walls": ...,
         "note": str}
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if layer_mapping is None:
        layer_mapping = {"A-WALL": "wall"}

    # V3.3 : ensure shared param `claude-in-revit:llm_id` est bound
    # AVANT toute création (bug runtime P7 : murs créés sans llm_id
    # côté Revit → user ne peut pas identifier individuellement).
    # Ouvre sa propre Tx Revit donc obligatoire de l'appeler hors des
    # Tx des sous-tools.
    if doc is not None:
        try:
            from .. import revit_primitives as rp
            rp.ensure_shared_param_binding(doc)
        except Exception:  # noqa: BLE001 — UX surface, jamais fatal.
            pass

    # V3.4 : charger section_lines depuis le KG si non fourni (pour
    # score 3D consensus).
    if section_lines is None:
        section_lines = []
        from .dxf_context import _find_live_context
        nid = _find_live_context(kg)
        if nid is not None:
            ctx = kg.get_node(nid)
            seen_keys = set()
            for sl in ctx.get("section_lines", []):
                key = (sl.get("coupe_path"), tuple(sl.get("plan_p1", [])),
                       tuple(sl.get("plan_p2", [])))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                section_lines.append(sl)

    # --- 1. Pré-validation + classify per plan -------------------------
    plan_records: List[Dict[str, Any]] = []
    level_elev_by_ref: Dict[str, float] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError("items[{}] must be a dict".format(i))
        fp = it.get("file_path")
        level_ref = it.get("level_ref")
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("items[{}]: file_path required".format(i))
        if not isinstance(level_ref, str) or not kg.has_node(level_ref):
            raise ValueError(
                "items[{}]: unknown level_ref {!r}".format(i, level_ref)
            )
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError(
                "items[{}]: file not found: {}".format(i, path)
            )
        _refuse_if_section(path)
        lvl_node = kg.get_node(level_ref)
        level_elev_by_ref[level_ref] = float(lvl_node.get("elevation", 0.0))

        entities, _ = dwg_reader.parse(path, scale_override=scale_override)
        classified = dwg_classifier.classify(
            entities, layer_mapping,
            min_thickness_m=min_thickness_m,
            max_thickness_m=max_thickness_m,
            include_centerline=include_centerline,
        )
        if len(classified.walls) > max_walls_per_file:
            raise ValueError(
                "items[{}] ({}): {} walls > max_walls_per_file={}".format(
                    i, path.name, len(classified.walls), max_walls_per_file,
                )
            )
        # Première passe : merge collinéaires gap ≤ 0.5m (joints, portes
        # intérieures courtes).
        walls_step1 = dwg_plan_openings.merge_collinear_walls(
            list(classified.walls), max_gap_m=0.50,
        )
        plan_records.append({
            "item": it,
            "walls": walls_step1,
        })

    # --- 2. Charger élévations pour vote --------------------------------
    elevations = _load_elevations_from_kg(kg, scale_override=scale_override)

    # --- 3. Fusion via vote élévation pour gaps moyens ------------------
    total_fusion_events = 0
    fusion_events_detail: List[Dict[str, Any]] = []
    for pr in plan_records:
        level_elev = level_elev_by_ref[pr["item"]["level_ref"]]
        height_m = float(pr["item"].get("height_m") or 3.0)
        merged_walls, events = dwg_plan_openings.merge_fragments_via_elevation_vote(
            pr["walls"], elevations,
            level_elev, height_m,
            max_gap_m=fusion_max_gap_m,
        )
        pr["walls"] = merged_walls
        total_fusion_events += len(events)
        fusion_events_detail.extend(events)

    # --- 3bis. Filter auto DÉSACTIVÉ (user : pause après 13 itérations).
    # Suspects flagués via score 3D plus bas pour suppression manuelle.
    total_walls_filtered = 0
    filtered_walls_detail: List[Dict[str, Any]] = []

    # NB : pas de dédup inter-niveaux. La logique « 100% identique
    # = View Range artifact » s'est avérée trop fragile (faux positifs
    # sur étages habitables avec murs porteurs légitimement empilés ou
    # apartments dont le périmètre matche la base sans cloisons). User
    # nettoie manuellement les éventuels artifacts View Range en Revit
    # UI. Module `lib/wall_inter_level_dedup.py` conservé comme
    # toolkit pour audit/check futur (pas appelé automatiquement).

    # --- 4. Dédup thicknesses + get_or_create types ---------------------
    seen_buckets: Set[int] = set()
    unique_thicknesses_m: List[float] = []
    for pr in plan_records:
        for w in pr["walls"]:
            cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
            if cm not in seen_buckets:
                seen_buckets.add(cm)
                unique_thicknesses_m.append(cm / 100.0)

    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    type_entry = registry.get("walls_get_or_create_dxf_type_many")
    if unique_thicknesses_m:
        types_result = type_entry.fn(
            kg=kg, doc=doc,
            thicknesses_m=unique_thicknesses_m,
            bucket_cm=bucket_cm,
            base_type_ref=base_type_ref,
        )
    else:
        types_result = {"types": [], "created_count": 0, "reused_count": 0}
    type_ref_by_cm: Dict[int, str] = {}
    for entry in types_result["types"]:
        cm = int(round(entry["thickness_m"] * 100))
        type_ref_by_cm[cm] = entry["llm_id"]

    # --- 5. Build wall items + walls_create_many -----------------------
    all_wall_items: List[Dict[str, Any]] = []
    walls_per_file: Dict[str, int] = {}
    thickness_dist: Dict[int, int] = {}
    for pr in plan_records:
        plan_item = pr["item"]
        fp = plan_item["file_path"]
        level_ref = plan_item["level_ref"]
        dx_m = float(plan_item.get("dx_m", 0.0))
        dy_m = float(plan_item.get("dy_m", 0.0))
        height_m = plan_item.get("height_m")
        count = 0
        for w in pr["walls"]:
            cm = int(round(w.thickness * 100 / bucket_cm)) * bucket_cm
            wall_type_ref = type_ref_by_cm.get(cm)
            if wall_type_ref is None:
                raise RuntimeError(
                    "Bucket {}cm not in type_ref_by_cm — bug?".format(cm)
                )
            wall_item: Dict[str, Any] = {
                "level_ref": level_ref,
                "wall_type_ref": wall_type_ref,
                "p1": [w.p1[0] + dx_m, w.p1[1] + dy_m],
                "p2": [w.p2[0] + dx_m, w.p2[1] + dy_m],
            }
            if height_m is not None:
                wall_item["height"] = float(height_m)
            all_wall_items.append(wall_item)
            count += 1
            thickness_dist[cm] = thickness_dist.get(cm, 0) + 1
        walls_per_file[fp] = count

    walls_entry = registry.get("walls_create_many")
    if all_wall_items:
        inner_walls = walls_entry.fn(kg=kg, doc=doc, items=all_wall_items)
    else:
        inner_walls = None

    # Récupère les wall_llm_ids dans l'ordre de création.
    wall_llm_ids: List[str] = []
    if inner_walls is not None:
        if "llm_ids" in inner_walls:
            wall_llm_ids = list(inner_walls["llm_ids"])
        elif inner_walls.get("contiguous") and "first_llm_id" in inner_walls:
            first = inner_walls["first_llm_id"]
            last = inner_walls["last_llm_id"]
            prefix = first.rsplit("_", 1)[0]
            start = int(first.rsplit("_", 1)[1])
            end = int(last.rsplit("_", 1)[1])
            wall_llm_ids = [
                "{}_{:03d}".format(prefix, n) for n in range(start, end + 1)
            ]

    # --- V3.4 : score 3D consensus pour identifier les suspects ---------
    # Pour chaque mur, calculer combien de sources le confirment :
    # plan (toujours +1), coupes traversées avec présence confirmée (+1
    # par coupe), élévations pertinentes votant yes confident (+1 par
    # élévation). Score < 2 = mur confirmé UNIQUEMENT par le plan →
    # suspect car aucune autre vue ne le valide. User décide via
    # llm_id côté Revit (Panneau Propriétés > claude-in-revit:llm_id).
    section_walls_by_coupe_v34: Dict[str, List[Dict[str, Any]]] = {}
    for sl in section_lines or []:
        cp = sl.get("coupe_path")
        if not cp or cp in section_walls_by_coupe_v34:
            continue
        cp_path = Path(cp)
        if not cp_path.exists():
            continue
        try:
            ents, _ = dwg_reader.parse(cp_path, scale_override=scale_override)
            sw_list = dwg_section_reader.read_section_walls(ents)
            section_walls_by_coupe_v34[cp] = [
                _section_wall_to_dict(sw) for sw in sw_list
            ]
        except Exception:  # noqa: BLE001
            section_walls_by_coupe_v34[cp] = []

    walls_suspect: List[Dict[str, Any]] = []
    walls_score_distribution: Dict[int, int] = {}
    if all_wall_items:
        # Reconstitue la liste « walls finaux » dans l'ordre des items.
        global_idx = 0
        for pr in plan_records:
            level_elev = level_elev_by_ref[pr["item"]["level_ref"]]
            height_m_local = float(pr["item"].get("height_m") or 3.0)
            for w in pr["walls"]:
                score_info = dwg_plan_openings.compute_3d_consensus_score(
                    w, level_elev, height_m_local,
                    section_lines or [],
                    section_walls_by_coupe_v34,
                    elevations,
                )
                score = score_info["score"]
                walls_score_distribution[score] = walls_score_distribution.get(score, 0) + 1
                if score < 2:
                    llm_id = (
                        wall_llm_ids[global_idx]
                        if global_idx < len(wall_llm_ids) else None
                    )
                    walls_suspect.append({
                        "llm_id": llm_id,
                        "p1": [round(w.p1[0], 3), round(w.p1[1], 3)],
                        "p2": [round(w.p2[0], 3), round(w.p2[1], 3)],
                        "thickness_m": round(w.thickness, 3),
                        "score": score,
                        "section_yes": score_info["section_yes"],
                        "elevation_yes_confident": score_info["elevation_yes_confident"],
                    })
                global_idx += 1

    note = (
        "**Walls-only V3.4** : {} murs continus créés ({} fusions via "
        "vote élévation), {} types DXF custom. **{} suspect(s) à score "
        "3D < 2** (= confirmés seulement par le plan, aucune coupe ni "
        "élévation pertinente ne les valide) — `llm_id` exposés pour "
        "suppression manuelle ciblée par l'user. Pas d'openings — "
        "étape 1 du pipeline décomposé.".format(
            len(all_wall_items),
            total_fusion_events,
            types_result["created_count"] + types_result["reused_count"],
            len(walls_suspect),
        )
    )

    return {
        "ok": True,
        "files_count": len(items),
        "walls_imported_total": len(all_wall_items),
        "walls_per_file": walls_per_file,
        "fusion_events": total_fusion_events,
        "fusion_events_detail": fusion_events_detail[:20],
        "walls_filtered_via_vote": total_walls_filtered,
        "filtered_walls_detail": filtered_walls_detail[:20],
        "walls_suspect_low_3d_consensus": walls_suspect,
        "walls_score_distribution": walls_score_distribution,
        "types_created": types_result["created_count"],
        "types_reused": types_result["reused_count"],
        "types": types_result["types"],
        "elevations_loaded": list(elevations.keys()),
        "thickness_distribution_global": {
            "{}cm".format(cm): count
            for cm, count in sorted(thickness_dist.items())
        },
        "inner_walls": inner_walls,
        "note": note,
    }


def _build_planset_integrity_note(
    report: dwg_coherence.PlansetIntegrityReport,
) -> str:
    """Génère une note actionnable pour l'agent à partir du report."""
    if report.gate_status == "pass":
        return (
            "**Audit OK** : tous les checks sont clean. Le dossier DXF est "
            "auto-cohérent. Tu peux enchaîner avec `levels_reconcile_with_dxf` "
            "et la suite du flow d'import sans réserve."
        )
    if report.gate_status == "needs_user":
        return (
            "**Audit avec avertissements** ({} warning(s)). Présenter à "
            "l'user via `ui_confirm_choices` pour décider si on continue. "
            "Les warnings ne bloquent PAS la création modèle mais doivent "
            "être traçables pour la suite (typiquement : openings "
            "unmatched, drift d'échelle modéré, niveaux manquants dans "
            "certaines coupes)."
        ).format(len(report.warnings))
    # abort
    return (
        "**AUDIT ÉCHOUÉ** ({} erreur(s)). N'enchaîne PAS `levels_create_many` "
        "/ `walls_create_many` / `views_create_*` : présente les erreurs à "
        "l'user pour résolution. Causes typiques : sources de layers "
        "mixtes (re-exporter avec convention uniforme), drift d'échelle "
        "majeur (mauvais matching trait↔coupe), conflit d'élévation pour "
        "un même niveau entre coupes, ou mismatch d'épaisseur > 10cm."
    ).format(len(report.errors))


# ----- Phase 2c — Sols (épaisseur coupes + boundary murs) ---------------
#
# Reproduit le pattern Phase 2a/2b pour les sols. La géométrie des
# dalles n'étant typiquement pas exportée en plan, on dérive :
# - **boundary** depuis l'enveloppe des Wall du niveau (convex hull p1/p2)
# - **épaisseur** depuis les paires de LINEs A-FLOR horizontales des
#   coupes du DxfImportContext
# - **type** : FloorType custom `DXF_FLOOR_<cm>cm` par épaisseur.
# Pas de toiture (= dernier niveau exclu — décision user 2026-05-13).


def _convex_hull_2d(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Andrew's monotone chain. Retourne le polygone fermé (sans répéter
    le 1er point en fin). Si < 3 points uniques, retourne tels quels."""
    pts = sorted(set((round(x, 4), round(y, 4)) for x, y in points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _shoelace_area_2d(points: List[Tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _collect_a_flor_loops_per_level(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
    snap_tol_m: float = 0.20,
) -> Dict[float, Dict[str, Any]]:
    """Lit la géom dalle directement depuis le DXF — convention Revit AIA :
    le contour de la dalle ET les trémies sont sur le **layer `A-FLOR`**
    (ou variantes) sous forme de LINEs séparées. On reconstruit les
    boucles fermées via planar face-tracing et on classifie : la plus
    grande boucle = contour outer, les autres = trous internes.

    Use case observé sur P2 (poteaux-dalles avec trémie) : 10 LINEs
    sur A-FLOR au N1 dont 4 forment le rectangle outer (19.5×16m) et
    6 forment la trémie en U (3.5×7m, avec 2 gaps de ~15cm pour
    landing-accesses d'escalier). `trace_floor_loops_2d(tol=0.20)`
    capture les deux.

    Returns:
        `{level_elev → {"outer": [(x,y), ...], "holes": [[(x,y), ...], ...]}}`.
        Niveaux sans loops détectées absents du dict.
    """
    from .dxf_context import _find_live_context
    plan_level_elev = _plan_path_to_level_elev(kg)
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)

    out: Dict[float, Dict[str, Any]] = {}
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "plan":
            continue
        plan_path = fi.get("path")
        if not plan_path:
            continue
        elev = plan_level_elev.get(plan_path)
        if elev is None:
            continue
        pp = Path(plan_path)
        if not pp.exists():
            continue
        try:
            ents, _meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        # Take LINEs on A-FLOR* (exclude OVHD = projections, not slab edges).
        segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for e in ents:
            if e.kind != "LINE":
                continue
            layer_upper = e.layer.upper()
            if not layer_upper.startswith("A-FLOR"):
                continue
            if "OVHD" in layer_upper:
                continue
            if len(e.coords) < 2:
                continue
            segments.append((
                (float(e.coords[0][0]), float(e.coords[0][1])),
                (float(e.coords[1][0]), float(e.coords[1][1])),
            ))
        if not segments:
            continue
        result = dwg_face_tracing.trace_floor_loops_2d(
            segments, snap_tol_m=snap_tol_m,
        )
        if result is None:
            continue
        out[round(float(elev), 3)] = result
    return out


def _collect_floor_holes_per_level(
    kg: ProjectKG,
    scale_override: Optional[float] = None,
) -> Dict[float, List[Dict[str, Any]]]:
    """Lit les trous (cages d'escalier, patios, atria) depuis les plans DXF
    et les indexe par élévation de niveau.

    Pour chaque plan référencé dans `DxfImportContext.files` :
    1. Détermine son niveau via `_plan_path_to_level_elev` (mapping
       linked_views.view_name ↔ Level.name).
    2. `read_floor_holes_from_plan(entities)` énumère les closed polylines
       sur `A-FLOR-STAIR/OPEN/PATIO/ATRIUM` (exclut OVHD par défaut).
    3. Stocke chaque trou sous forme `{layer, kind, points: [[x,y], ...]}`.

    Returns:
        `{level_elevation_m → [{layer, kind, points}, ...]}`.
        Niveaux sans trous = absents du dict (le caller défaute à []).
    """
    from .dxf_context import _find_live_context
    plan_level_elev = _plan_path_to_level_elev(kg)
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)

    out: Dict[float, List[Dict[str, Any]]] = {}
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "plan":
            continue
        plan_path = fi.get("path")
        if not plan_path:
            continue
        elev = plan_level_elev.get(plan_path)
        if elev is None:
            continue
        pp = Path(plan_path)
        if not pp.exists():
            continue
        try:
            ents, _meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        holes = dwg_section_reader.read_floor_holes_from_plan(ents)
        if not holes:
            continue
        bucket = out.setdefault(round(float(elev), 3), [])
        for h in holes:
            bucket.append({
                "layer": h.layer,
                "kind": h.kind,
                "points": [[float(x), float(y)] for x, y in h.points],
            })
    return out


def _slab_thicknesses_per_level(
    kg: ProjectKG,
    scale_override: Optional[float],
    level_match_tol_m: float = 0.05,
) -> Dict[float, float]:
    """Pour chaque niveau du KG, retourne l'épaisseur de dalle observée
    en coupe (max vote inter-coupes si plusieurs).

    Retourne `{level_elevation_m → thickness_m}`. Un niveau sans dalle
    visible (typiquement le sommet = toiture) est absent.
    """
    from .dxf_context import _find_live_context
    nid = _find_live_context(kg)
    if nid is None:
        return {}
    ctx = kg.get_node(nid)
    # level_elevations cibles depuis le KG
    target_elevs: List[float] = []
    for lid in kg.find_by_type("Level"):
        n = kg.get_node(lid)
        if n.get("deleted_at_turn") is not None:
            continue
        target_elevs.append(float(n.get("elevation", 0.0)))
    target_elevs.sort()

    thickness_votes: Dict[float, List[float]] = {}
    for fi in ctx.get("files") or []:
        if fi.get("kind") != "section":
            continue
        pp = Path(fi.get("path") or "")
        if not pp.exists():
            continue
        try:
            ents, _meta = dwg_reader.parse(pp, scale_override=scale_override)
        except Exception:  # noqa: BLE001
            continue
        slabs = dwg_section_reader.read_section_floor_slabs(ents)
        for s in slabs:
            # Match top_y à un niveau (tol).
            for elev in target_elevs:
                if abs(s.top_y_m - elev) <= level_match_tol_m:
                    thickness_votes.setdefault(elev, []).append(s.thickness_m)
                    break

    out: Dict[float, float] = {}
    for elev, thks in thickness_votes.items():
        # Vote max — défense contre une coupe occasionnellement
        # mal-dimensionnée. Pour P7, tous les votes sont identiques (25cm).
        out[elev] = max(thks)
    return out


@tool(name="dwg_create_floors_many", tier=2)
def create_floors_many(
    kg: ProjectKG,
    doc: Any,
    scale_override: Optional[float] = None,
    bucket_cm: int = 1,
    base_floor_type_ref: Optional[str] = None,
    skip_top_level: bool = True,
    boundary_inflation_m: float = 0.0,
) -> Dict[str, Any]:
    """Phase 2c — Crée les sols par niveau, en dérivant :
    - **épaisseur** depuis les paires A-FLOR horizontales des coupes,
    - **boundary** depuis le convex hull des murs Wall du niveau,
    - **type** : FloorType custom `DXF_FLOOR_<cm>cm`.

    Use case : Phase 2a a créé les murs continus, Phase 2b a posé les
    ouvertures. Ce tool ferme l'enveloppe par les dalles. Le dernier
    niveau (toiture) est skip par défaut (`skip_top_level=True`).

    Concepts: sol, dalle, floor, slab, plancher, phase 2c, dxf, import,
              boundary, convex hull, épaisseur
    Phrases: "crée les sols", "ajoute les dalles", "phase 2c",
             "import des planchers"
    Similar: dwg_create_continuous_walls_many,
             dwg_add_openings_to_walls_many, floors_create_many

    Args:
        scale_override: cf. dwg_inspect.
        bucket_cm: granularité du bucketing épaisseur (défaut 1).
        base_floor_type_ref: FloorType à dupliquer. Sinon auto.
        skip_top_level: ne pas créer le sol au niveau le plus haut
            (= toiture, à traiter séparément). Défaut True.
        boundary_inflation_m: dilatation isotrope du convex hull pour
            inclure une marge autour des murs (typiquement 0, set à
            ~0.10m si tu veux que le sol déborde des murs extérieurs).

    Returns:
        {ok, floors_created_count, floors_per_level, types_created,
         types_reused, types, inner_floors, note}
    """
    if doc is not None:
        try:
            from .. import revit_primitives as rp
            rp.ensure_shared_param_binding(doc)
        except Exception:  # noqa: BLE001
            pass

    # 1. Épaisseur par niveau (depuis coupes).
    thk_by_elev = _slab_thicknesses_per_level(kg, scale_override)
    if not thk_by_elev:
        return {
            "ok": True,
            "floors_created_count": 0,
            "floors_per_level": {},
            "types_created": 0,
            "types_reused": 0,
            "types": [],
            "inner_floors": None,
            "note": (
                "Aucune dalle détectée dans les coupes (A-FLOR horizontales). "
                "Soit le DXF n'inclut pas les sols, soit la convention de "
                "layer diffère — vérifier dwg_inspect_sections."
            ),
        }

    # 2. Murs vivants par niveau (level_ref → list of (p1, p2)).
    level_to_ref: Dict[float, str] = {}
    for lid in kg.find_by_type("Level"):
        n = kg.get_node(lid)
        if n.get("deleted_at_turn") is not None:
            continue
        level_to_ref[round(float(n.get("elevation", 0.0)), 3)] = lid

    walls_by_level: Dict[float, List[Tuple[Tuple[float, float], Tuple[float, float]]]] = {}
    for nid in kg.find_by_type("Wall"):
        n = kg.get_node(nid)
        if n.get("deleted_at_turn") is not None:
            continue
        lvl_ref = n.get("level_ref")
        if lvl_ref is None:
            continue
        lvl_node = kg.get_node(lvl_ref)
        elev = round(float(lvl_node.get("elevation", 0.0)), 3)
        p1 = n.get("p1")
        p2 = n.get("p2")
        if not p1 or not p2:
            continue
        walls_by_level.setdefault(elev, []).append(
            ((float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1]))),
        )

    # 3. Détermine la liste des niveaux à traiter (skip toiture).
    elevs_with_walls = sorted(walls_by_level.keys())
    if not elevs_with_walls:
        return {
            "ok": True,
            "floors_created_count": 0,
            "floors_per_level": {},
            "types_created": 0,
            "types_reused": 0,
            "types": [],
            "inner_floors": None,
            "note": (
                "Aucun mur vivant dans le KG par niveau — Phase 2a n'a "
                "pas été exécutée ou les murs ont été supprimés. Sans "
                "murs, pas de boundary pour les sols."
            ),
        }
    target_elevs = [e for e in elevs_with_walls if e in thk_by_elev]
    if skip_top_level and target_elevs:
        # Le niveau le plus haut traité par Phase 2a = toiture probable.
        # On le retire seulement s'il est aussi le plus haut tout court
        # (= pas de niveau au-dessus). Conservateur : on garde l'avant-dernier.
        # En pratique pour P7, on a niveaux 0/1/2, walls aux 0+1, slabs aux 0+1
        # → on traite les 2 niveaux (pas de skip nécessaire).
        # On ne skip QUE si le top wall-level matche aussi le top slab-level
        # *et* qu'il existe un niveau au-dessus dans le KG.
        all_kg_elevs = sorted(level_to_ref.keys())
        max_walls = target_elevs[-1]
        if max_walls < all_kg_elevs[-1] - 1e-3:
            # Il y a un niveau au-dessus du dernier mur → garder.
            pass
        # Sinon : pas de skip (P7 cas typique).

    # 3bis. Détecte les trous (cages d'escalier, patios, atria) par niveau
    # depuis les plans DXF — closed polylines sur A-FLOR-STAIR/OPEN/PATIO.
    # `holes_by_elev[elev]` = list of {layer, kind, points}. Plans sans trous
    # absents du dict.
    holes_by_elev = _collect_floor_holes_per_level(kg, scale_override)

    # 3ter. Tente la lecture directe de la géom dalle depuis le DXF —
    # convention Revit AIA met le contour ET les trémies sur layer A-FLOR
    # comme LINEs séparées. Si détecté, c'est la source de vérité
    # (priorité sur le face-tracing des murs, qui peut diverger : saillies,
    # cantilever, retraits). Validé sur P2 poteaux-dalles avec trémie.
    a_flor_loops_by_elev = _collect_a_flor_loops_per_level(kg, scale_override)

    # 4. Build per-level items : boundary, thickness, level_ref, holes.
    floor_items_raw: List[Dict[str, Any]] = []
    thicknesses_unique: Set[int] = set()
    holes_count_by_kind: Dict[str, int] = {}
    boundary_methods: Dict[str, int] = {
        "a_flor_lines": 0,  # priorité 1 — géom dalle directe depuis DXF
        "face_tracing": 0,  # priorité 2 — contour mur via planar
        "convex_hull_fallback": 0,  # priorité 3 — dernier recours
    }
    a_flor_holes_added = 0
    for elev in target_elevs:
        thk = thk_by_elev[elev]
        wall_pts: List[Tuple[float, float]] = []
        for p1, p2 in walls_by_level[elev]:
            wall_pts.append(p1)
            wall_pts.append(p2)
        hull = _convex_hull_2d(wall_pts)
        if len(hull) < 3:
            continue

        # Priorité 1 : géom dalle directe depuis A-FLOR LINEs si détectée.
        # C'est la source de vérité — le DXF peut avoir des saillies /
        # cantilever / retraits où la dalle diverge des murs.
        a_flor_loops = a_flor_loops_by_elev.get(round(elev, 3))
        boundary_method = None
        a_flor_inner_holes: List[List[List[float]]] = []
        if a_flor_loops and len(a_flor_loops["outer"]) >= 3:
            hull = a_flor_loops["outer"]
            a_flor_inner_holes = [
                [[round(p[0], 4), round(p[1], 4)] for p in h]
                for h in a_flor_loops["holes"]
            ]
            boundary_method = "a_flor_lines"
            a_flor_holes_added += len(a_flor_inner_holes)
        else:
            # Priorité 2-3 : face-tracing sur murs + fallback convex hull.
            outer_pts, fallback_method = dwg_face_tracing.trace_outer_boundary_with_fallback(
                wall_segments=list(walls_by_level[elev]),
                fallback=hull,
            )
            hull = outer_pts if fallback_method == "face_tracing" else hull
            boundary_method = fallback_method
        boundary_methods[boundary_method] = boundary_methods.get(boundary_method, 0) + 1
        if len(hull) < 3:
            continue
        # Inflation isotrope (optionnelle).
        if boundary_inflation_m > 0:
            cx = sum(p[0] for p in hull) / len(hull)
            cy = sum(p[1] for p in hull) / len(hull)
            inflated: List[Tuple[float, float]] = []
            for x, y in hull:
                vx = x - cx
                vy = y - cy
                length = math.hypot(vx, vy)
                if length < 1e-6:
                    inflated.append((x, y))
                    continue
                k = (length + boundary_inflation_m) / length
                inflated.append((cx + vx * k, cy + vy * k))
            hull = inflated
        boundary = [[round(p[0], 4), round(p[1], 4)] for p in hull]
        # Holes union :
        # - closed polylines sur A-FLOR-STAIR/OPEN/PATIO (existing detection
        #   par layer name)
        # - inner faces reconstruites depuis A-FLOR LINEs (P2-style)
        # Si A-FLOR LINEs ont déjà donné le outer (priority 1), les holes
        # de cette source sont aussi pris ; sinon, juste les closed polylines.
        raw_holes = holes_by_elev.get(round(elev, 3), [])
        holes_pts: List[List[List[float]]] = list(a_flor_inner_holes)
        for h in raw_holes:
            holes_pts.append([[round(p[0], 4), round(p[1], 4)] for p in h["points"]])
            holes_count_by_kind[h["kind"]] = holes_count_by_kind.get(h["kind"], 0) + 1
        if a_flor_inner_holes:
            # A-FLOR LINE-based holes : on les compte sous "a_flor_inner"
            # (pas de classification fine — la sémantique vient de la
            # géom, pas du nom de layer).
            holes_count_by_kind["a_flor_inner"] = (
                holes_count_by_kind.get("a_flor_inner", 0) + len(a_flor_inner_holes)
            )
        # Net area : outer (shoelace) − sum(hole areas).
        gross = _shoelace_area_2d(hull)
        net = gross
        for h_pts in holes_pts:
            net -= _shoelace_area_2d([(p[0], p[1]) for p in h_pts])
        area_m2 = round(max(net, 0.0), 4)
        thk_cm = int(round(thk * 100 / bucket_cm)) * bucket_cm
        thicknesses_unique.add(thk_cm)
        floor_items_raw.append({
            "level_ref": level_to_ref[elev],
            "level_elevation_m": elev,
            "thickness_m": thk_cm / 100.0,
            "boundary": boundary,
            "holes": holes_pts,
            "area_m2": area_m2,
        })

    if not floor_items_raw:
        return {
            "ok": True,
            "floors_created_count": 0,
            "floors_per_level": {},
            "types_created": 0,
            "types_reused": 0,
            "types": [],
            "inner_floors": None,
            "note": (
                "Aucun sol candidat construit (épaisseur ↔ niveau matchant "
                "+ murs disponibles). Vérifier dwg_inspect_sections et "
                "Phase 2a."
            ),
        }

    # 5. Bulk get_or_create FloorType DXF.
    from .. import llm_protocol
    registry = llm_protocol.get_registry()
    ft_entry = registry.get("floors_get_or_create_dxf_type_many")
    types_result: Dict[str, Any] = {"types": [], "created_count": 0, "reused_count": 0}
    type_ref_by_cm: Dict[int, str] = {}
    if ft_entry is not None:
        types_result = ft_entry.fn(
            kg=kg, doc=doc,
            thicknesses_m=[c / 100.0 for c in sorted(thicknesses_unique)],
            bucket_cm=bucket_cm,
            base_type_ref=base_floor_type_ref,
        )
        for t in types_result["types"]:
            cm = int(round(t["thickness_m"] * 100))
            type_ref_by_cm[cm] = t["llm_id"]

    # 6. Build floors_create_many items.
    floors_items: List[Dict[str, Any]] = []
    floors_per_level: Dict[float, int] = {}
    for it in floor_items_raw:
        thk_cm = int(round(it["thickness_m"] * 100))
        type_ref = type_ref_by_cm.get(thk_cm)
        if type_ref is None:
            continue
        item: Dict[str, Any] = {
            "floor_type_ref": type_ref,
            "level_ref": it["level_ref"],
            "boundary": it["boundary"],
            "area_m2": it["area_m2"],
        }
        if it.get("holes"):
            item["holes"] = it["holes"]
        floors_items.append(item)
        floors_per_level[it["level_elevation_m"]] = (
            floors_per_level.get(it["level_elevation_m"], 0) + 1
        )

    # 7. Crée les Floor via floors_create_many.
    floors_entry = registry.get("floors_create_many")
    inner_floors = None
    if floors_items and floors_entry is not None:
        inner_floors = floors_entry.fn(
            kg=kg, doc=doc, items=floors_items,
        )

    holes_total = sum(holes_count_by_kind.values())
    holes_note = (
        " {} trou(s) détecté(s) ({}).".format(
            holes_total,
            ", ".join("{}={}".format(k, v) for k, v in sorted(holes_count_by_kind.items())),
        ) if holes_total else ""
    )
    note = (
        "Phase 2c : {} sol(s) créé(s) sur {} niveau(x). "
        "{} FloorType DXF créé(s) / {} réutilisé(s). Épaisseurs : {}.{}"
        .format(
            len(floors_items), len(floors_per_level),
            types_result["created_count"], types_result["reused_count"],
            sorted(thicknesses_unique),
            holes_note,
        )
    )

    return {
        "ok": True,
        "floors_created_count": len(floors_items),
        "floors_per_level": {str(k): v for k, v in floors_per_level.items()},
        "types_created": types_result["created_count"],
        "types_reused": types_result["reused_count"],
        "types": types_result["types"],
        "holes_count_by_kind": holes_count_by_kind,
        "boundary_methods": boundary_methods,
        "inner_floors": inner_floors,
        "note": note,
    }


# ----- Phase 2d : import des poteaux depuis les plans DXF -------------
#
# Pipeline aligned avec Phase 2a (walls) et Phase 2c (floors) :
# 1. Pour chaque plan, extraire les INSERTs S-COLS via
#    `dwg_plan_columns.extract_columns_from_entities` (1 candidate par
#    instance).
# 2. Aggréger inter-niveaux par position : `aggregate_columns_across_plans`
#    retourne 1 colonne aggrégée par grille-point unique, avec
#    base/top elevation déduits de l'apparition à chaque niveau.
# 3. Get-or-create un ColumnType placeholder `DXF_COL_<famille>_<type>`
#    par paire `(family, type)` unique via
#    `columns_get_or_create_dxf_type_many`. Aucune assomption matériau.
# 4. Bulk create via `columns_create_many` (1 Tx Revit, 1 Tx KG).
#
# Le module gère naturellement le View Range Revit (qui fait apparaître
# 60 INSERTs au niveau intermédiaire d'un projet 3 niveaux) en aggrégeant
# par position avant création — chaque position physique → 1 colonne.


@tool(name="dwg_create_columns_many", tier=2)
def create_columns_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    scale_override: Optional[float] = None,
    base_column_type_ref: Optional[str] = None,
    default_storey_height_m: float = 3.0,
    position_merge_tol_m: float = 0.05,
) -> Dict[str, Any]:
    """Phase 2d — Crée les poteaux depuis les S-COLS INSERTs des plans.

    Pipeline :

    1. Parse chaque plan, extrait les INSERTs S-COLS → `ColumnCandidate`
       (position, family_name, type_name, rotation, block_name).
    2. **Aggrège inter-niveaux par position** (à `position_merge_tol_m`
       près) : pour chaque grille-point unique, retient le niveau le
       plus bas comme base et déduit la hauteur du niveau suivant.
       Évite la création de doublons quand le View Range Revit affiche
       plusieurs niveaux dans le même plan.
    3. Get-or-create un `ColumnType` placeholder `DXF_COL_<famille>_<type>`
       par paire `(family, type)` unique (cf.
       `columns_get_or_create_dxf_type_many` — duplique un poteau
       générique du projet, aucune assomption matériau).
    4. Bulk-call `columns_create_many` (1 Tx Revit, 1 Tx KG).

    **Aucune assomption sur le matériau** : couvre HEA acier, béton,
    bois, etc. (cf. `dwg_plan_columns.parse_column_block_name`). Les
    types DXF_COL_* sont des placeholders traçables ; l'user remappe
    vers les vraies familles après import.

    Concepts: poteau, column, dxf, import, phase 2d, plans, s-cols,
              HEA, béton, bois, family, type, placeholder, bulk
    Phrases: "crée les poteaux", "import des colonnes",
             "phase 2d", "import poteaux dxf"
    Similar: dwg_create_continuous_walls_many, dwg_create_floors_many,
             columns_create_many

    Args:
        items: liste de dicts `{file_path, level_ref}`. Chaque item =
            un plan DXF associé à son niveau Revit (llm_id du Level).
            `file_path` doit pointer vers un DXF de plan (pas de
            coupe / élévation). L'ordre par level_elevation est
            indifférent — la fonction trie elle-même.
        scale_override: cf. `dwg_inspect`.
        base_column_type_ref: llm_id d'un ColumnType template à
            dupliquer pour les placeholders DXF_COL_*. Si None
            (défaut), cherche un FamilySymbol de poteau générique
            chargé dans le projet (cf.
            `columns_get_or_create_dxf_type_many`).
        default_storey_height_m: hauteur d'étage par défaut quand
            une colonne n'apparaît qu'à un seul niveau (pas d'info
            top). Défaut 3 m.
        position_merge_tol_m: tolérance pour fusionner des positions
            de colonnes quasi-identiques (drift d'export DXF).
            Défaut 5 cm.

    Returns:
        ``{"ok": bool, "files_count": int, "candidates_total": int,
            "aggregated_count": int, "columns_created_count": int,
            "types_created": int, "types_reused": int,
            "types": [{family_name, type_name, kind, llm_id, ...}, ...],
            "columns_per_level": {elevation: count},
            "inner_columns": dict | None, "note": str}``
    """
    from .. import dwg_plan_columns
    from . import columns as columns_tool

    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")

    # --- 1. Pré-validation + collect candidates per plan ----------------
    plan_records: List[Dict[str, Any]] = []
    level_elev_by_ref: Dict[str, float] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError("items[{}] must be a dict".format(i))
        fp = it.get("file_path")
        level_ref = it.get("level_ref")
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("items[{}]: file_path required".format(i))
        if not isinstance(level_ref, str) or not kg.has_node(level_ref):
            raise ValueError(
                "items[{}]: unknown level_ref {!r}".format(i, level_ref)
            )
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError(
                "items[{}]: file not found: {}".format(i, path)
            )
        _refuse_if_section(path)
        lvl_node = kg.get_node(level_ref)
        level_elev_by_ref[level_ref] = float(lvl_node.get("elevation", 0.0))

        entities, _ = dwg_reader.parse(path, scale_override=scale_override)
        candidates = dwg_plan_columns.extract_columns_from_entities(entities)
        plan_records.append({
            "item": it,
            "candidates": candidates,
        })

    candidates_total = sum(len(pr["candidates"]) for pr in plan_records)
    if candidates_total == 0:
        return {
            "ok": True,
            "files_count": len(items),
            "candidates_total": 0,
            "aggregated_count": 0,
            "columns_created_count": 0,
            "types_created": 0,
            "types_reused": 0,
            "types": [],
            "columns_per_level": {},
            "inner_columns": None,
            "note": (
                "Aucun INSERT S-COLS détecté dans les plans. "
                "Soit le projet n'a pas de poteaux, soit la convention "
                "de layer diffère (S-COLS attendu — vérifier dwg_inspect)."
            ),
        }

    # --- 2. Per-level dedup (1 colonne par étage par grille-point) -----
    # Convention Revit structurelle : chaque storey = un élément
    # distinct (jonctions physiques séparées). Pour P2 : 30 par niveau
    # × 3 niveaux = 90 colonnes.
    columns_by_level_elev: List[Tuple[float, List[Any]]] = [
        (level_elev_by_ref[pr["item"]["level_ref"]], pr["candidates"])
        for pr in plan_records
    ]
    aggregated = dwg_plan_columns.dedup_columns_within_plans(
        columns_by_level_elev,
        position_merge_tol_m=position_merge_tol_m,
        default_storey_height_m=default_storey_height_m,
    )

    if not aggregated:
        return {
            "ok": True,
            "files_count": len(items),
            "candidates_total": candidates_total,
            "aggregated_count": 0,
            "columns_created_count": 0,
            "types_created": 0,
            "types_reused": 0,
            "types": [],
            "columns_per_level": {},
            "inner_columns": None,
            "note": (
                "Aggregation a retourné 0 colonnes — vérifier "
                "position_merge_tol_m et le contenu DXF."
            ),
        }

    # --- 3. Get-or-create ColumnType placeholders ---------------------
    # Pour chaque (family, type) unique, on prend les dimensions bbox
    # de la 1re occurrence rencontrée — toutes les instances d'un même
    # (family, type) devraient avoir le même bbox (mêmes définitions
    # BLOCK).
    type_specs: List[Dict[str, Any]] = []
    seen_type_keys: Set[Tuple[str, str]] = set()
    for col in aggregated:
        key = (col.family_name, col.type_name)
        if key in seen_type_keys:
            continue
        seen_type_keys.add(key)
        type_specs.append({
            "family_name": col.family_name,
            "type_name": col.type_name,
            "kind": "structural",
            "width_m": col.width_m,
            "depth_m": col.depth_m,
        })
    types_result = columns_tool.get_or_create_dxf_type_many(
        kg=kg, doc=doc, types=type_specs,
        base_type_ref=base_column_type_ref,
    )
    # Map (original_family, original_type) → llm_id du placeholder.
    type_ref_by_original: Dict[Tuple[str, str], str] = {}
    for col_t in types_result["types"]:
        # `family_name` ici = la family d'origine conservée par
        # get_or_create_dxf_type_many ; on a besoin du couple original.
        # Le placeholder name encode (family, type) — on re-parse.
        # En pratique, get_or_create conserve `family_name` = original,
        # et `type_name` = `DXF_COL_<family>_<type>`. On retrouve
        # l'original_type via le suffixe.
        placeholder = col_t["type_name"]  # DXF_COL_<fam>_<type>
        family_in_node = col_t["family_name"]
        # Extract original type by stripping prefix and family.
        prefix = "DXF_COL_"
        rest = placeholder[len(prefix):] if placeholder.startswith(prefix) else placeholder
        # Le family_name peut contenir des `_` (sanitization) → on
        # retrouve l'original_type en cherchant la version sanitizée
        # de family puis le `_` séparateur.
        type_ref_by_original[(family_in_node, col_t["type_name"])] = col_t["llm_id"]
    # Construire le mapping placeholder_name → llm_id, plus simple.
    type_ref_by_placeholder: Dict[str, str] = {
        col_t["type_name"]: col_t["llm_id"]
        for col_t in types_result["types"]
    }

    # --- 4. Build column items + columns_create_many -----------------
    # Trouver, pour chaque AggregatedColumn, le level_ref de sa base.
    elev_to_level_ref: Dict[float, str] = {
        round(e, 4): lr for lr, e in level_elev_by_ref.items()
    }

    column_items: List[Dict[str, Any]] = []
    columns_per_level: Dict[float, int] = {}
    skipped_no_base_level = 0
    for col in aggregated:
        base_elev_key = round(col.base_level_elev_m, 4)
        base_level_ref = elev_to_level_ref.get(base_elev_key)
        if base_level_ref is None:
            # Pas de niveau Revit correspondant à la base elevation —
            # peut arriver si user a passé des plans qui ne couvrent
            # pas tous les niveaux. Skip avec compteur.
            skipped_no_base_level += 1
            continue
        # Le placeholder name est `DXF_COL_<sanitized_family>_<sanitized_type>`.
        from ..tools.columns import _dxf_column_type_name
        placeholder_name = _dxf_column_type_name(
            col.family_name, col.type_name,
        )
        type_ref = type_ref_by_placeholder.get(placeholder_name)
        if type_ref is None:
            # Type pas trouvé — devrait pas arriver puisqu'on vient de
            # le créer ci-dessus.
            skipped_no_base_level += 1
            continue
        height_m = col.top_level_elev_m - col.base_level_elev_m
        column_items.append({
            "level_ref": base_level_ref,
            "column_type_ref": type_ref,
            "position": [col.position[0], col.position[1]],
            "height": height_m,
        })
        columns_per_level[col.base_level_elev_m] = (
            columns_per_level.get(col.base_level_elev_m, 0) + 1
        )

    inner_columns = None
    if column_items:
        inner_columns = columns_tool.create_many(
            kg=kg, doc=doc, items=column_items,
        )

    note = (
        "Phase 2d : {} poteau(x) créé(s) sur {} grille-points uniques "
        "({} candidates parsés depuis {} plan(s)). "
        "{} ColumnType DXF_COL_* créé(s) / {} réutilisé(s)."
        .format(
            len(column_items), len(aggregated), candidates_total,
            len(items),
            types_result["created_count"], types_result["reused_count"],
        )
    )
    if skipped_no_base_level:
        note += " {} colonne(s) skippée(s) (level_ref base manquant).".format(
            skipped_no_base_level,
        )

    return {
        "ok": True,
        "files_count": len(items),
        "candidates_total": candidates_total,
        "aggregated_count": len(aggregated),
        "columns_created_count": len(column_items),
        "columns_skipped_no_base_level": skipped_no_base_level,
        "types_created": types_result["created_count"],
        "types_reused": types_result["reused_count"],
        "types": types_result["types"],
        "columns_per_level": {str(k): v for k, v in columns_per_level.items()},
        "inner_columns": inner_columns,
        "note": note,
    }


# ----- Reset des imports DXF (Phase 2 maintenance) ----------------------
#
# Outil de nettoyage : soft-delete dans le KG tous les Wall/Window/Door
# vivants + WallType/FamilyType DXF_*, et supprime côté Revit en une
# seule transaction. Utile entre deux itérations d'import pour repartir
# vierge sans avoir à effacer manuellement dans Revit + supprimer le
# fichier .kg.json. Atomicité KG+Revit : si le commit Revit échoue, le
# KG rollback via `kg.transaction()`.


_DXF_TYPE_PREFIXES = ("DXF_WALL_", "DXF_WIN_", "DXF_DOOR_", "DXF_FLOOR_", "DXF_COL_")


def _is_dxf_type_node(attrs: Dict[str, Any]) -> bool:
    """Match WallType/FloorType/FamilyType/ColumnType DXF_* (Phase 2a/b/c/d)."""
    t = attrs.get("_type")
    if t == "WallType":
        name = attrs.get("name") or ""
        return name.startswith("DXF_WALL_")
    if t == "FloorType":
        name = attrs.get("name") or ""
        return name.startswith("DXF_FLOOR_")
    if t == "FamilyType":
        tn = attrs.get("type_name") or ""
        return tn.startswith("DXF_WIN_") or tn.startswith("DXF_DOOR_")
    if t == "ColumnType":
        tn = attrs.get("type_name") or ""
        return tn.startswith("DXF_COL_")
    return False


@tool(name="kg_reset_dxf_imports", tier=2)
def kg_reset_dxf_imports(
    kg: ProjectKG,
    doc: Any = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reset des imports DXF : soft-delete tous les Wall/Window/Door/Floor
    vivants du KG + WallType `DXF_WALL_*` + FloorType `DXF_FLOOR_*` +
    FamilyType `DXF_WIN_*`/`DXF_DOOR_*`, et supprime côté Revit en une
    seule transaction.

    Cible : repartir vierge entre deux itérations d'import sans toucher
    manuellement le `.kg.json` ni purger Revit à la main. Atomique :
    si le commit Revit échoue, le KG rollback.

    **Ne touche pas** : Level, Room, DxfImportContext, View — seuls
    les artefacts de modélisation Phase 2 (murs + ouvertures + sols)
    et leurs types custom sont impactés. Pour un reset total, supprimer
    le `.kg.json` à la main.

    Si un node n'a pas de binding `revit_id` ou si l'élément a déjà
    disparu côté Revit (binding périmé), le KG est soft-delete quand
    même (pas de crash, comptabilisé dans `already_gone`).

    Concepts: reset, cleanup, nettoyage, dxf, import, soft-delete,
              purge, vierge, repartir
    Phrases: "reset des imports DXF", "nettoie ce que j'ai importé",
             "repars à zéro", "supprime tous les murs/fenêtres DXF",
             "vide le KG des imports"
    Similar: walls_delete_many, dwg_import_clear_context

    Args:
        dry_run: si True, n'effectue aucune mutation, retourne juste
            l'inventaire de ce qui serait supprimé. Défaut False.

    Returns:
        {"ok": bool, "dry_run": bool,
         "walls": {"count": int, "llm_ids": [...]},
         "windows": {"count": int, "llm_ids": [...]},
         "doors": {"count": int, "llm_ids": [...]},
         "wall_types": {"count": int, "llm_ids": [...]},
         "family_types": {"count": int, "llm_ids": [...]},
         "revit_deleted": int,        # nb d'éléments Revit effectivement supprimés
         "already_gone": int,         # nb sans binding ou binding périmé
         "deleted_at_turn": int | None}
    """
    # --- 1. Inventaire : nodes vivants à reset ------------------------------
    walls_ids = kg.find_by_type("Wall")
    windows_ids = kg.find_by_type("Window")
    doors_ids = kg.find_by_type("Door")
    floors_ids = kg.find_by_type("Floor")

    wall_types_ids: List[str] = []
    floor_types_ids: List[str] = []
    family_types_ids: List[str] = []
    for nid in kg.find_by_type("WallType"):
        attrs = kg.get_node(nid)
        if _is_dxf_type_node(attrs):
            wall_types_ids.append(nid)
    for nid in kg.find_by_type("FloorType"):
        attrs = kg.get_node(nid)
        if _is_dxf_type_node(attrs):
            floor_types_ids.append(nid)
    for nid in kg.find_by_type("FamilyType"):
        attrs = kg.get_node(nid)
        if _is_dxf_type_node(attrs):
            family_types_ids.append(nid)

    inventory = {
        "walls": {"count": len(walls_ids), "llm_ids": walls_ids},
        "windows": {"count": len(windows_ids), "llm_ids": windows_ids},
        "doors": {"count": len(doors_ids), "llm_ids": doors_ids},
        "floors": {"count": len(floors_ids), "llm_ids": floors_ids},
        "wall_types": {"count": len(wall_types_ids), "llm_ids": wall_types_ids},
        "floor_types": {"count": len(floor_types_ids), "llm_ids": floor_types_ids},
        "family_types": {
            "count": len(family_types_ids), "llm_ids": family_types_ids,
        },
    }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "revit_deleted": 0,
            "already_gone": 0,
            "deleted_at_turn": None,
            **inventory,
        }

    # Ordre de suppression : instances d'abord (Window/Door dépendent du
    # host Wall), puis Walls, puis Types. Côté Revit, supprimer un host
    # supprime ses hosted ; on supprime explicitement quand même pour
    # propager les soft-delete KG proprement.
    ordered = (
        windows_ids + doors_ids + floors_ids + walls_ids
        + wall_types_ids + floor_types_ids + family_types_ids
    )

    if doc is None:
        # Hors-Revit (CLI / tests) — KG-only soft-delete.
        with kg.transaction():
            for nid in ordered:
                kg.soft_delete(nid)
        return {
            "ok": True,
            "dry_run": False,
            "revit_deleted": 0,
            "already_gone": len(ordered),
            "deleted_at_turn": kg.turn,
            **inventory,
        }

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    revit_deleted = 0
    already_gone = 0

    with kg.transaction():
        with rp.transaction(doc, "kg_reset_dxf_imports"):
            for nid in ordered:
                eid_raw = kg.get_revit_id(nid)
                if eid_raw is None:
                    kg.soft_delete(nid)
                    already_gone += 1
                    continue
                try:
                    element = doc.GetElement(ElementId(eid_raw))
                except Exception:  # noqa: BLE001
                    element = None
                if element is None:
                    kg.soft_delete(nid)
                    already_gone += 1
                    continue
                try:
                    doc.Delete(ElementId(eid_raw))
                    revit_deleted += 1
                except Exception:  # noqa: BLE001 — Revit refuse parfois
                    # Hosted déjà supprimée en cascade par le host, ou
                    # contrainte. Soft-delete KG quand même.
                    already_gone += 1
                kg.soft_delete(nid)

    return {
        "ok": True,
        "dry_run": False,
        "revit_deleted": revit_deleted,
        "already_gone": already_gone,
        "deleted_at_turn": kg.turn,
        **inventory,
    }


# =====================================================================
# Meta-tools : `dwg_import_project_audit` / `dwg_import_project_execute`
# =====================================================================
#
# Pipeline complet « import projet DXF → BIM Revit » en 2 stages, pour
# que le LLM ne paie pas la latence + tokens d'un orchestrating ~14 tools
# step-by-step. Le human-in-the-loop reste préservé entre audit et execute
# (dialogs ui_confirm_* pour warnings + reconciliation niveaux).
#
# Structure interne : un helper privé `_meta_phase*_*(...)` par étape,
# chacun avec un contrat I/O typé. Ajouter une micro-step = ajouter un
# helper + 1 ligne dans `import_project_execute`. Pas de framework, juste
# du Python sequentiel lisible.


def _meta_classify_files(directory_path: Path) -> Dict[str, List[Path]]:
    """Triage les .dxf du dossier par kind via dwg_section_reader.classify_dxf.

    Retourne {"plans": [Path], "coupes": [Path], "elevations": [Path]}.
    Ignore les fichiers `unknown`. Tolérant : si un parse échoue, le fichier
    est skippé silencieusement (visible dans inspect_sections par ailleurs).
    """
    out: Dict[str, List[Path]] = {"plans": [], "coupes": [], "elevations": []}
    for p in sorted(directory_path.glob("*.dxf")):
        try:
            _ents, meta = dwg_reader.parse(p)
            kind, _ev = dwg_section_reader.classify_dxf(meta["layers"], file_name=p.name)
        except Exception:  # noqa: BLE001
            continue
        if kind == "plan":
            out["plans"].append(p)
        elif kind == "section":
            out["coupes"].append(p)
        elif kind == "elevation":
            out["elevations"].append(p)
    return out


def _meta_build_level_actions_proposed(reconcile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convertit le payload `levels_reconcile_with_dxf` en liste d'actions
    structurées que l'execute peut consommer.

    Sortie : liste de `{action, name, elevation_m, [existing_llm_id]}` avec
    action ∈ {create, update, skip}. L'execute applique uniquement les
    `create` (V0) ; `update` est un placeholder pour V1 (élévation
    différente, nécessite levels_set_elevation). `skip` est informatif.
    """
    actions: List[Dict[str, Any]] = []
    for lvl in reconcile.get("missing_in_project") or []:
        actions.append({
            "action": "create",
            "name": lvl["name"],
            "elevation_m": float(lvl["elevation_m"]),
        })
    for nm in reconcile.get("name_only_matches") or []:
        actions.append({
            "action": "update",
            "name": nm["name"],
            "elevation_m": float(nm["coupe_elevation_m"]),
            "existing_llm_id": nm.get("llm_id"),
            "note": "different elevation in project (Δ={:.3f}m)".format(nm.get("delta_m", 0.0)),
        })
    for mt in reconcile.get("matches") or []:
        actions.append({
            "action": "skip",
            "name": mt["name"],
            "elevation_m": float(mt["elevation_m"]),
            "existing_llm_id": mt.get("llm_id"),
            "note": "already aligned",
        })
    return actions


def _meta_run_phase1_audit(
    kg: ProjectKG,
    directory: str,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Pipeline Phase 1 read-only : check + inspect + markers + assign + reconcile.

    Helper interne réutilisé par `import_project_audit` (pour la première
    exécution exposée au LLM) ET par `import_project_execute` (qui re-run
    Phase 1 cheap après confirmation utilisateur, pour ne pas avoir à
    persister l'état entre les deux calls).

    Pure read-only — ne mute ni le KG, ni Revit, ni le filesystem.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise FileNotFoundError("Directory not found: {}".format(dir_path))

    # 1. Integrity check (gate).
    integrity = check_planset_integrity(
        kg=kg, directory=str(dir_path), scale_override=scale_override,
    )
    gate_status = integrity.get("gate_status")
    if gate_status == "abort":
        return {
            "gate_status": "abort",
            "severity": integrity.get("severity"),
            "integrity_audit": integrity,
            "next_step": "Resolve errors before retrying (cf. integrity_audit.errors).",
        }

    # 2. Classify files (plans / coupes / elevations) via inspect_sections.
    inspection = inspect_sections(
        kg=kg, directory=str(dir_path), scale_override=scale_override,
    )
    classified = _meta_classify_files(dir_path)
    plans = classified["plans"]
    coupes = classified["coupes"]
    elevations = classified["elevations"]

    if not plans:
        raise ValueError(
            "No plan DXF detected in {}. Need at least one plan for section "
            "marker detection.".format(dir_path.name)
        )
    if not coupes:
        raise ValueError(
            "No section DXF detected in {}. Phase 2c (floors) needs coupes "
            "for thickness extraction.".format(dir_path.name)
        )

    # 3. Section markers — try plans until one has section traits.
    markers_payload: Dict[str, Any] = {"markers": [], "section_count": 0}
    plan_for_markers: Optional[Path] = None
    for p in plans:
        try:
            payload = find_section_markers(kg=kg, file_path=str(p))
        except Exception:  # noqa: BLE001
            continue
        section_markers = [m for m in (payload.get("markers") or []) if m.get("kind") == "section"]
        if section_markers:
            markers_payload = payload
            plan_for_markers = p
            break
    section_markers = [m for m in (markers_payload.get("markers") or []) if m.get("kind") == "section"]

    # 4. Pair coupes with markers (best-fit by drift).
    assignment: List[Dict[str, Any]] = []
    if section_markers and len(coupes) == len(section_markers):
        assign_result = assign_coupes_to_traits(
            kg=kg, coupe_paths=[str(c) for c in coupes],
            section_markers=section_markers,
        )
        for entry in assign_result.get("assignment") or []:
            mk = section_markers[entry["marker_index"]]
            view_dir = (
                mk.get("inferred_view_dir")
                or (mk.get("view_dir_candidates") or ["up"])[0]
            )
            assignment.append({
                "coupe_path": entry["coupe_path"],
                "coupe_name": entry.get("coupe_name"),
                "marker_index": entry["marker_index"],
                "plan_p1": mk["p1_m"],
                "plan_p2": mk["p2_m"],
                "view_dir": view_dir,
                "drift_m": entry.get("drift_m"),
                "drift_pct": entry.get("drift_pct"),
                "x_axis_convention": "identity",  # défaut, override ci-dessous.
            })

    # 4bis. Détection X axis convention par coupe (P2 mirror fix).
    # On cross-valide murs plan ↔ section_walls coupe pour identifier
    # les coupes en convention "reversed" (DXF X = -world axis).
    if assignment and plan_for_markers is not None:
        try:
            section_lines_for_detect = [
                {
                    "coupe_path": a["coupe_path"],
                    "plan_p1": a["plan_p1"],
                    "plan_p2": a["plan_p2"],
                    "view_dir": a["view_dir"],
                    "name": a.get("coupe_name"),
                }
                for a in assignment
            ]
            detect_result = detect_section_orientations(
                kg=kg,
                plan_path=str(plan_for_markers),
                section_lines=section_lines_for_detect,
                scale_override=scale_override,
            )
            # Dict coupe_path → verdict complet (avec walls_crossed +
            # confidence pour distinguer signal solide vs ambigu).
            verdict_by_path = {
                o["coupe_path"]: o
                for o in detect_result.get("orientations") or []
            }
            for a in assignment:
                verdict = verdict_by_path.get(a["coupe_path"])
                if verdict is None:
                    a["x_axis_convention"] = None
                    continue
                wall_signal_solid = (
                    verdict.get("walls_crossed", 0) > 0
                    and verdict.get("confidence", 0.0) >= 0.1
                )
                bbox_signal = verdict.get("bbox_signal")
                if wall_signal_solid:
                    # Signal solide via murs : applique la convention.
                    a["x_axis_convention"] = verdict["convention"]
                elif bbox_signal is not None:
                    # Pas de mur croisé mais bbox A-FLOR/A-WALL en coupe
                    # matche plan (cf. P2 Coupe 3 = reversed via slabs).
                    a["x_axis_convention"] = bbox_signal
                else:
                    # Vraiment ambigu : None → no flip, Revit's default.
                    a["x_axis_convention"] = None
        except Exception:  # noqa: BLE001 — détection non-fatale.
            # Si la détection échoue (parse error, etc.), garder None
            # par défaut (= no flip). L'user peut overrider à la main.
            for a in assignment:
                a["x_axis_convention"] = None

    # 5. Level reconciliation (uses first coupe — they declare same levels).
    coupe_levels_reconcile: Dict[str, Any] = {}
    if coupes:
        from .levels import reconcile_with_dxf as _reconcile_levels
        try:
            coupe_levels_reconcile = _reconcile_levels(kg=kg, coupe_path=str(coupes[0]))
        except Exception as exc:  # noqa: BLE001 — let user see what failed
            coupe_levels_reconcile = {
                "ok": False, "error": "{}".format(exc),
                "alignment_complete": False,
            }
    level_actions_proposed = _meta_build_level_actions_proposed(coupe_levels_reconcile)

    # 6. Build the consolidated next_step hint.
    needs_warnings_confirm = gate_status == "needs_user"
    needs_levels_confirm = not coupe_levels_reconcile.get("alignment_complete", False)
    confirm_steps: List[str] = []
    if needs_warnings_confirm:
        confirm_steps.append("ui_confirm_yes_no(integrity warnings)")
    if needs_levels_confirm:
        confirm_steps.append("ui_confirm_choices(level_actions_proposed)")
    if confirm_steps:
        next_step = (
            "Surface to user via {}, then call dwg_import_project_execute() "
            "with level_actions= the confirmed subset and proceed_on_warnings=True."
        ).format(" + ".join(confirm_steps))
    else:
        next_step = (
            "No user confirmation needed — call dwg_import_project_execute() "
            "directly with level_actions=None (auto-apply)."
        )

    return {
        "gate_status": gate_status,
        "severity": integrity.get("severity"),
        "integrity_audit": integrity,
        "files": {
            "plans": [str(p) for p in plans],
            "coupes": [str(c) for c in coupes],
            "elevations": [str(e) for e in elevations],
            "plan_with_markers": str(plan_for_markers) if plan_for_markers else None,
        },
        "inspection_summary": {
            "files_count": len(inspection.get("files", [])),
            "section_to_plan_matches": inspection.get("section_to_plan_matches"),
        },
        "section_assignment": assignment,
        "level_reconciliation": {
            "alignment_complete": coupe_levels_reconcile.get("alignment_complete", False),
            "summary_for_dialog": coupe_levels_reconcile.get("summary_for_dialog"),
            "missing_count": len(coupe_levels_reconcile.get("missing_in_project") or []),
            "matches_count": len(coupe_levels_reconcile.get("matches") or []),
            "elev_only_matches_count": len(coupe_levels_reconcile.get("elev_only_matches") or []),
        },
        "level_actions_proposed": level_actions_proposed,
        "needs_warnings_confirm": needs_warnings_confirm,
        "needs_levels_confirm": needs_levels_confirm,
        "next_step": next_step,
    }


@tool(name="dwg_import_project_audit", tier=3)
def import_project_audit(
    kg: ProjectKG,
    directory: str,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Meta-tool Phase 1 — audit read-only complet d'un dossier DXF projet.

    Enchaîne `check_planset_integrity` + `inspect_sections` +
    `find_section_markers` + `dxf_assign_coupes_to_traits` +
    `levels_reconcile_with_dxf` en **un seul** appel tool. Retourne un
    plan consolidé que le LLM surface à l'utilisateur via `ui_confirm_*`
    avant de lancer `dwg_import_project_execute`.

    **Pure read-only** — ne mute ni le KG, ni Revit, ni le filesystem.
    Idempotent : peut être re-appelé sans effet de bord.

    Pattern attendu côté LLM :
    1. `dwg_import_project_audit(directory)` → plan
    2. Si `gate_status == "abort"` → STOP, présenter `integrity_audit.errors`
    3. Si `needs_warnings_confirm` → `ui_confirm_yes_no(...)` (warnings)
    4. Si `needs_levels_confirm` → `ui_confirm_choices(level_actions_proposed)`
    5. `dwg_import_project_execute(directory, level_actions=..., proceed_on_warnings=True)`

    Concepts: dxf, dwg, import, projet, audit, plan, niveau, coupe,
              élévation, méta, pipeline, phase 1
    Phrases: "audite ce projet DXF", "prépare l'import projet",
             "plan d'import", "qu'est-ce qu'il y a dans ce dossier"
    Similar: dwg_import_project_execute, check_planset_integrity,
             dwg_inspect_sections, levels_reconcile_with_dxf

    Args:
        directory: chemin du dossier contenant les DXFs du projet
            (plan + coupes + élévations). Doit exister.
        scale_override: forçage scale (m/unit) si `$INSUNITS` absent
            dans les DXFs. Appliqué à tous les fichiers.

    Returns:
        {"ok": bool,
         "gate_status": "pass" | "needs_user" | "abort",
         "severity": "clean" | "warnings" | "errors",
         "integrity_audit": {...},
         "files": {"plans", "coupes", "elevations", "plan_with_markers"},
         "section_assignment": [{coupe_path, plan_p1, plan_p2, view_dir, ...}],
         "level_reconciliation": {alignment_complete, summary_for_dialog,
                                   missing_count, matches_count, ...},
         "level_actions_proposed": [{action, name, elevation_m, ...}],
         "needs_warnings_confirm": bool,
         "needs_levels_confirm": bool,
         "next_step": str}
        `gate_status == "abort"` → seul `integrity_audit` est rempli, le
        reste est omis (early-return).
    """
    audit = _meta_run_phase1_audit(kg, directory, scale_override=scale_override)
    return {"ok": True, **audit}


# ----- import_project_execute helpers -----------------------------------


def _meta_apply_level_actions(
    kg: ProjectKG,
    doc: Any,
    level_actions: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Apply confirmed level_actions. V0 supports only `action="create"` ;
    `update` / `skip` are no-ops (informative). Idempotent — if a level with
    the same name already exists in the KG, the create is skipped silently
    via the levels_create_many internal name_collision check.
    """
    creates = [a for a in level_actions if a.get("action") == "create"]
    if not creates:
        return {"levels_created": 0, "levels_create_skipped_existing": 0}

    # Avoid duplicates if the user re-runs execute on the same project.
    existing_names = set()
    for lid in kg.find_by_type("Level"):
        attrs = kg.get_node(lid)
        if attrs.get("deleted_at_turn") is None:
            existing_names.add(attrs.get("name"))
    fresh_items: List[Dict[str, Any]] = []
    skipped_existing = 0
    for a in creates:
        if a["name"] in existing_names:
            skipped_existing += 1
            continue
        fresh_items.append({"name": a["name"], "elevation_m": float(a["elevation_m"])})

    if not fresh_items:
        return {"levels_created": 0, "levels_create_skipped_existing": skipped_existing}

    from .levels import create_many as _levels_create_many
    result = _levels_create_many(kg=kg, doc=doc, items=fresh_items)
    return {
        "levels_created": result.get("count", 0),
        "levels_create_skipped_existing": skipped_existing,
    }


def _meta_register_dxf_context(
    kg: ProjectKG,
    directory: str,
    inspection: Dict[str, Any],
    section_assignment: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Persiste inspection + section lines dans le DxfImportContext KG.
    Pure-Python (pas de Revit ici). Idempotent : register_inspection met à
    jour le node existant si déjà créé."""
    from .dxf_context import register_inspection as _reg_insp
    from .dxf_context import register_section_line_many as _reg_lines

    _reg_insp(kg=kg, directory=directory, inspection=inspection)

    section_lines_count = 0
    if section_assignment:
        specs = []
        for entry in section_assignment:
            specs.append({
                "coupe_path": entry["coupe_path"],
                "plan_p1": entry["plan_p1"],
                "plan_p2": entry["plan_p2"],
                "view_dir": entry["view_dir"],
                "name": Path(entry["coupe_path"]).stem,
                "confirmed_by_user": True,
                "scale_verified": True,
                "drift_pct": entry.get("drift_pct", 0.0),
            })
        _reg_lines(kg=kg, section_lines=specs)
        section_lines_count = len(specs)
    return {"section_lines_registered": section_lines_count}


def _coupe_y_extent_m(
    path: Path,
    scale_override: Optional[float] = None,
    height_buffer_m: float = 0.5,
) -> Optional[Tuple[float, float]]:
    """Lit le Y extent (en mètres post-conversion) du contenu structurel
    d'un DXF coupe : LINEs sur A-WALL, A-FLOR, A-FLOR-LEVL, S-COLS.

    Use case : adapter `bottom_elev_m` / `top_elev_m` de la ViewSection
    Revit à la hauteur effective du DXF coupe (= inclure fondations si
    Y < 0, acrotères/toiture si Y > top_level). Évite que des éléments
    DXF importés tombent hors du frustum de la ViewSection.

    Returns:
        `(y_min - buffer, y_max + buffer)` en mètres, ou None si le
        fichier ne contient aucune LINE pertinente.
    """
    try:
        entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    except Exception:  # noqa: BLE001
        return None
    ys: List[float] = []
    for e in entities:
        if e.kind != "LINE":
            continue
        if e.layer not in (
            "A-WALL", "A-FLOR", "A-FLOR-LEVL", "S-COLS", "S-STRS",
        ):
            continue
        for pt in e.coords:
            ys.append(pt[1])
    if not ys:
        return None
    return (round(min(ys) - height_buffer_m, 3),
            round(max(ys) + height_buffer_m, 3))


def _meta_create_section_views(
    kg: ProjectKG,
    doc: Any,
    section_assignment: List[Dict[str, Any]],
    top_elev_m: float = 6.0,
    scale_override: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Crée les vues Section Revit pour chaque coupe assignée.
    Retourne la liste des sections créées avec leur revit_id, indexée par
    coupe_path pour le link_cad subséquent. doc=None → no-op (test path).

    Pour chaque coupe : lit le Y extent du DXF (`_coupe_y_extent_m`) et
    fixe `bottom_elev_m` / `top_elev_m` per-item pour que le bbox de la
    ViewSection englobe exactement le contenu du DXF (fondations sous
    Z=0 + toiture + acrotères inclus). Fallback au top-level `top_elev_m`
    si la lecture DXF échoue.
    """
    if doc is None or not section_assignment:
        return []
    from .views import create_section_many as _create_sections
    items = []
    for entry in section_assignment:
        coupe_path = Path(entry["coupe_path"])
        y_extent = _coupe_y_extent_m(coupe_path, scale_override=scale_override)
        item = {
            "name": "Coupe " + coupe_path.stem.split(" - ")[-1],
            "p1_m": entry["plan_p1"],
            "p2_m": entry["plan_p2"],
            "view_dir": entry["view_dir"],
            "x_axis_convention": entry.get("x_axis_convention"),  # None = pas de flip
        }
        if y_extent is not None:
            item["bottom_elev_m"] = y_extent[0]
            item["top_elev_m"] = y_extent[1]
        items.append(item)
    result = _create_sections(
        kg=kg, doc=doc, items=items, top_elev_m=top_elev_m,
    )
    # Re-zip with coupe_path for downstream link_cad mapping.
    out: List[Dict[str, Any]] = []
    for entry, section in zip(section_assignment, result.get("sections", [])):
        out.append({
            "coupe_path": entry["coupe_path"],
            "section_name": section.get("name"),
            "section_revit_id": section.get("revit_id"),
            # Diagnostic mirror (P2 fix) : propagate basis_x verdict.
            "x_axis_convention": entry.get("x_axis_convention"),
            "intended_basis_x": section.get("intended_basis_x"),
            "actual_right_direction": section.get("actual_right_direction"),
            "basis_x_match": section.get("basis_x_match"),
        })
    return out


def _meta_simulate_linked_views_offline(
    kg: ProjectKG,
    plans: List[str],
    coupes_with_sections: List[Dict[str, Any]],
    elevations: List[str],
) -> int:
    """Mode test : pas de Revit, on injecte des linked_views synthétiques
    dans le DxfImportContext pour que les collectors aval
    (`_collect_plan_openings_world`, fusion élévation, etc.) trouvent les
    plans/coupes/élévations attendus.

    Les revit_ids synthétiques (1_000_000+) n'ont aucun sens hors-test ;
    seuls `file_path` / `view_kind` / `view_name` sont consommés en aval
    (cf. `_plan_path_to_level_elev`, `_load_elevations_from_kg`).
    """
    from .dxf_context import register_linked_view_many as _reg_linked
    entries: List[Dict[str, Any]] = []
    rid = 1_000_000
    for plan_path in plans:
        stem = Path(plan_path).stem
        view_name = stem.split(" - ")[-1] if " - " in stem else stem
        entries.append({
            "file_path": plan_path, "link_revit_id": rid,
            "view_revit_id": rid + 1, "view_kind": "plan",
            "view_name": view_name,
        })
        rid += 2
    for c in coupes_with_sections:
        stem = Path(c["coupe_path"]).stem
        view_name = stem.split(" - ")[-1] if " - " in stem else stem
        entries.append({
            "file_path": c["coupe_path"], "link_revit_id": rid,
            "view_revit_id": rid + 1, "view_kind": "section",
            "view_name": view_name,
        })
        rid += 2
    for e in elevations:
        stem = Path(e).stem
        view_name = stem.split(" - ")[-1] if " - " in stem else stem
        entries.append({
            "file_path": e, "link_revit_id": rid,
            "view_revit_id": rid + 1, "view_kind": "elevation",
            "view_name": view_name,
        })
        rid += 2
    if entries:
        _reg_linked(kg=kg, entries=entries)
    return len(entries)


def _meta_link_cad_for_all_dxfs(
    kg: ProjectKG,
    doc: Any,
    plans: List[str],
    coupes_with_sections: List[Dict[str, Any]],
    elevations: List[str],
) -> Dict[str, Any]:
    """Linke tous les DXFs dans leurs vues respectives + enregistre le
    mapping dans le KG.

    Linking targets :
    - chaque plan DXF → la FloorPlan view du Level matchant (par nom)
    - chaque coupe DXF → la SectionView fraîchement créée
    - chaque élévation DXF → la vue Élévation Revit matchant la direction
      (par nom de fichier : « Élévation Est » → vue « Est », etc.)

    doc=None → mode test : on enregistre des linked_views synthétiques
    pour que les collectors aval voient les fichiers (sinon la fusion
    élévation ne tourne pas → résultats divergent du runtime Revit).
    """
    if doc is None:
        n = _meta_simulate_linked_views_offline(
            kg, plans, coupes_with_sections, elevations,
        )
        return {"linked_views_count": n, "skipped_unmatched": 0, "offline_simulated": True}

    from .catalog import list_levels as _list_levels
    from .catalog import list_elevation_views as _list_elev_views
    from .views import link_cad_many as _link_cad_many
    from .dxf_context import register_linked_view_many as _reg_linked

    links: List[Dict[str, Any]] = []
    linked_view_specs: List[Dict[str, Any]] = []
    skipped = 0

    # Plans → FloorPlan views.
    levels_payload = _list_levels(kg=kg, doc=doc)
    floor_plan_by_level_name: Dict[str, int] = {}
    for lvl in levels_payload.get("levels", []):
        fpv = lvl.get("floor_plan_view_revit_id")
        if fpv is not None:
            floor_plan_by_level_name[lvl.get("name")] = int(fpv)
    for plan_path in plans:
        # Convention : « Projet8-Plan d'étage - Niveau 0.dxf » → « Niveau 0 ».
        stem = Path(plan_path).stem
        suffix = stem.split(" - ")[-1] if " - " in stem else stem
        view_revit_id = floor_plan_by_level_name.get(suffix)
        if view_revit_id is None:
            skipped += 1
            continue
        links.append({"file_path": plan_path, "view_revit_id": view_revit_id})
        linked_view_specs.append({
            "file_path": plan_path,
            "view_revit_id": view_revit_id,
            # link_revit_id is filled after link_cad_many returns.
            "view_kind": "plan",
            "view_name": suffix,
        })

    # Coupes → SectionViews (just created).
    # Si basis_x_match=False (Revit a ignoré notre BasisX flippé),
    # demander à link_cad de mirror l'instance post-placement pour
    # compenser. Cf. fix bug mirror P2 longitudinales 2026-05-14.
    for coupe in coupes_with_sections:
        view_revit_id = coupe.get("section_revit_id")
        if view_revit_id is None:
            skipped += 1
            continue
        need_mirror = (
            coupe.get("x_axis_convention") is not None
            and coupe.get("basis_x_match") is False
        )
        links.append({
            "file_path": coupe["coupe_path"],
            "view_revit_id": int(view_revit_id),
            "mirror_post_link": need_mirror,
        })
        linked_view_specs.append({
            "file_path": coupe["coupe_path"],
            "view_revit_id": int(view_revit_id),
            "view_kind": "section",
            "view_name": coupe.get("section_name"),
        })

    # Elevations → Revit elevation views (by direction match).
    elev_views_payload = _list_elev_views(kg=kg, doc=doc)
    view_by_direction: Dict[str, int] = {}
    for v in elev_views_payload.get("elevation_views", []):
        d = v.get("direction")
        if d:
            view_by_direction[d] = int(v.get("revit_id"))
    for elev_path in elevations:
        stem = Path(elev_path).stem
        suffix = stem.split(" - ")[-1] if " - " in stem else stem
        # Direction = last token of suffix : « Élévation Est » → « Est ».
        direction_word = suffix.split()[-1].strip()
        # Normalize French → match dictionary keys.
        direction = None
        for cardinal in ("Est", "Ouest", "Nord", "Sud"):
            if cardinal.lower() == direction_word.lower():
                direction = cardinal
                break
        if direction is None or direction not in view_by_direction:
            skipped += 1
            continue
        view_revit_id = view_by_direction[direction]
        links.append({"file_path": elev_path, "view_revit_id": view_revit_id})
        linked_view_specs.append({
            "file_path": elev_path,
            "view_revit_id": view_revit_id,
            "view_kind": "elevation",
            "view_name": "Élévation " + direction,
        })

    if not links:
        return {"linked_views_count": 0, "skipped_unmatched": skipped}

    link_result = _link_cad_many(kg=kg, doc=doc, links=links)

    # Enrich linked_view_specs with link_revit_id from result, then register
    # the mapping in the DxfImportContext KG.
    link_by_file = {l["file"]: l for l in link_result.get("links", [])}
    for spec in linked_view_specs:
        live = link_by_file.get(spec["file_path"])
        if live is not None:
            spec["link_revit_id"] = int(live.get("link_revit_id") or 0)
    # Strip any spec missing link_revit_id (failed link) — register only success.
    valid_specs = [s for s in linked_view_specs if s.get("link_revit_id")]
    if valid_specs:
        _reg_linked(kg=kg, entries=valid_specs)

    return {
        "linked_views_count": len(valid_specs),
        "skipped_unmatched": skipped,
    }


def _meta_phase2a_walls(
    kg: ProjectKG,
    doc: Any,
    audit: Dict[str, Any],
    height_per_level_m: float,
) -> Dict[str, Any]:
    """Phase 2a — extract_thicknesses (info) + create_continuous_walls_many."""
    plans = audit.get("files", {}).get("plans") or []
    if not plans:
        return {"walls_imported_total": 0, "note": "no plans to import"}

    # Find level_ref per plan via name match (same convention as link_cad).
    plan_items: List[Dict[str, Any]] = []
    plan_view_names = {}
    for plan_path in plans:
        stem = Path(plan_path).stem
        suffix = stem.split(" - ")[-1] if " - " in stem else stem
        plan_view_names[plan_path] = suffix

    level_by_name: Dict[str, str] = {}
    levels_sorted: List[Tuple[float, str]] = []
    for lid in kg.find_by_type("Level"):
        attrs = kg.get_node(lid)
        if attrs.get("deleted_at_turn") is not None:
            continue
        level_by_name[attrs.get("name")] = lid
        levels_sorted.append((float(attrs.get("elevation", 0.0)), lid))
    levels_sorted.sort()

    if not levels_sorted:
        return {"walls_imported_total": 0, "note": "no levels in KG — cannot import walls"}

    for plan_path in plans:
        level_ref = level_by_name.get(plan_view_names[plan_path])
        if level_ref is None:
            # Fallback : bottom-most level.
            level_ref = levels_sorted[0][1]
        plan_items.append({
            "file_path": plan_path,
            "level_ref": level_ref,
            "height_m": height_per_level_m,
        })

    # Info pass : thickness distribution (logged in summary but not gating).
    thickness_info = extract_wall_thicknesses_many(
        kg=kg, file_paths=plans,
    )

    walls_result = create_continuous_walls_many(
        kg=kg, doc=doc, items=plan_items,
    )
    return {
        "walls_imported_total": walls_result.get("walls_imported_total", 0),
        "walls_per_file": walls_result.get("walls_per_file"),
        "fusion_events": walls_result.get("fusion_events"),
        "walls_suspect_low_3d_consensus": walls_result.get("walls_suspect_low_3d_consensus"),
        "types_created": walls_result.get("types_created"),
        "types_reused": walls_result.get("types_reused"),
        "elevations_loaded": walls_result.get("elevations_loaded"),
        "thickness_distribution_global": thickness_info.get("global_distribution"),
    }


def _meta_phase2b_openings(kg: ProjectKG, doc: Any) -> Dict[str, Any]:
    result = add_openings_to_walls_many(kg=kg, doc=doc)
    return {
        "plan_openings_detected": result.get("plan_openings_detected"),
        "openings_doors_created": result.get("openings_doors_created"),
        "openings_windows_created": result.get("openings_windows_created"),
        "openings_orphan": result.get("openings_orphan"),
        "openings_oversize_for_wall": result.get("openings_oversize_for_wall"),
        "opening_types_created": result.get("opening_types_created"),
    }


def _meta_phase2c_floors(
    kg: ProjectKG,
    doc: Any,
    skip_top_floor: bool,
) -> Dict[str, Any]:
    result = create_floors_many(kg=kg, doc=doc, skip_top_level=skip_top_floor)
    return {
        "floors_created_count": result.get("floors_created_count"),
        "floors_per_level": result.get("floors_per_level"),
        "types_created": result.get("types_created"),
        "types_reused": result.get("types_reused"),
        # Phase 2c V2 : trous détectés (cages d'escalier, patios, atria).
        # Vide si aucun trou (cas P7).
        "holes_count_by_kind": result.get("holes_count_by_kind") or {},
        # Phase 2c V2 : méthode utilisée pour tracer le contour outer de
        # chaque sol (face_tracing OK = plan en L bien tracé, convex_hull_fallback
        # = murs fragmentés, hull surestime probablement).
        "boundary_methods": result.get("boundary_methods") or {},
    }


def _meta_phase2d_columns(
    kg: ProjectKG,
    doc: Any,
    audit: Dict[str, Any],
) -> Dict[str, Any]:
    """Phase 2d — `dwg_create_columns_many` sur tous les plans assignés
    aux niveaux. Réutilise le même mapping plan → level que Phase 2a."""
    plans = audit.get("files", {}).get("plans") or []
    if not plans:
        return {
            "columns_created_count": 0,
            "note": "no plans to import columns from",
        }

    # Build plan_items via le même mapping nom-de-fichier → Level que 2a.
    plan_view_names: Dict[str, str] = {}
    for plan_path in plans:
        stem = Path(plan_path).stem
        suffix = stem.split(" - ")[-1] if " - " in stem else stem
        plan_view_names[plan_path] = suffix

    level_by_name: Dict[str, str] = {}
    levels_sorted: List[Tuple[float, str]] = []
    for lid in kg.find_by_type("Level"):
        attrs = kg.get_node(lid)
        if attrs.get("deleted_at_turn") is not None:
            continue
        level_by_name[attrs.get("name")] = lid
        levels_sorted.append((float(attrs.get("elevation", 0.0)), lid))
    levels_sorted.sort()
    if not levels_sorted:
        return {
            "columns_created_count": 0,
            "note": "no levels in KG — cannot import columns",
        }

    plan_items: List[Dict[str, Any]] = []
    for plan_path in plans:
        level_ref = level_by_name.get(plan_view_names[plan_path])
        if level_ref is None:
            level_ref = levels_sorted[0][1]
        plan_items.append({
            "file_path": plan_path,
            "level_ref": level_ref,
        })

    try:
        result = create_columns_many(kg=kg, doc=doc, items=plan_items)
    except ValueError as exc:
        # Cas typique : aucune famille de poteau chargée dans le projet
        # → non-fatal (le projet peut ne pas avoir de poteaux).
        return {
            "columns_created_count": 0,
            "skipped_reason": str(exc),
            "note": "Phase 2d skippée (cf. skipped_reason).",
        }
    return {
        "columns_created_count": result.get("columns_created_count", 0),
        "candidates_total": result.get("candidates_total", 0),
        "aggregated_count": result.get("aggregated_count", 0),
        "columns_per_level": result.get("columns_per_level"),
        "types_created": result.get("types_created"),
        "types_reused": result.get("types_reused"),
        "note": result.get("note"),
    }


def _meta_open_3d_view(kg: ProjectKG, doc: Any) -> bool:
    if doc is None:
        return False
    try:
        from .views import open_3d as _open_3d
        _open_3d(kg=kg, doc=doc)
        return True
    except Exception:  # noqa: BLE001 — non-fatal UX-only step.
        return False


@tool(name="dwg_import_project_execute", tier=3)
def import_project_execute(
    kg: ProjectKG,
    doc: Any,
    directory: str,
    level_actions: Optional[List[Dict[str, Any]]] = None,
    proceed_on_warnings: bool = False,
    height_per_level_m: float = 3.0,
    skip_top_floor: bool = True,
    open_3d_view: bool = True,
    scale_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Meta-tool Phase 1.5 + Phase 2 — exécute l'import BIM complet.

    Suit `dwg_import_project_audit`. Re-run Phase 1 read-only (deterministe,
    bon marché) en interne pour éviter de passer l'état Phase 1 par le LLM,
    puis applique `level_actions` confirmés, registre le DxfImportContext,
    crée les vues Section, linke tous les DXFs (plans/coupes/élévations),
    crée murs/openings/sols, et optionnellement ouvre la vue 3D.

    Pipeline interne (un helper privé `_meta_phase*_*` par étape — ajouter
    une micro-step = ajouter un helper + 1 ligne ci-dessous) :

    1. Re-run Phase 1 audit (gate check : abort sur errors, refuse sur
       warnings si `proceed_on_warnings=False`).
    2. `_meta_apply_level_actions` : levels_create_many des actions confirmées.
    3. `_meta_register_dxf_context` : register_inspection +
       register_section_line_many.
    4. `_meta_create_section_views` : views_create_section_many.
    5. `_meta_link_cad_for_all_dxfs` : link tous les DXFs +
       register_linked_view_many.
    6. `_meta_phase2a_walls` : extract_wall_thicknesses_many +
       dwg_create_continuous_walls_many.
    7. `_meta_phase2b_openings` : dwg_add_openings_to_walls_many.
    8. `_meta_phase2c_floors` : dwg_create_floors_many (skip top level).
    9. `_meta_open_3d_view` (optionnel).

    Token saving : ~13 sequential LLM calls → ~1 meta call. Estimé ~70%
    de réduction sur ce use case (cf. JOURNAL session w + benchmark).

    Concepts: dxf, dwg, import, projet, execute, BIM, méta, pipeline,
              phase 1, phase 2, walls, openings, floors, batch
    Phrases: "importe le projet", "lance l'import complet",
             "exécute le pipeline d'import", "crée les murs/openings/sols depuis DXF"
    Similar: dwg_import_project_audit, dwg_create_continuous_walls_many,
             dwg_add_openings_to_walls_many, dwg_create_floors_many

    Args:
        directory: dossier des DXFs (idem que pour _audit).
        level_actions: liste retournée par _audit puis filtrée par
            confirmation user. Si None, auto-derive de la reconciliation
            interne (crée tous les niveaux missing). Si [], no-op (user
            a explicitement refusé toute action niveau).
        proceed_on_warnings: si False (défaut), refuse l'exécution quand
            le gate est `needs_user` (warnings non confirmés). Si True,
            outrepasse (user a déjà confirmé via ui_confirm_yes_no).
        height_per_level_m: hauteur par défaut des murs (défaut 3.0m).
        skip_top_floor: skip la création de sol au niveau le plus haut
            (= toiture, défaut True).
        open_3d_view: ouvre {3D} à la fin pour validation visuelle.
        scale_override: idem que pour _audit.

    Returns:
        Summary consolidé :
        {"ok", "phase1_setup": {levels_created, sections_created,
          linked_views, section_lines_registered},
         "phase2a": {...}, "phase2b": {...}, "phase2c": {...},
         "view_3d_opened": bool,
         "note": str}
    """
    # 1. Re-run Phase 1 audit (deterministic, cheap).
    audit = _meta_run_phase1_audit(kg, directory, scale_override=scale_override)
    if audit.get("gate_status") == "abort":
        return {
            "ok": False,
            "reason": "integrity_audit aborted — errors in DXF planset",
            "integrity_audit": audit.get("integrity_audit"),
        }
    if audit.get("gate_status") == "needs_user" and not proceed_on_warnings:
        return {
            "ok": False,
            "reason": (
                "integrity_audit has warnings — user must confirm via "
                "ui_confirm_yes_no, then re-call with proceed_on_warnings=True."
            ),
            "integrity_audit": audit.get("integrity_audit"),
        }

    # 2. Apply level actions (default : auto-derive from audit's missing).
    if level_actions is None:
        level_actions = audit.get("level_actions_proposed") or []
    levels_summary = _meta_apply_level_actions(kg, doc, level_actions)

    # 3. Register DxfImportContext (inspection + section_lines).
    inspection_for_register = inspect_sections(
        kg=kg, directory=directory, scale_override=scale_override,
    )
    ctx_summary = _meta_register_dxf_context(
        kg, directory,
        inspection=inspection_for_register,
        section_assignment=audit.get("section_assignment") or [],
    )

    # 4. Create section views in Revit. Per-coupe Y extent depuis le
    # DXF (= fondations + toiture inclues) ; fallback top-level si lecture
    # DXF échoue.
    coupes_with_sections = _meta_create_section_views(
        kg, doc, audit.get("section_assignment") or [],
        top_elev_m=height_per_level_m * 2,
        scale_override=scale_override,
    )

    # 5. Link all DXFs in their respective views + register the mapping.
    # Offline (doc=None) : `coupes_with_sections` est vide ; le simulateur
    # offline doit voir les coupes brutes pour les enregistrer comme
    # linked_views synthétiques. On les construit depuis section_assignment.
    if doc is None and not coupes_with_sections:
        coupes_with_sections = [
            {"coupe_path": entry["coupe_path"], "section_name": None,
             "section_revit_id": None}
            for entry in audit.get("section_assignment") or []
        ]
    link_summary = _meta_link_cad_for_all_dxfs(
        kg, doc,
        plans=audit.get("files", {}).get("plans") or [],
        coupes_with_sections=coupes_with_sections,
        elevations=audit.get("files", {}).get("elevations") or [],
    )

    # 6-9. Phase 2.
    phase2a = _meta_phase2a_walls(kg, doc, audit, height_per_level_m)
    phase2b = _meta_phase2b_openings(kg, doc)
    phase2c = _meta_phase2c_floors(kg, doc, skip_top_floor)
    phase2d = _meta_phase2d_columns(kg, doc, audit)

    # 10. Open 3D view (optional).
    view_3d_opened = _meta_open_3d_view(kg, doc) if open_3d_view else False

    note = (
        "Import OK — {} murs / {} fenêtres / {} portes / {} sols / "
        "{} poteaux créés sur {} niveau(x). {}".format(
            phase2a.get("walls_imported_total", 0),
            phase2b.get("openings_windows_created", 0),
            phase2b.get("openings_doors_created", 0),
            phase2c.get("floors_created_count", 0),
            phase2d.get("columns_created_count", 0),
            levels_summary.get("levels_created", 0)
            + levels_summary.get("levels_create_skipped_existing", 0),
            "Vue 3D ouverte." if view_3d_opened else "",
        )
    )

    # Diagnostic : orientations détectées (P2 mirror fix) + verdict
    # basis_x match (= est-ce que Revit a accepté notre BasisX flippé,
    # ou re-dérivé sa propre version ?).
    section_orientations_diag = []
    coupes_diag_by_path = {
        c.get("coupe_path"): c for c in coupes_with_sections
    }
    for entry in audit.get("section_assignment") or []:
        cp = entry.get("coupe_path")
        cs = coupes_diag_by_path.get(cp) or {}
        section_orientations_diag.append({
            "coupe_path": cp,
            "name": Path(cp).stem.split(" - ")[-1] if cp else None,
            "view_dir": entry.get("view_dir"),
            "x_axis_convention": entry.get("x_axis_convention"),
            "intended_basis_x": cs.get("intended_basis_x"),
            "actual_right_direction": cs.get("actual_right_direction"),
            "basis_x_match": cs.get("basis_x_match"),
        })

    return {
        "ok": True,
        "phase1_setup": {
            **levels_summary,
            "sections_created": len(coupes_with_sections),
            "section_orientations": section_orientations_diag,
            **ctx_summary,
            **link_summary,
        },
        "phase2a_walls": phase2a,
        "phase2b_openings": phase2b,
        "phase2c_floors": phase2c,
        "phase2d_columns": phase2d,
        "view_3d_opened": view_3d_opened,
        "note": note,
    }
