"""walls.py — création et modification de murs.

Slice scope: `walls_create` only. Sufficient to exercise atomic KG mutation
through the dispatcher.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

from ..llm_protocol import tool
from ..project_kg import ProjectKG


def _length(p1: List[float], p2: List[float]) -> float:
    return math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)


@tool(name="walls_create", tier=1)
def create(
    kg: ProjectKG,
    level_ref: str,
    wall_type_ref: str,
    p1: List[float],
    p2: List[float],
    height: float,
) -> Dict[str, Any]:
    """Crée un mur droit entre deux points sur un niveau donné.

    Le mur est ajouté au KG et lié par les arêtes `at_level` (vers le Level)
    et `is_type` (vers le WallType). Longueur calculée depuis p1, p2.

    Concepts: mur, création, géométrie, plan
    Phrases: "dessine un mur", "trace un mur", "ajoute un mur de X à Y",
             "create a wall"
    Similar: walls_modify, walls_delete

    Args:
        level_ref: llm_id du Level cible (obtenu via catalog_list_levels).
        wall_type_ref: llm_id du WallType (obtenu via catalog_list_wall_types).
        p1: point de départ [x, y] en mètres dans le plan du niveau.
        p2: point d'arrivée [x, y] en mètres dans le plan du niveau.
        height: hauteur du mur en mètres.

    Returns:
        {"ok": bool, "llm_id": str, "length_m": float}
    """
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if not kg.has_node(wall_type_ref):
        raise ValueError("Unknown wall_type_ref: {}".format(wall_type_ref))

    length = _length(p1, p2)
    llm_id = kg.add_node("Wall", {
        "type_ref": wall_type_ref,
        "level_ref": level_ref,
        "p1": list(p1),
        "p2": list(p2),
        "length": length,
        "height": height,
    })
    kg.add_edge(llm_id, level_ref, "at_level")
    kg.add_edge(llm_id, wall_type_ref, "is_type")
    return {"ok": True, "llm_id": llm_id, "length_m": round(length, 3)}
