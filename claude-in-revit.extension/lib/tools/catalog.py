"""catalog.py — read-only inventory of the project (levels, wall types, families).

In production these tools query Revit (`Document.GetElements...`). In the slice
they read the KG directly — the CLI bootstraps a couple of Levels and a
WallType so the LLM has something to reference.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..llm_protocol import tool
from ..project_kg import ProjectKG


@tool(name="catalog_list_levels", tier=1)
def list_levels(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les niveaux du projet avec leur llm_id, nom et altitude (m).

    Concepts: niveau, level, étage, plan d'étage, inventaire
    Phrases: "quels niveaux", "liste les niveaux", "list levels"
    Similar: catalog_list_wall_types

    Args:
        (aucun)

    Returns:
        {"levels": [{llm_id, name, elevation}, ...]}
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Level"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "name": attrs.get("name"),
            "elevation": attrs.get("elevation"),
        })
    return {"levels": out}


@tool(name="catalog_list_wall_types", tier=1)
def list_wall_types(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les types de murs disponibles avec llm_id, nom et épaisseur (m).

    Concepts: type de mur, wall type, catalogue, inventaire
    Phrases: "quels types de mur", "list wall types", "catalogue murs"
    Similar: catalog_list_levels

    Args:
        (aucun)

    Returns:
        {"wall_types": [{llm_id, name, total_thickness}, ...]}
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("WallType"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "name": attrs.get("name"),
            "total_thickness": attrs.get("total_thickness"),
        })
    return {"wall_types": out}


@tool(name="catalog_list_walls", tier=1)
def list_walls(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les murs vivants du projet avec leur géométrie complète.

    Concepts: mur, inventaire, géométrie, p1, p2, longueur, hauteur, walls
    Phrases: "liste les murs", "quels murs", "all walls", "tous les murs",
             "donne-moi la géométrie des murs"
    Similar: catalog_list_levels, catalog_list_wall_types, catalog_list_lines

    Args:
        (aucun)

    Returns:
        {"walls": [{llm_id, level_ref, type_ref, p1, p2, length, height}, ...]}
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Wall"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "level_ref": attrs.get("level_ref"),
            "type_ref": attrs.get("type_ref"),
            "p1": attrs.get("p1"),
            "p2": attrs.get("p2"),
            "length": attrs.get("length"),
            "height": attrs.get("height"),
        })
    return {"walls": out}


@tool(name="catalog_list_lines", tier=1)
def list_lines(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste toutes les lignes du projet (modèle 3D + détail view-bound).

    Concepts: ligne, line, modèle, détail, esquisse, ancre, géométrie
    Phrases: "liste les lignes", "quelles lignes", "all lines", "donne-moi
             les coordonnées des lignes", "lines for walls anchor"
    Similar: catalog_list_walls, walls_create

    Args:
        (aucun)

    Returns:
        {"lines": [{llm_id, kind, p1, p2, length}, ...]}
        - `kind` vaut `"ModelLine"` (3D, indépendant de la vue) ou
          `"DetailLine"` (2D, vue-bound — z toujours nul en pratique).
        - p1/p2 sont en `[x, y, z]` mètres. Pour tracer des murs sur
          une ligne, prends `[x, y]` (les murs sont 2D dans le plan
          du niveau, z est ignoré).
    """
    out: List[Dict[str, Any]] = []
    for kind in ("ModelLine", "DetailLine"):
        for nid in kg.find_by_type(kind):
            attrs = kg.get_node(nid)
            out.append({
                "llm_id": nid,
                "kind": kind,
                "p1": attrs.get("p1"),
                "p2": attrs.get("p2"),
                "length": attrs.get("length"),
            })
    return {"lines": out}
