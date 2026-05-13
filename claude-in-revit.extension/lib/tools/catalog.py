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


def _list_family_types_by_category(
    kg: ProjectKG, category: str,
) -> List[Dict[str, Any]]:
    """Filter FamilyType nodes by their `category` attr. Shared by
    `catalog_list_door_types` / `_window_types`. Includes `dimensions`
    when the type's family exposes recognised height/width parameters
    (`{height_m, width_m}` — either key may be absent for partial
    coverage)."""
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("FamilyType"):
        attrs = kg.get_node(nid)
        if attrs.get("category") != category:
            continue
        entry: Dict[str, Any] = {
            "llm_id": nid,
            "family_name": attrs.get("family_name"),
            "type_name": attrs.get("type_name"),
        }
        dims = attrs.get("dimensions")
        if dims:
            entry["dimensions"] = dims
        out.append(entry)
    return out


@tool(name="catalog_list_door_types", tier=1)
def list_door_types(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les types de porte (`FamilyType` de catégorie "Doors").

    Concepts: type de porte, door type, famille, catalogue, inventaire
    Phrases: "quels types de porte", "list door types", "catalogue portes"
    Similar: catalog_list_window_types, openings_create_door

    Args:
        (aucun)

    Returns:
        {"door_types": [{llm_id, family_name, type_name}, ...]}
    """
    return {"door_types": _list_family_types_by_category(kg, "Doors")}


@tool(name="catalog_list_window_types", tier=1)
def list_window_types(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les types de fenêtre (`FamilyType` de catégorie "Windows").

    Concepts: type de fenêtre, window type, famille, catalogue, inventaire
    Phrases: "quels types de fenêtre", "list window types",
             "catalogue fenêtres"
    Similar: catalog_list_door_types, openings_create_window

    Args:
        (aucun)

    Returns:
        {"window_types": [{llm_id, family_name, type_name}, ...]}
    """
    return {"window_types": _list_family_types_by_category(kg, "Windows")}


@tool(name="catalog_list_doors", tier=1)
def list_doors(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste toutes les portes vivantes du projet avec leur géométrie complète.

    Concepts: porte, door, ouverture, inventaire, hosted
    Phrases: "liste les portes", "quelles portes", "all doors",
             "toutes les portes"
    Similar: catalog_list_windows, catalog_list_walls, openings_create_door

    Args:
        (aucun)

    Returns:
        {"doors": [{llm_id, host_wall_ref, type_ref, position,
                    sill_height, head_height}, ...]}
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Door"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "host_wall_ref": attrs.get("host_wall_ref"),
            "type_ref": attrs.get("type_ref"),
            "position": attrs.get("position"),
            "sill_height": attrs.get("sill_height"),
            "head_height": attrs.get("head_height"),
        })
    return {"doors": out}


@tool(name="catalog_list_windows", tier=1)
def list_windows(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste toutes les fenêtres vivantes du projet avec leur géométrie complète.

    Concepts: fenêtre, window, ouverture, inventaire, hosted, allège
    Phrases: "liste les fenêtres", "quelles fenêtres", "all windows",
             "toutes les fenêtres"
    Similar: catalog_list_doors, catalog_list_walls, openings_create_window

    Args:
        (aucun)

    Returns:
        {"windows": [{llm_id, host_wall_ref, type_ref, position,
                      sill_height, head_height}, ...]}
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Window"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "host_wall_ref": attrs.get("host_wall_ref"),
            "type_ref": attrs.get("type_ref"),
            "position": attrs.get("position"),
            "sill_height": attrs.get("sill_height"),
            "head_height": attrs.get("head_height"),
        })
    return {"windows": out}


@tool(name="catalog_list_floor_types", tier=1)
def list_floor_types(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les types de sol / dalle (FloorType) avec llm_id, nom et
    épaisseur en mètres.

    Concepts: type de sol, type de dalle, floor type, slab type, catalogue
    Phrases: "quels types de sol", "list floor types", "catalogue dalles",
             "types de plancher"
    Similar: catalog_list_floors, catalog_list_wall_types

    Args:
        (aucun)

    Returns:
        {"floor_types": [{llm_id, name, total_thickness}, ...]}
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("FloorType"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "name": attrs.get("name"),
            "total_thickness": attrs.get("total_thickness"),
        })
    return {"floor_types": out}


@tool(name="catalog_list_floors", tier=1)
def list_floors(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les sols vivants du projet (Floor) avec contour et aire.

    Concepts: sol, dalle, floor, slab, plancher, inventaire, contour, aire
    Phrases: "liste les sols", "quels sols", "all floors", "toutes les dalles",
             "donne-moi les sols", "where are the slabs"
    Similar: catalog_list_floor_types, catalog_list_levels, catalog_list_walls

    Args:
        (aucun)

    Returns:
        {"floors": [{llm_id, level_ref, type_ref, area_m2,
                     vertex_count, boundary}, ...]}
        `boundary` est la polyligne `[[x, y], ...]` en mètres, polygone
        fermé implicite (1er sommet pas répété en fin).
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Floor"):
        attrs = kg.get_node(nid)
        boundary = attrs.get("boundary", [])
        out.append({
            "llm_id": nid,
            "level_ref": attrs.get("level_ref"),
            "type_ref": attrs.get("type_ref"),
            "area_m2": round(float(attrs.get("area_m2", 0.0)), 3),
            "vertex_count": len(boundary),
            "boundary": boundary,
        })
    return {"floors": out}


@tool(name="catalog_list_rooms", tier=1)
def list_rooms(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste toutes les pièces (Rooms) vivantes du projet avec leur aire et niveau.

    Concepts: pièce, room, espace, locale, inventaire, aire, surface
    Phrases: "liste les pièces", "quelles rooms", "all rooms",
             "toutes les pièces", "donne-moi les surfaces"
    Similar: catalog_list_levels, catalog_list_walls, rooms_get_area,
             rooms_recompute_boundaries

    Args:
        (aucun)

    Returns:
        {"rooms": [{llm_id, name, level_ref, area_m2}, ...]}
        Les rooms « unplaced » (aire = 0) apparaissent normalement dans
        la liste — le LLM peut les détecter via `area_m2 == 0` et
        suggérer un `rooms_recompute_boundaries` après que l'utilisateur
        ait fermé l'enveloppe murale.
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Room"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "name": attrs.get("name"),
            "level_ref": attrs.get("level_ref"),
            "area_m2": round(float(attrs.get("area", 0.0)), 3),
        })
    return {"rooms": out}


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
