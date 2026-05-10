"""query.py — KG lookup helpers exposed to the LLM.

These tools are read-only and tier-1: the LLM should be able to look things
up cheaply at any point during a turn.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG


@tool(name="query_find_by_name", tier=1)
def find_by_name(
    kg: ProjectKG,
    name: str,
    node_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Trouve les éléments dont l'attribut `name` correspond exactement.

    Concepts: recherche, lookup, identification, filtre
    Phrases: "trouve N00", "où est le niveau RDC", "find by name"
    Similar: query_find_in_region, aggregations_count

    Args:
        name: valeur exacte de l'attribut name (sensible à la casse).
        node_type: type optionnel pour restreindre la recherche
            (Level, Wall, Room, Door, Window, WallType, FamilyType).

    Returns:
        {"matches": [{llm_id, _type, name}, ...]}
    """
    matches: List[Dict[str, Any]] = []
    for nid in kg.find_by_name(name=name, node_type=node_type):
        attrs = kg.get_node(nid)
        matches.append({
            "llm_id": nid,
            "_type": attrs.get("_type"),
            "name": attrs.get("name"),
        })
    return {"matches": matches}
