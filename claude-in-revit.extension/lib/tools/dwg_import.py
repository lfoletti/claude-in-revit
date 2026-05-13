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

from .. import dwg_classifier, dwg_coherence, dwg_reader, dwg_section_reader
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

    # Coupe A-WALL X extent (in metres post-conversion).
    xs: List[float] = []
    for e in coupe_entities:
        if e.layer != "A-WALL" or e.kind != "LINE":
            continue
        for pt in e.coords:
            xs.append(pt[0])
    if not xs:
        # Fallback : use any LINE on any wall-like layer.
        for e in coupe_entities:
            if e.kind == "LINE":
                for pt in e.coords:
                    xs.append(pt[0])
    if not xs:
        return {
            "ok": False,
            "scale_match": False,
            "warning": "Coupe contient aucune LINE — extent géométrique non calculable.",
            "section_line_length_m": float(section_line_length_m),
            "coupe_a_wall_extent_m": 0.0,
            "drift_pct": float("inf"),
            "drift_m": 0.0,
            "units_consistent": units_consistent,
            "plan_units_factor": plan_units_factor,
            "coupe_units_factor": coupe_units_factor,
        }

    coupe_extent_m = max(xs) - min(xs)
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


def _coupe_a_wall_extent_m(path: Path) -> Optional[float]:
    """Helper : retourne l'étendue X des LINEs A-WALL d'un DXF coupe,
    en mètres. None si le fichier n'a pas de A-WALL.
    """
    entities, _ = dwg_reader.parse(path)
    xs: List[float] = []
    for e in entities:
        if e.layer != "A-WALL" or e.kind != "LINE":
            continue
        for pt in e.coords:
            xs.append(pt[0])
    if not xs:
        return None
    return max(xs) - min(xs)


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
        ext = _coupe_a_wall_extent_m(path)
        if ext is None:
            raise ValueError("Coupe {} has no A-WALL LINEs".format(path.name))
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
                xs = [
                    pt[0] for e in ents
                    if e.layer == "A-WALL" and e.kind == "LINE"
                    for pt in e.coords
                ]
                ext = (max(xs) - min(xs)) if xs else 0.0
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
        xs = [
            pt[0] for e in ents
            if e.layer == "A-WALL" and e.kind == "LINE"
            for pt in e.coords
        ]
        extent = (max(xs) - min(xs)) if xs else 0.0
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
        walls_check, openings_check,
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
