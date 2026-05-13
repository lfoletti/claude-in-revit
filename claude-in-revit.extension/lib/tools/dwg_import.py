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

from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import dwg_classifier, dwg_reader, dwg_section_reader
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

    return {
        "ok": True,
        "file": str(path),
        "units_code": meta["units_code"],
        "units_factor_to_m": meta["units_factor_to_m"],
        "total_entities": meta["total_entities"],
        "dxf_version": meta["dxf_version"],
        "source_format": meta["source_format"],
        "layers": meta["layers"],
    }


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
    file_paths: List[str],
    scale_override: Optional[float] = None,
    opening_preview_limit: int = 30,
) -> Dict[str, Any]:
    """Inspecte un plan + N coupes DXF. Read-only, sort un rapport JSON.

    Pipeline du chapitre coupes (UC1 Phase 4, voir JOURNAL 2026-05-12 note
    d'intention + entrée 2026-05-13 inventaire Projet4) :

    1. Parse chaque fichier.
    2. `classify_dxf` → `plan` | `section` | `unknown`.
    3. Pour les sections : extrait niveaux (layer `A-FLOR-LEVL`) +
       ouvertures (INSERTs sur `A-GLAZ`).
    4. Pour le plan : extrait les ouvertures (INSERTs sur `A-GLAZ`).
    5. Pour chaque section, calcule le matching ouvertures coupe↔plan
       via `block_id` partagé (l'ID Revit inscrit dans le nom de bloc,
       partagé entre `... -255828-Niveau 0` et `... -255828-Coupe 1`).

    Le caller (LLM ou user) utilise ensuite ce rapport pour décider :
    - lesquelles des coupes utiliser pour quel pan du plan (géo-ref via
      pointage user, à venir) ;
    - quels niveaux créer (via `levels_create_many`) ;
    - quelles hauteurs sill/head appliquer aux ouvertures du plan.

    Aucun écrit Revit ou KG ici. Le tool est sûr à appeler plusieurs fois.

    Concepts: dwg, dxf, coupe, section, niveau, level, elevation,
              fenêtre, opening, glazing, plan, géo-ref, inspect, audit
    Phrases: "inspecte les coupes", "qu'y a-t-il dans ces coupes",
             "extrait les niveaux", "match les fenêtres entre plan et coupe",
             "préview des coupes du projet", "analyse plan + coupes DXF"
    Similar: dwg_inspect, dwg_classify, levels_create_many

    Args:
        file_paths: liste de chemins de fichiers .dxf (plan + coupes).
            Au moins 1 plan + 1 coupe recommandés pour le matching.
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
    if not file_paths:
        raise ValueError("file_paths must contain at least one DXF path.")

    parsed: List[Dict[str, Any]] = []
    plan_index: Optional[int] = None

    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError("File not found: {}".format(path))

        entities, meta = dwg_reader.parse(path, scale_override=scale_override)
        kind, evidence = dwg_section_reader.classify_dxf(meta["layers"])

        file_record: Dict[str, Any] = {
            "path": str(path),
            "name": path.name,
            "kind": kind,
            "kind_evidence": evidence,
            "units_factor_to_m": meta["units_factor_to_m"],
            "total_entities": meta["total_entities"],
        }

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
        plan_openings = parsed[plan_index].pop("_openings_internal")
        for i, rec in enumerate(parsed):
            if rec.get("kind") != "section":
                continue
            sec_openings = rec.pop("_openings_internal")
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
    else:
        # No plan found — drop any internal data we may have stored.
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
