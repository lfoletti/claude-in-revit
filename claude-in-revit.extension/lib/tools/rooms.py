"""tools/rooms.py — création, nommage et consultation des Rooms.

Pattern doc-aware standard (cf. `walls.py` / `openings.py`) : si `doc is None`
(CLI / pytest), seule la mutation KG est appliquée et `revit_id: None` est
explicitement retourné. Sinon, la branche Revit ouvre une
`revit_primitives.transaction` qui enveloppe `Document.Create.NewRoom`
+ `kg.add_node`/`set_revit_id` + `stamp_llm_id` ; le `kg.transaction()`
externe (ouvert par le dispatcher) fournit la rollback symétrique.

**Boundary walls non calculés en V0.** L'attribut `boundary_walls` reste à
`[]` à la création comme au rescan. Le calcul réel via
`Room.GetBoundarySegments` est reporté à l'arrivée de la compliance
(UC8) où la liste devient load-bearing.

**Aire**. `area` est lue depuis `ROOM_AREA` après `doc.Regenerate()` : Revit
recalcule à partir des boucles de boundary. Une room « unplaced » (pas
d'enveloppe murale) renvoie `area=0` — c'est attendu et signalé par le
préfixe `note` dans la réponse.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG
from ._helpers import bulk_setter_summary, stamp_llm_id


# ----- Internal helpers --------------------------------------------------


def _require_live_room(kg: ProjectKG, llm_id: str) -> Dict[str, Any]:
    """Preflight: node exists, is a Room, not soft-deleted. Returns attrs."""
    if not kg.has_node(llm_id):
        raise ValueError("Unknown llm_id: {}".format(llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Room":
        raise ValueError(
            "llm_id {} is a {}, not a Room".format(llm_id, node.get("_type"))
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError("Room {} is already soft-deleted".format(llm_id))
    return node


def _record_in_kg(
    kg: ProjectKG,
    *,
    level_ref: str,
    name: str,
    area: float = 0.0,
) -> str:
    """KG-side creation. Returns the new room's llm_id.

    `boundary_walls=[]` posed at creation — populated later by a future
    boundary-loop scan (compliance work). `at_level` is the only edge in V0.
    """
    llm_id = kg.add_node("Room", {
        "name": name,
        "level_ref": level_ref,
        "area": float(area),
        "boundary_walls": [],
    })
    kg.add_edge(llm_id, level_ref, "at_level")
    return llm_id


# ----- Tools -------------------------------------------------------------


@tool(name="rooms_create", tier=1)
def create(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    point: List[float],
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Place une pièce (Room) sur un niveau au point donné, dans une enceinte
    murale existante.

    Revit place la room à `point=[x, y]` et l'associe au `level_ref`. Si
    le point n'est pas enclos par des murs (ou par des room separation
    lines), la room est créée mais « unplaced » : `area=0` jusqu'à ce que
    l'utilisateur ferme l'enveloppe puis appelle `rooms_recompute_boundaries`.

    Concepts: pièce, room, espace, locale, plan, niveau, création
    Phrases: "crée une pièce", "ajoute une room", "place a room",
             "définis cet espace"
    Similar: rooms_set_name, rooms_recompute_boundaries, rooms_get_area

    Args:
        level_ref: llm_id du Level cible (obtenu via `catalog_list_levels`).
        point: [x, y] en mètres dans le plan du niveau. Doit tomber à
            l'intérieur d'une enveloppe murale fermée pour que Revit
            calcule l'aire ; sinon room créée mais `area=0`.
        name: nom optionnel (par défaut Revit pose un nom générique
            "Room" — `rooms_set_name` peut le changer ensuite).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None,
         "area_m2": float, "name": str, "note": str | None}
        `note` est posé si Revit a retourné `area=0` à la création
        (room unplaced ou enveloppe non fermée) — le LLM doit alerter.
    """
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if kg.get_node(level_ref).get("_type") != "Level":
        raise ValueError(
            "level_ref {} is a {}, not a Level".format(
                level_ref, kg.get_node(level_ref).get("_type"),
            )
        )
    if not isinstance(point, list) or len(point) != 2:
        raise ValueError("point must be [x, y] in metres")
    requested_name = name if name else "Room"

    if doc is None:
        llm_id = _record_in_kg(
            kg, level_ref=level_ref, name=requested_name, area=0.0,
        )
        return {
            "ok": True,
            "llm_id": llm_id,
            "revit_id": None,
            "area_m2": 0.0,
            "name": requested_name,
            "note": None,
        }

    level_eid_raw = kg.get_revit_id(level_ref)
    if level_eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(level_ref)
        )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId, UV

    level_eid = ElementId(level_eid_raw)
    uv = UV(rp.meters_to_internal(point[0]), rp.meters_to_internal(point[1]))

    revit_id: Optional[int] = None
    actual_area = 0.0
    actual_name = requested_name
    llm_id_out: Optional[str] = None

    with rp.transaction(doc, "rooms.create"):
        level = doc.GetElement(level_eid)
        room = doc.Create.NewRoom(level, uv)
        revit_id = int(room.Id.Value)

        # Nom : Revit pose un default ("Room") — si l'utilisateur a passé
        # un nom non vide, on l'écrit avant le Regenerate pour que la
        # lecture post-régen voit le bon name.
        if name:
            name_param = room.get_Parameter(BuiltInParameter.ROOM_NAME)
            if name_param is not None and not name_param.IsReadOnly:
                name_param.Set(str(name))

        # Regenerate pour que ROOM_AREA reflète la boundary effective
        # post-placement (sinon area=0 même si l'enveloppe est fermée).
        doc.Regenerate()

        llm_id_out = _record_in_kg(
            kg, level_ref=level_ref, name=requested_name, area=0.0,
        )
        kg.set_revit_id(llm_id_out, revit_id)
        stamp_llm_id(room, llm_id_out)
        # Mirror Revit truth via the central read-back helper — picks up
        # the post-Regenerate area and the actual name (in case Revit
        # rejected our Set silently).
        kg_sync.refresh_node_from_revit(kg, doc, llm_id_out)

    refreshed = kg.get_node(llm_id_out)
    actual_area = float(refreshed.get("area", 0.0))
    actual_name = refreshed.get("name", requested_name)
    note = None
    if actual_area <= 0.0:
        note = (
            "Room créée mais area=0 — point hors enveloppe murale fermée "
            "ou room \"unplaced\". Ferme les murs autour puis appelle "
            "`rooms_recompute_boundaries`."
        )
    return {
        "ok": True,
        "llm_id": llm_id_out,
        "revit_id": revit_id,
        "area_m2": round(actual_area, 3),
        "name": actual_name,
        "note": note,
    }


@tool(name="rooms_set_name", tier=1)
def set_name(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    name: str,
) -> Dict[str, Any]:
    """Change le nom d'une pièce (paramètre `ROOM_NAME`).

    Concepts: pièce, room, nom, name, renomme, étiquette
    Phrases: "renomme cette pièce", "set room name", "appelle cette pièce",
             "nomme cet espace"
    Similar: rooms_create, rooms_get_area, query_find_by_name

    Args:
        llm_id: llm_id de la room.
        name: nouveau nom (non vide).

    Returns:
        {"ok": bool, "llm_id": str, "name": str, "requested_name": str,
         "drift": bool, "drift_note": str | None}
    """
    _require_live_room(kg, llm_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    new_name = name.strip()

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
            "Room {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "rooms.set_name"):
        room = doc.GetElement(eid)
        param = room.get_Parameter(BuiltInParameter.ROOM_NAME)
        if param is None:
            raise ValueError(
                "Room {} has no ROOM_NAME parameter — unexpected.".format(llm_id)
            )
        if param.IsReadOnly:
            raise ValueError(
                "ROOM_NAME on {} is read-only.".format(llm_id)
            )
        param.Set(new_name)
        kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual_name = kg.get_node(llm_id).get("name", new_name)
    # Drift detection on strings : direct equality. `detect_drift` is
    # numeric-only, so we open-code the comparison here.
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


@tool(name="rooms_recompute_boundaries", tier=1)
def recompute_boundaries(
    kg: ProjectKG,
    doc: Any,
    llm_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Force Revit à recalculer les boundaries (et donc les aires) puis
    mirror dans le KG.

    Appelle `doc.Regenerate()` qui reconstruit les loops de toutes les
    rooms — Revit le fait normalement automatiquement à la modification
    d'un mur, mais après une série de mutations le KG peut être en retard
    sur les aires. Si `llm_id` est fourni, seule cette room est
    rafraîchie ; sinon toutes les rooms vivantes du projet le sont.

    Concepts: pièce, room, aire, boundary, recalcul, regenerate, surface
    Phrases: "recalcule les aires", "regenerate rooms", "rafraîchis les
             pièces", "update room areas"
    Similar: rooms_get_area, rooms_set_name

    Args:
        llm_id: optionnel. Si présent, ne recalcule que cette room. Si
            None, rafraîchit toutes les rooms du projet.

    Returns:
        {"ok": bool, "rooms_refreshed": int,
         "refreshed": [{llm_id, name, area_m2}, ...]}
    """
    if llm_id is not None:
        _require_live_room(kg, llm_id)
        target_ids = [llm_id]
    else:
        target_ids = [
            nid for nid in kg.find_by_type("Room")
            if kg.get_node(nid).get("deleted_at_turn") is None
        ]

    if doc is None:
        # KG-only path : no Revit Regenerate, areas stay whatever the KG
        # carries. Caller (tests / CLI) gets a deterministic shape.
        out = []
        for nid in target_ids:
            attrs = kg.get_node(nid)
            out.append({
                "llm_id": nid,
                "name": attrs.get("name"),
                "area_m2": round(float(attrs.get("area", 0.0)), 3),
            })
        return {
            "ok": True,
            "rooms_refreshed": len(out),
            "refreshed": out,
            "revit_regenerated": False,
        }

    from .. import kg_sync, revit_primitives as rp

    with rp.transaction(doc, "rooms.recompute_boundaries"):
        doc.Regenerate()
        for nid in target_ids:
            kg_sync.refresh_node_from_revit(kg, doc, nid)

    out = []
    for nid in target_ids:
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "name": attrs.get("name"),
            "area_m2": round(float(attrs.get("area", 0.0)), 3),
        })
    return {
        "ok": True,
        "rooms_refreshed": len(out),
        "refreshed": out,
        "revit_regenerated": True,
    }


@tool(name="rooms_get_area", tier=1)
def get_area(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
) -> Dict[str, Any]:
    """Retourne l'aire (m²) d'une pièce.

    Lit depuis le KG. Si `doc` est disponible et la room est bindée
    Revit, fait un read-back préalable pour mirror l'aire courante (au
    cas où le KG serait derrière une modification de mur récente). Sinon
    retourne la valeur KG telle quelle.

    Concepts: pièce, room, aire, surface, m2, mesure, area
    Phrases: "aire de cette pièce", "quelle est la surface", "room area",
             "donne-moi les m2"
    Similar: rooms_recompute_boundaries, catalog_list_rooms

    Args:
        llm_id: llm_id de la room.

    Returns:
        {"ok": bool, "llm_id": str, "name": str, "level_ref": str,
         "area_m2": float, "stale": bool}
        `stale=True` quand la valeur vient du KG sans read-back (pas de
        doc Revit en main) — utile au LLM pour décider s'il doit appeler
        `rooms_recompute_boundaries`.
    """
    attrs = _require_live_room(kg, llm_id)

    stale = True
    if doc is not None and kg.get_revit_id(llm_id) is not None:
        from .. import kg_sync
        kg_sync.refresh_node_from_revit(kg, doc, llm_id)
        attrs = kg.get_node(llm_id)
        stale = False

    return {
        "ok": True,
        "llm_id": llm_id,
        "name": attrs.get("name"),
        "level_ref": attrs.get("level_ref"),
        "area_m2": round(float(attrs.get("area", 0.0)), 3),
        "stale": stale,
    }


@tool(name="rooms_delete", tier=1)
def delete(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
) -> Dict[str, Any]:
    """Supprime une pièce (Revit + soft delete KG).

    Côté KG, soft delete : `deleted_at_turn=N` posé, le nœud reste pour
    la traçabilité historique. Côté Revit, suppression dure.

    Concepts: pièce, room, suppression, delete, supprime
    Phrases: "supprime cette pièce", "delete the room", "enlève la room",
             "vire cette pièce"
    Similar: rooms_create, walls_delete

    Args:
        llm_id: llm_id de la room à supprimer.

    Returns:
        {"ok": bool, "llm_id": str, "deleted_at_turn": int,
         "revit_deleted": bool}
    """
    _require_live_room(kg, llm_id)

    if doc is None:
        kg.soft_delete(llm_id)
        return {
            "ok": True,
            "llm_id": llm_id,
            "deleted_at_turn": kg.turn,
            "revit_deleted": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Room {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "rooms.delete"):
        doc.Delete(eid)
        kg.soft_delete(llm_id)

    return {
        "ok": True,
        "llm_id": llm_id,
        "deleted_at_turn": kg.turn,
        "revit_deleted": True,
    }


# ----- Bulk setters (V0 session 2026-05-12 b — dette setters_many) ----------


def _validate_set_name_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> Dict[str, Any]:
    """Preflight one item from `rooms_set_name_many` : `{llm_id, name}`.
    Collision detection happens at batch level (cross-item + vs untouched),
    not here."""
    if not isinstance(item, dict):
        raise ValueError(
            "items[{}] must be a dict, got {}".format(index, type(item).__name__)
        )
    llm_id = item.get("llm_id")
    name = item.get("name")
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Room":
        raise ValueError(
            "items[{}]: {} is a {}, not a Room".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is soft-deleted".format(index, llm_id)
        )
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            "items[{}]: name must be a non-empty string".format(index)
        )
    return {"llm_id": llm_id, "name": name.strip()}


@tool(name="rooms_set_name_many", tier=1)
def set_name_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Renomme N pièces en une seule Tx Revit + une seule Tx KG.

    **Note Revit** : contrairement aux Levels, les Rooms autorisent
    plusieurs pièces avec le même nom (c'est le `Number` qui doit être
    unique, pas le `Name`). Donc pas de pré-check de collision côté
    KG — on laisse l'utilisateur libre de nommer deux salons "Salon".

    Concepts: pièce, room, nom, renomme, bulk, batch, plusieurs, masse
    Phrases: "renomme toutes ces pièces", "uniformise les noms",
             "bulk rename rooms"
    Similar: rooms_set_name, levels_set_name

    Args:
        items: liste de specs `{llm_id: str, name: str}`. Au moins un
            item, chaque `llm_id` doit pointer sur une Room vivante.

    Returns:
        Réponse compacte (`_helpers.bulk_setter_summary`). Drifts pointent
        les rooms où le name committé ≠ name demandé (rare — Revit
        accepte presque toujours un name string).
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [_validate_set_name_item(kg, it, i) for i, it in enumerate(items)]

    if doc is None:
        for spec in specs:
            kg.modify_node(spec["llm_id"], {"name": spec["name"]})
        return bulk_setter_summary([], count=len(specs), revit_modified=False)

    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["llm_id"]) is None:
            raise ValueError(
                "items[{}]: Room {} has no Revit binding — run Refresh KG.".format(
                    i, spec["llm_id"],
                )
            )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    drifts: List[Dict[str, Any]] = []
    with rp.transaction(doc, "rooms.set_name_many"):
        for spec in specs:
            eid = ElementId(kg.get_revit_id(spec["llm_id"]))
            room = doc.GetElement(eid)
            param = room.get_Parameter(BuiltInParameter.ROOM_NAME)
            if param is None:
                raise ValueError(
                    "Room {} has no ROOM_NAME parameter — unexpected.".format(
                        spec["llm_id"],
                    )
                )
            if param.IsReadOnly:
                raise ValueError(
                    "ROOM_NAME on {} is read-only.".format(spec["llm_id"])
                )
            param.Set(spec["name"])
            kg_sync.refresh_node_from_revit(kg, doc, spec["llm_id"])
            actual = kg.get_node(spec["llm_id"]).get("name", spec["name"])
            if actual != spec["name"]:
                drifts.append({
                    "llm_id": spec["llm_id"],
                    "note": "Revit a commit name={!r} au lieu de {!r}".format(
                        actual, spec["name"],
                    ),
                })

    return bulk_setter_summary(drifts, count=len(specs), revit_modified=True)


def _validate_delete_item(
    kg: ProjectKG, item: Any, index: int,
) -> str:
    """Preflight one item from `rooms_delete_many` : `{llm_id}` ou string."""
    if isinstance(item, dict):
        llm_id = item.get("llm_id")
    else:
        llm_id = item
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Room":
        raise ValueError(
            "items[{}]: {} is a {}, not a Room".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is already soft-deleted".format(index, llm_id)
        )
    return llm_id


@tool(name="rooms_delete_many", tier=1)
def delete_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Any],
) -> Dict[str, Any]:
    """Supprime N pièces en une seule Tx Revit + KG.

    Tolérant aux ElementId périmés (orphelins KG).

    Concepts: pièce, room, suppression, bulk, batch, masse, delete
    Phrases: "supprime toutes ces pièces", "delete rooms",
             "vire les rooms", "remove rooms"
    Similar: rooms_delete, rooms_set_name_many

    Args:
        items: liste de llm_ids ou specs `{"llm_id": str}`.

    Returns:
        {"ok", "count", "deleted_revit", "deleted_kg_only",
         "revit_already_gone": [llm_id], "deleted_at_turn",
         "revit_modified"}
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    llm_ids = [_validate_delete_item(kg, it, i) for i, it in enumerate(items)]

    if doc is None:
        for nid in llm_ids:
            kg.soft_delete(nid)
        return {
            "ok": True,
            "count": len(llm_ids),
            "deleted_revit": 0,
            "deleted_kg_only": len(llm_ids),
            "revit_already_gone": [],
            "deleted_at_turn": kg.turn,
            "revit_modified": False,
        }

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    deleted_revit = 0
    revit_already_gone: List[str] = []
    with rp.transaction(doc, "rooms.delete_many"):
        for nid in llm_ids:
            eid_raw = kg.get_revit_id(nid)
            if eid_raw is None:
                kg.soft_delete(nid)
                revit_already_gone.append(nid)
                continue
            element = doc.GetElement(ElementId(eid_raw))
            if element is None:
                kg.soft_delete(nid)
                revit_already_gone.append(nid)
                continue
            try:
                doc.Delete(ElementId(eid_raw))
                kg.soft_delete(nid)
                deleted_revit += 1
            except Exception:  # noqa: BLE001
                kg.soft_delete(nid)
                revit_already_gone.append(nid)

    return {
        "ok": True,
        "count": len(llm_ids),
        "deleted_revit": deleted_revit,
        "deleted_kg_only": len(llm_ids) - deleted_revit,
        "revit_already_gone": revit_already_gone,
        "deleted_at_turn": kg.turn,
        "revit_modified": True,
    }
