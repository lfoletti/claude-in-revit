"""aggregations.py — comptage, sommes, regroupements pour le métré."""
from __future__ import annotations

from typing import Any, Dict, List

from ..llm_protocol import tool
from ..project_kg import ProjectKG


@tool(name="aggregations_count", tier=1)
def count(kg: ProjectKG, node_type: str) -> Dict[str, Any]:
    """Compte les éléments d'un type donné (hors éléments soft-deleted).

    Concepts: métré, quantité, comptage, nombre
    Phrases: "combien de murs", "nombre de portes", "compte les niveaux",
             "how many walls"
    Similar: aggregations_sum_area, aggregations_group_by

    Args:
        node_type: type d'élément à compter (Wall, Door, Window, Room, Level,
            WallType, FamilyType).

    Returns:
        {"node_type": str, "count": int, "ids": [llm_id, ...]}
    """
    ids: List[str] = kg.find_by_type(node_type)
    return {"node_type": node_type, "count": len(ids), "ids": ids}
