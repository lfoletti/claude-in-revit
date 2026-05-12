"""tools/levels.py — création et modification de niveaux (Levels).

Pattern doc-aware standard. `set_active` est volontairement omis : c'est
une opération UX sur les vues (changer le plan d'étage actif dans
l'UIDocument), pas une mutation du modèle. L'utilisateur peut basculer
de vue directement dans Revit sans tool dédié.

`levels_delete` est également omis pour V0 : la suppression d'un niveau
casse les refs `at_level` de tous les éléments hôtés (Walls, Columns,
Rooms, Doors, Windows) et nécessite une stratégie de re-hosting que la
session courante ne couvre pas — reporté.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG
from ._helpers import stamp_llm_id


# ----- Internal helpers --------------------------------------------------


def _require_live_level(kg: ProjectKG, llm_id: str) -> Dict[str, Any]:
    """Preflight: node exists, is a Level, not soft-deleted. Returns attrs."""
    if not kg.has_node(llm_id):
        raise ValueError("Unknown llm_id: {}".format(llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Level":
        raise ValueError(
            "llm_id {} is a {}, not a Level".format(llm_id, node.get("_type"))
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError("Level {} is already soft-deleted".format(llm_id))
    return node


def _record_in_kg(kg: ProjectKG, *, name: str, elevation: float) -> str:
    """KG-side creation. Returns the new level's llm_id. No edges (Levels
    have no inbound refs — they're depended-on, never depend on others)."""
    return kg.add_node("Level", {
        "name": name,
        "elevation": float(elevation),
    })


def _name_collision(kg: ProjectKG, name: str, exclude_id: Optional[str] = None) -> bool:
    """Return True iff another live Level already uses `name`. Revit refuses
    duplicate Level names — pre-check côté KG pour rendre l'erreur plus
    lisible que l'`InvalidOperationException` brute de Revit."""
    for nid in kg.find_by_type("Level"):
        if nid == exclude_id:
            continue
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is not None:
            continue
        if node.get("name") == name:
            return True
    return False


# ----- Tools -------------------------------------------------------------


@tool(name="levels_create", tier=1)
def create(
    kg: ProjectKG,
    doc: Any,
    name: str,
    elevation_m: float,
) -> Dict[str, Any]:
    """Crée un nouveau niveau à l'altitude donnée.

    Le niveau n'a pas de vue associée à sa création — Revit ne fabrique
    pas automatiquement de plan d'étage. L'utilisateur peut le faire
    depuis l'onglet Vue / Plan d'étage dans Revit si nécessaire.

    Concepts: niveau, level, étage, étages, plan, élévation, création
    Phrases: "ajoute un niveau", "crée un étage", "create a level",
             "nouveau niveau à X m"
    Similar: levels_set_elevation, levels_set_name, catalog_list_levels

    Args:
        name: nom du niveau (ex: "N01", "Étage 1", "Toiture"). Unique
            dans le projet — Revit refuse les doublons.
        elevation_m: altitude en mètres (origine = niveau 0 du projet).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None,
         "name": str, "elevation_m": float}
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    new_name = name.strip()
    if _name_collision(kg, new_name):
        raise ValueError(
            "Level name {!r} already exists — Revit refuses duplicates.".format(new_name)
        )
    elev = float(elevation_m)

    if doc is None:
        llm_id = _record_in_kg(kg, name=new_name, elevation=elev)
        return {
            "ok": True,
            "llm_id": llm_id,
            "revit_id": None,
            "name": new_name,
            "elevation_m": round(elev, 3),
        }

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import Level

    elev_ft = rp.meters_to_internal(elev)

    revit_id: Optional[int] = None
    llm_id_out: Optional[str] = None

    with rp.transaction(doc, "levels.create"):
        # Static factory: `Level.Create(doc, elevation_ft)` returns the
        # new Level instance. Auto-generated name like "Level 3" — we
        # rename afterward via the writable `Name` property.
        level = Level.Create(doc, elev_ft)
        revit_id = int(level.Id.Value)
        # Revit auto-names ("Level 3", "Niveau 3" depending on locale) —
        # rename to the requested string. Direct property assignment is
        # the documented path; collisions raise InvalidOperationException
        # which propagates out (already pre-checked via _name_collision
        # so this should not fire under normal flow).
        level.Name = new_name

        llm_id_out = _record_in_kg(kg, name=new_name, elevation=elev)
        kg.set_revit_id(llm_id_out, revit_id)
        stamp_llm_id(level, llm_id_out)
        kg_sync.refresh_node_from_revit(kg, doc, llm_id_out)

    refreshed = kg.get_node(llm_id_out)
    return {
        "ok": True,
        "llm_id": llm_id_out,
        "revit_id": revit_id,
        "name": refreshed.get("name", new_name),
        "elevation_m": round(float(refreshed.get("elevation", elev)), 3),
    }


@tool(name="levels_set_elevation", tier=1)
def set_elevation(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    elevation_m: float,
) -> Dict[str, Any]:
    """Change l'altitude d'un niveau (paramètre `Level.Elevation`).

    Cascade Revit : tous les éléments hôtés (murs, colonnes, rooms…)
    suivent automatiquement. La discipline read-back KG↔Revit (session 5
    du 2026-05-11) traite ce niveau — mais les éléments hôtés ne sont
    PAS re-lus automatiquement : si une cascade a modifié leurs attrs
    (rare pour walls/columns qui sont relatifs au niveau, mais possible
    avec des contraintes Top), l'utilisateur doit lancer
    `rooms_recompute_boundaries` pour rafraîchir les aires.

    Concepts: niveau, level, altitude, élévation, hauteur, modification
    Phrases: "monte ce niveau à X m", "abaisse le niveau", "change
             l'altitude du niveau", "set level elevation"
    Similar: levels_create, levels_set_name

    Args:
        llm_id: llm_id du niveau.
        elevation_m: nouvelle altitude en mètres.

    Returns:
        {"ok": bool, "llm_id": str, "elevation_m": float,
         "requested_elevation_m": float, "drift": bool,
         "drift_note": str | None}
    """
    _require_live_level(kg, llm_id)
    new_elev = float(elevation_m)

    if doc is None:
        kg.modify_node(llm_id, {"elevation": new_elev})
        return {
            "ok": True,
            "llm_id": llm_id,
            "elevation_m": round(new_elev, 3),
            "requested_elevation_m": round(new_elev, 3),
            "drift": False,
            "drift_note": None,
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "levels.set_elevation"):
        level = doc.GetElement(eid)
        # `.Elevation` est writable directement (pas via Parameter.Set) —
        # documenté dans l'API Revit. Une contrainte d'élévation max
        # (rare) lèverait une exception qu'on laisse propager.
        level.Elevation = rp.meters_to_internal(new_elev)
        kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual_elev = float(kg.get_node(llm_id).get("elevation", new_elev))
    drift, drift_note = kg_sync.detect_drift(
        new_elev, actual_elev, field="elevation_m",
    )
    return {
        "ok": True,
        "llm_id": llm_id,
        "elevation_m": round(actual_elev, 3),
        "requested_elevation_m": round(new_elev, 3),
        "drift": drift,
        "drift_note": drift_note,
        "revit_modified": True,
    }


@tool(name="levels_set_name", tier=1)
def set_name(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    name: str,
) -> Dict[str, Any]:
    """Change le nom d'un niveau (propriété `Level.Name`).

    Pré-vérifie la collision côté KG — Revit refuse les doublons et
    lèverait sinon une `InvalidOperationException`. Le nouveau nom doit
    être unique parmi les niveaux vivants.

    Concepts: niveau, level, nom, renomme, étiquette
    Phrases: "renomme ce niveau", "appelle ce niveau", "set level name"
    Similar: levels_create, levels_set_elevation

    Args:
        llm_id: llm_id du niveau.
        name: nouveau nom (non vide, unique dans le projet).

    Returns:
        {"ok": bool, "llm_id": str, "name": str, "requested_name": str,
         "drift": bool, "drift_note": str | None}
    """
    _require_live_level(kg, llm_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    new_name = name.strip()
    if _name_collision(kg, new_name, exclude_id=llm_id):
        raise ValueError(
            "Level name {!r} already exists — Revit refuses duplicates.".format(new_name)
        )

    if doc is None:
        kg.modify_node(llm_id, {"name": new_name})
        return {
            "ok": True,
            "llm_id": llm_id,
            "name": new_name,
            "requested_name": new_name,
            "drift": False,
            "drift_note": None,
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "levels.set_name"):
        level = doc.GetElement(eid)
        level.Name = new_name
        kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual_name = kg.get_node(llm_id).get("name", new_name)
    drift = actual_name != new_name
    drift_note = (
        "Revit a commit name={!r} au lieu de {!r} demandé".format(
            actual_name, new_name,
        ) if drift else None
    )
    return {
        "ok": True,
        "llm_id": llm_id,
        "name": actual_name,
        "requested_name": new_name,
        "drift": drift,
        "drift_note": drift_note,
        "revit_modified": True,
    }
