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

from .. import dwg_classifier, dwg_reader
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
) -> Dict[str, Any]:
    """Applique un mapping layer → rôle + détecte les paires de lignes
    parallèles → wall candidates. Read-only — ne crée rien dans Revit / KG.

    Le LLM appelle ce tool *après* `dwg_inspect` pour valider la qualité
    de la détection avant `dwg_import_walls`. Permet d'ajuster
    `layer_mapping` ou les seuils d'épaisseur sans engager de mutation.

    Concepts: dwg, dxf, classification, murs, paires, layer mapping
    Phrases: "preview les murs détectés", "classifie ce DXF",
             "combien de murs trouve-t-on", "essaie d'abord sans créer"
    Similar: dwg_inspect, dwg_import_walls

    Args:
        file_path: chemin du fichier .dxf ou .dwg.
        layer_mapping: `{layer_name: "wall" | "door" | "window" |
            "ignore" | "text"}`. Layers absents ignorés. Seul "wall"
            est traité en V0 phase 1.
        scale_override: voir `dwg_inspect`.
        min_thickness_m: distance perpendiculaire min pour une paire
            (défaut 0.05 m).
        max_thickness_m: distance max (défaut 0.50 m).

    Returns:
        {"ok": bool, "walls_count": int,
         "walls": [{"p1", "p2", "thickness_m", "layer", "confidence"}, …],
         "rejected_count": int,
         "rejected_summary": [{"layer", "count", "sample_reason"}, …]}
        `walls` enuméré sous `preview_limit=20` ; au-delà tronqué avec
        first/last pour rester compact.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))

    entities, _ = dwg_reader.parse(path, scale_override=scale_override)
    result = dwg_classifier.classify(
        entities, layer_mapping,
        min_thickness_m=min_thickness_m,
        max_thickness_m=max_thickness_m,
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
        "rejected_count": len(result.rejected),
        "rejected_summary": list(rejected_by_layer.values()),
    }
    preview_limit = 20
    if len(walls_dicts) <= preview_limit:
        out["walls"] = walls_dicts
    else:
        out["walls"] = walls_dicts[:preview_limit]
        out["walls_truncated"] = True
        out["note"] = (
            "Preview limited to {} walls of {}. Apply via dwg_import_walls "
            "to commit all.".format(preview_limit, len(walls_dicts))
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
