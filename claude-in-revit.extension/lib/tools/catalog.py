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
