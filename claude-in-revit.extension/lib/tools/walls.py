"""walls.py — création et modification de murs.

`walls_create` est *doc-aware* (§5 du design doc) : si le dispatcher injecte
un `Document` Revit, on crée vraiment le mur via `Wall.Create` ; sinon (CLI,
tests), on mute uniquement le KG. Le KG-only path reste utilisable pour le
slice harness et la rétro-compat des tests Semaine 0.

Atomicité §4.1 : la branche Revit ouvre une `revit_primitives.transaction`
qui *enveloppe à la fois* l'appel `Wall.Create` et les mutations KG
(`add_node`/`set_revit_id`/`add_edge`). En cas d'exception, Revit rollback
+ le `kg.transaction()` externe (ouvert par le dispatcher) restaure la
snapshot KG. La fenêtre résiduelle (persist disque qui échoue après le
commit Revit) reste couverte par `refresh_kg`.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set

from ._helpers import bulk_setter_summary, bulk_summary, stamp_llm_id
from ..llm_protocol import tool
from ..project_kg import ProjectKG


def _default_story_height(kg: ProjectKG, level_ref: str) -> float:
    """Story height = next level's elevation - this level's, in metres.

    Raises ValueError when the base level is the topmost in the project
    (no story above) — the agent must then provide an explicit height
    rather than guess one. Mirrors `columns._default_column_height` —
    kept separate for now; a shared `ProjectKG.story_height_above`
    method would consolidate both if a third caller emerges.
    """
    base = kg.get_node(level_ref)
    base_elev = base.get("elevation")
    if base_elev is None:
        raise ValueError(
            "Level {} has no elevation — can't infer story height.".format(level_ref)
        )
    above: List[float] = []
    for nid in kg.find_by_type("Level"):
        elev = kg.get_node(nid).get("elevation")
        if elev is not None and elev > base_elev:
            above.append(elev)
    if not above:
        raise ValueError(
            "No level above {} (elevation={} m). Specify `height` "
            "explicitly when the wall sits on the top level.".format(
                level_ref, base_elev,
            )
        )
    return min(above) - base_elev


def _length(p1: List[float], p2: List[float]) -> float:
    return math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)


def _record_in_kg(
    kg: ProjectKG,
    level_ref: str,
    wall_type_ref: str,
    p1: List[float],
    p2: List[float],
    length: float,
    height: float,
) -> str:
    """Mutate the KG side. Returns the new wall's llm_id."""
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
    return llm_id


@tool(name="walls_create", tier=1)
def create(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    wall_type_ref: str,
    p1: List[float],
    p2: List[float],
    height: float,
) -> Dict[str, Any]:
    """Crée un mur droit entre deux points sur un niveau donné.

    Si on est attaché à un document Revit, le mur est aussi instancié via
    `Wall.Create` et son ElementId est lié au nœud KG (`_revit_id`).
    Sinon (CLI, tests), seul le KG est muté.

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
        {"ok": bool, "llm_id": str, "length_m": float, "revit_id": int | None}
        **L'`llm_id` retourné est l'unique source de vérité pour identifier
        ce mur dans toute opération ultérieure (walls_move, walls_delete,
        walls_set_height, etc.).** Les compteurs internes peuvent avoir
        des trous (suppressions, sessions antérieures), donc ne JAMAIS
        deviner l'id par numérotation séquentielle ou offset — toujours
        lire le `llm_id` de ce dict, ou re-vérifier via `catalog_list_walls`
        avant une modification en masse.
    """
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if not kg.has_node(wall_type_ref):
        raise ValueError("Unknown wall_type_ref: {}".format(wall_type_ref))

    length = _length(p1, p2)

    if doc is None:
        # Hors-Revit (CLI / pytest) — pure KG mutation, no Revit binding.
        llm_id = _record_in_kg(kg, level_ref, wall_type_ref, p1, p2, length, height)
        return {
            "ok": True,
            "llm_id": llm_id,
            "length_m": round(length, 3),
            "revit_id": None,
        }

    # Revit-backed path. Binding checks happen BEFORE the Revit imports
    # so a hors-Revit caller with a non-None doc (test sentinel) gets a
    # clean ValueError rather than an ImportError from `revit_primitives`.
    level_eid_raw = kg.get_revit_id(level_ref)
    wt_eid_raw = kg.get_revit_id(wall_type_ref)
    if level_eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(level_ref)
        )
    if wt_eid_raw is None:
        raise ValueError(
            "WallType {} has no Revit binding — run Refresh KG.".format(wall_type_ref)
        )

    # Lazy Revit imports — only reached when we have everything we need.
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, Line, Wall, XYZ

    level_eid = ElementId(level_eid_raw)
    wt_eid = ElementId(wt_eid_raw)

    p1_xyz = XYZ(rp.meters_to_internal(p1[0]), rp.meters_to_internal(p1[1]), 0.0)
    p2_xyz = XYZ(rp.meters_to_internal(p2[0]), rp.meters_to_internal(p2[1]), 0.0)
    line = Line.CreateBound(p1_xyz, p2_xyz)
    height_ft = rp.meters_to_internal(height)

    revit_id: Optional[int] = None
    with rp.transaction(doc, "walls.create"):
        # `Wall.Create(doc, curve, wallTypeId, levelId, height, offset, flip, structural)`
        # — overload V0 cible, voir REVIT_API_NOTES Phase 1.
        wall = Wall.Create(doc, line, wt_eid, level_eid, height_ft, 0.0, False, False)
        revit_id = int(wall.Id.Value)
        llm_id = _record_in_kg(kg, level_ref, wall_type_ref, p1, p2, length, height)
        kg.set_revit_id(llm_id, revit_id)
        stamp_llm_id(wall, llm_id)
        # Read-back discipline (2026-05-11 session 5) : Revit may snap
        # endpoints to nearest gridline, refuse short curves, or
        # otherwise adjust geometry. KG mirrors what was committed.
        from .. import kg_sync as _kg_sync
        _kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual = kg.get_node(llm_id)
    return {
        "ok": True,
        "llm_id": llm_id,
        "length_m": round(actual.get("length", length), 3),
        "height_m": round(actual.get("height", height), 3),
        "p1_m": actual.get("p1"),
        "p2_m": actual.get("p2"),
        "revit_id": revit_id,
    }


def _require_live_wall(kg: ProjectKG, llm_id: str) -> Dict[str, Any]:
    """Common preflight for mutating tools.

    Raises ValueError if the node is missing, not a Wall, or already
    soft-deleted. Returns the node attrs dict so the caller can use it.
    """
    if not kg.has_node(llm_id):
        raise ValueError("Unknown llm_id: {}".format(llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Wall":
        raise ValueError(
            "llm_id {} is a {}, not a Wall".format(llm_id, node.get("_type"))
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError("Wall {} is already soft-deleted".format(llm_id))
    return node


@tool(name="walls_delete", tier=1)
def delete(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
) -> Dict[str, Any]:
    """Supprime un mur du projet (Revit + KG).

    Côté KG c'est un *soft delete* : le nœud reste avec
    `deleted_at_turn=N` posé, ce qui le retire des queries par défaut
    mais préserve la traçabilité historique. Côté Revit c'est une
    suppression dure (`Document.Delete`).

    Concepts: mur, suppression, delete, supprime
    Phrases: "supprime le mur", "delete the wall", "enlève le mur",
             "vire le mur"
    Similar: walls_modify, walls_move

    Args:
        llm_id: llm_id du mur à supprimer.

    Returns:
        {"ok": bool, "llm_id": str, "deleted_at_turn": int}
    """
    _require_live_wall(kg, llm_id)

    if doc is None:
        # Hors-Revit (CLI / tests) — KG soft delete uniquement.
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
            "Wall {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "walls.delete"):
        doc.Delete(eid)
        kg.soft_delete(llm_id)

    return {
        "ok": True,
        "llm_id": llm_id,
        "deleted_at_turn": kg.turn,
        "revit_deleted": True,
    }


@tool(name="walls_move", tier=1)
def move(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    dx: float,
    dy: float,
) -> Dict[str, Any]:
    """Translate un mur de (dx, dy) mètres dans le plan du niveau.

    La translation s'applique à p1 et p2 ; la longueur du mur ne change
    pas. Pour changer un mur de niveau, c'est un autre tool (réassigner
    `WALL_BASE_LEVEL_PARAM`).

    Concepts: mur, déplacement, translation, move, décale
    Phrases: "déplace le mur", "move the wall", "décale le mur",
             "translate the wall"
    Similar: walls_create, walls_set_height, walls_delete

    Args:
        llm_id: llm_id du mur à déplacer.
        dx: déplacement selon x en mètres.
        dy: déplacement selon y en mètres.

    Returns:
        {"ok": bool, "llm_id": str, "p1_m": [x, y], "p2_m": [x, y]}
    """
    node = _require_live_wall(kg, llm_id)
    new_p1 = [node["p1"][0] + dx, node["p1"][1] + dy]
    new_p2 = [node["p2"][0] + dx, node["p2"][1] + dy]

    if doc is None:
        kg.modify_node(llm_id, {"p1": new_p1, "p2": new_p2})
        return {
            "ok": True,
            "llm_id": llm_id,
            "p1_m": new_p1,
            "p2_m": new_p2,
            "requested_p1_m": new_p1,
            "requested_p2_m": new_p2,
            "drift": False,
            "drift_note": None,
            "revit_moved": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Wall {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, ElementTransformUtils, XYZ

    translation = XYZ(rp.meters_to_internal(dx), rp.meters_to_internal(dy), 0.0)
    eid = ElementId(eid_raw)
    with rp.transaction(doc, "walls.move"):
        ElementTransformUtils.MoveElement(doc, eid, translation)
        # Mirror Revit reality (read-back discipline 2026-05-11 session 5)
        # rather than trust the computed translation. Cheap insurance
        # against future cases where Revit might snap the move (e.g.
        # constrained by alignment, locked end, etc.).
        kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    refreshed = kg.get_node(llm_id)
    actual_p1 = refreshed["p1"]
    actual_p2 = refreshed["p2"]
    drift_p1, note_p1 = kg_sync.detect_drift(new_p1, actual_p1, field="p1")
    drift_p2, note_p2 = kg_sync.detect_drift(new_p2, actual_p2, field="p2")
    drift = drift_p1 or drift_p2
    drift_note = note_p1 or note_p2

    return {
        "ok": True,
        "llm_id": llm_id,
        "p1_m": actual_p1,
        "p2_m": actual_p2,
        "requested_p1_m": new_p1,
        "requested_p2_m": new_p2,
        "drift": drift,
        "drift_note": drift_note,
        "revit_moved": True,
    }


@tool(name="walls_set_height", tier=1)
def set_height(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    height_m: float,
) -> Dict[str, Any]:
    """Change la hauteur d'un mur (paramètre Revit `WALL_USER_HEIGHT_PARAM`).

    Concepts: mur, hauteur, modification, height, modify
    Phrases: "hauteur du mur à X m", "remonte le mur à X m",
             "set the wall height to X m"
    Similar: walls_move, walls_create

    Args:
        llm_id: llm_id du mur.
        height_m: nouvelle hauteur en mètres.

    Returns:
        {"ok": bool, "llm_id": str, "height_m": float}
    """
    _require_live_wall(kg, llm_id)

    if doc is None:
        kg.modify_node(llm_id, {"height": height_m})
        return {
            "ok": True,
            "llm_id": llm_id,
            "height_m": height_m,
            "requested_height_m": height_m,
            "drift": False,
            "drift_note": None,
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Wall {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "walls.set_height"):
        wall = doc.GetElement(eid)
        param = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        if param is None:
            raise ValueError(
                "Wall {} has no WALL_USER_HEIGHT_PARAM — type may not be a "
                "Basic Wall (curtain walls don't expose this parameter).".format(llm_id)
            )
        ok = param.Set(rp.meters_to_internal(height_m))
        if not ok:
            raise ValueError(
                "Setting WALL_USER_HEIGHT_PARAM on {} returned False — the "
                "parameter may be read-only (constrained by Top Constraint).".format(llm_id)
            )
        # Read-back discipline : Top Constraint or other rules may have
        # silently overridden our Set even though it returned True. The
        # KG mirrors what Revit actually kept.
        kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual_height = kg.get_node(llm_id).get("height", height_m)
    drift, drift_note = kg_sync.detect_drift(
        height_m, actual_height, field="height_m",
    )
    if drift and drift_note:
        drift_note = (
            drift_note
            + " (probable Top Constraint ou contrainte de niveau supérieur "
            "qui fixe la hauteur)"
        )

    return {
        "ok": True,
        "llm_id": llm_id,
        "height_m": round(actual_height, 3),
        "requested_height_m": round(height_m, 3),
        "drift": drift,
        "drift_note": drift_note,
        "revit_modified": True,
    }


# ----- Bulk creation engine + patterns ---------------------------------------


def _validate_wall_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> Dict[str, Any]:
    """Pre-flight one item from walls_create_many.

    Returns a normalised dict with float-cast `p1`/`p2`, resolved
    `height` (default = story height if omitted), and the input refs.
    Raises ValueError with the offending index on bad input.
    """
    if not isinstance(item, dict):
        raise ValueError("items[{}] must be a dict, got {}".format(index, type(item).__name__))
    level_ref = item.get("level_ref")
    wt_ref = item.get("wall_type_ref")
    p1 = item.get("p1")
    p2 = item.get("p2")
    height = item.get("height")  # may be None.

    if not isinstance(level_ref, str) or not kg.has_node(level_ref):
        raise ValueError("items[{}]: invalid level_ref {!r}".format(index, level_ref))
    if not isinstance(wt_ref, str) or not kg.has_node(wt_ref):
        raise ValueError("items[{}]: invalid wall_type_ref {!r}".format(index, wt_ref))
    if kg.get_node(wt_ref).get("_type") != "WallType":
        raise ValueError(
            "items[{}]: wall_type_ref {} is a {}, not a WallType".format(
                index, wt_ref, kg.get_node(wt_ref).get("_type"),
            )
        )
    if not isinstance(p1, list) or len(p1) != 2:
        raise ValueError("items[{}]: p1 must be [x, y] in metres".format(index))
    if not isinstance(p2, list) or len(p2) != 2:
        raise ValueError("items[{}]: p2 must be [x, y] in metres".format(index))
    if height is None:
        try:
            height = _default_story_height(kg, level_ref)
        except ValueError as exc:
            raise ValueError("items[{}]: {}".format(index, exc))
    elif not isinstance(height, (int, float)) or height <= 0:
        raise ValueError("items[{}]: height must be a positive number".format(index))

    return {
        "level_ref": level_ref,
        "wall_type_ref": wt_ref,
        "p1": [float(p1[0]), float(p1[1])],
        "p2": [float(p2[0]), float(p2[1])],
        "height": float(height),
    }


@tool(name="walls_create_many", tier=1)
def create_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Crée N murs en **une seule** transaction Revit + une seule transaction
    KG. Préférer ce tool dès qu'on a plusieurs murs à créer en une fois —
    speed-up typique 10-100× vs N appels séparés à `walls_create`.

    Transactionnel : si un item échoue, **aucune création** n'est commitée.

    Concepts: mur, bulk, batch, plusieurs, série, walls
    Phrases: "trace plusieurs murs", "crée tous ces murs", "batch walls"
    Similar: walls_create, walls_create_polyline, walls_create_from_lines

    Args:
        items: liste de specs. Chaque entrée est un dict :
            - `level_ref` (str, requis) : llm_id du Level.
            - `wall_type_ref` (str, requis) : llm_id du WallType.
            - `p1`, `p2` (list[float], requis) : [x, y] en mètres.
            - `height` (float, optionnel) : hauteur en mètres.
              Défaut = hauteur d'étage.

    Returns:
        Réponse compacte (`lib.tools._helpers.bulk_summary`) — soit
        `{ok, count, llm_ids: [...]}` pour les petits batchs, soit
        `{ok, count, first_llm_id, last_llm_id, contiguous, note}` pour
        les batchs larges contigus. Détails par item via
        `catalog_list_walls` ou `query_get_node(llm_id)`.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")

    specs = [_validate_wall_item(kg, item, i) for i, item in enumerate(items)]

    if doc is None:
        llm_ids: List[str] = []
        for spec in specs:
            length = _length(spec["p1"], spec["p2"])
            llm_ids.append(_record_in_kg(
                kg, spec["level_ref"], spec["wall_type_ref"],
                spec["p1"], spec["p2"], length, spec["height"],
            ))
        return bulk_summary(llm_ids)

    # Revit path — bindings upfront before importing Autodesk.Revit.DB.
    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["level_ref"]) is None:
            raise ValueError(
                "items[{}]: Level {} has no Revit binding — run Refresh KG.".format(
                    i, spec["level_ref"],
                )
            )
        if kg.get_revit_id(spec["wall_type_ref"]) is None:
            raise ValueError(
                "items[{}]: WallType {} has no Revit binding — run Refresh KG.".format(
                    i, spec["wall_type_ref"],
                )
            )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, Line, Wall, XYZ

    llm_ids: List[str] = []
    with rp.transaction(doc, "walls.create_many"):
        for spec in specs:
            level_eid = ElementId(kg.get_revit_id(spec["level_ref"]))
            wt_eid = ElementId(kg.get_revit_id(spec["wall_type_ref"]))
            p1_xyz = XYZ(
                rp.meters_to_internal(spec["p1"][0]),
                rp.meters_to_internal(spec["p1"][1]),
                0.0,
            )
            p2_xyz = XYZ(
                rp.meters_to_internal(spec["p2"][0]),
                rp.meters_to_internal(spec["p2"][1]),
                0.0,
            )
            line = Line.CreateBound(p1_xyz, p2_xyz)
            height_ft = rp.meters_to_internal(spec["height"])
            wall = Wall.Create(
                doc, line, wt_eid, level_eid, height_ft, 0.0, False, False,
            )
            revit_id = int(wall.Id.Value)
            length = _length(spec["p1"], spec["p2"])
            llm_id = _record_in_kg(
                kg, spec["level_ref"], spec["wall_type_ref"],
                spec["p1"], spec["p2"], length, spec["height"],
            )
            kg.set_revit_id(llm_id, revit_id)
            stamp_llm_id(wall, llm_id)
            llm_ids.append(llm_id)
        # Read-back discipline : mirror live Revit geometry on every
        # wall, in case Revit snapped any endpoint.
        from .. import kg_sync as _kg_sync
        for nid in llm_ids:
            _kg_sync.refresh_node_from_revit(kg, doc, nid)

    return bulk_summary(llm_ids)


@tool(name="walls_create_polyline", tier=1)
def create_polyline(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    wall_type_ref: str,
    vertices: List[List[float]],
    height: Optional[float] = None,
    closed: bool = False,
) -> Dict[str, Any]:
    """Crée des murs chaînés entre les sommets d'une polyligne.

    `vertices` est une liste de points `[[x, y], …]` en mètres. Génère
    `len(vertices) - 1` murs (ou `len(vertices)` si `closed=True`, en
    refermant sur le premier sommet). Cas typique : contour d'une
    pièce, façade chaînée.

    Pattern tool : Python construit les paires `(p1, p2)` localement,
    puis délègue à `walls_create_many` (single Tx Revit + KG).
    Coût LLM : N+2 entrées (vertices) au lieu de 5×N (un item par mur
    avec p1, p2, level, type, height répétés).

    Concepts: polyligne, chaîne, contour, périmètre, façade, polygon
    Phrases: "contour de la pièce", "trace les murs autour", "périmètre",
             "polyline of walls", "fermer le polygone"
    Similar: walls_create_many, walls_create_from_lines

    Args:
        level_ref: llm_id du Level (via catalog_list_levels).
        wall_type_ref: llm_id du WallType.
        vertices: liste de sommets `[[x, y], …]` en mètres, ≥ 2 points.
        height: hauteur uniforme en mètres. Optionnel — défaut hauteur
            d'étage.
        closed: si True, ajoute un mur de fermeture entre `vertices[-1]`
            et `vertices[0]`. Pratique pour une pièce ou un polygone.

    Returns:
        Même schéma que `walls_create_many`.
    """
    if not isinstance(vertices, list) or len(vertices) < 2:
        raise ValueError("vertices must be a list of at least 2 points")
    for k, v in enumerate(vertices):
        if not isinstance(v, list) or len(v) != 2:
            raise ValueError(
                "vertices[{}] must be [x, y] in metres, got {!r}".format(k, v)
            )

    pairs: List[tuple] = []
    for i in range(len(vertices) - 1):
        pairs.append((vertices[i], vertices[i + 1]))
    if closed:
        pairs.append((vertices[-1], vertices[0]))

    items = [
        {
            "level_ref": level_ref,
            "wall_type_ref": wall_type_ref,
            "p1": [float(p1[0]), float(p1[1])],
            "p2": [float(p2[0]), float(p2[1])],
            "height": height,
        }
        for p1, p2 in pairs
    ]
    return create_many(kg=kg, doc=doc, items=items)


@tool(name="walls_create_from_lines", tier=1)
def create_from_lines(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    wall_type_ref: str,
    line_llm_ids: List[str],
    height: Optional[float] = None,
) -> Dict[str, Any]:
    """Convertit des lignes (ModelLine / DetailLine) déjà dans le KG en murs.

    Pour chaque llm_id passé, lit les endpoints depuis le KG (p1/p2
    en `[x, y, z]`), **drop le z** (les murs sont 2D dans le plan du
    niveau), construit un item par mur et délègue à `walls_create_many`.

    Cas typique : l'utilisateur a tracé l'esquisse en lignes
    (architecturales ou de modèle) puis demande de les convertir en
    murs. Les lignes-source restent inchangées — c'est un *create*,
    pas un *convert in place*.

    Concepts: ligne, mur, conversion, esquisse, line to wall
    Phrases: "convertis les lignes en murs", "trace des murs sur ces lignes",
             "from lines to walls", "convert lines"
    Similar: walls_create_many, walls_create_polyline

    Args:
        level_ref: llm_id du Level cible.
        wall_type_ref: llm_id du WallType.
        line_llm_ids: liste de llm_ids de ModelLine ou DetailLine.
        height: hauteur uniforme en mètres. Optionnel — défaut hauteur
            d'étage.

    Returns:
        Même schéma que `walls_create_many`. Une erreur sur un id
        invalide (non-line, soft-deleted, inexistant) abandonne le
        batch entier — pas de demi-conversion.
    """
    if not isinstance(line_llm_ids, list) or not line_llm_ids:
        raise ValueError("line_llm_ids must be a non-empty list")

    items: List[Dict[str, Any]] = []
    for k, lid in enumerate(line_llm_ids):
        if not isinstance(lid, str) or not kg.has_node(lid):
            raise ValueError(
                "line_llm_ids[{}]: unknown llm_id {!r}".format(k, lid)
            )
        node = kg.get_node(lid)
        if node.get("_type") not in ("ModelLine", "DetailLine"):
            raise ValueError(
                "line_llm_ids[{}]: {} is a {}, not a line".format(
                    k, lid, node.get("_type"),
                )
            )
        if node.get("deleted_at_turn") is not None:
            raise ValueError(
                "line_llm_ids[{}]: {} is soft-deleted".format(k, lid)
            )
        p1 = node.get("p1")
        p2 = node.get("p2")
        if not (isinstance(p1, list) and isinstance(p2, list)):
            raise ValueError(
                "line_llm_ids[{}]: {} has no p1/p2 geometry".format(k, lid)
            )
        # Lines store [x, y, z]; drop the z for the wall plane.
        items.append({
            "level_ref": level_ref,
            "wall_type_ref": wall_type_ref,
            "p1": [float(p1[0]), float(p1[1])],
            "p2": [float(p2[0]), float(p2[1])],
            "height": height,
        })
    return create_many(kg=kg, doc=doc, items=items)


# ----- Bulk setters / movers (V0 session 2026-05-12 b — dette setters_many) -


def _validate_set_height_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> Dict[str, Any]:
    """Preflight one item from `walls_set_height_many` : `{llm_id, height_m}`."""
    if not isinstance(item, dict):
        raise ValueError(
            "items[{}] must be a dict, got {}".format(index, type(item).__name__)
        )
    llm_id = item.get("llm_id")
    height = item.get("height_m")
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Wall":
        raise ValueError(
            "items[{}]: {} is a {}, not a Wall".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is soft-deleted".format(index, llm_id)
        )
    if not isinstance(height, (int, float)) or height <= 0:
        raise ValueError(
            "items[{}]: height_m must be a positive number".format(index)
        )
    return {"llm_id": llm_id, "height_m": float(height)}


@tool(name="walls_set_height_many", tier=1)
def set_height_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Change la hauteur de N murs en **une seule** Tx Revit + une seule Tx KG.

    Préférer ce tool à N appels séparés à `walls_set_height` dès qu'il y
    a plusieurs murs à ajuster — un appel API au lieu de N, cache hit
    rate meilleur, latence /N. Transactionnel : un item invalide
    (llm_id inconnu, soft-deleted, height négative) → **aucune mutation**
    n'est commitée.

    Concepts: mur, hauteur, bulk, batch, plusieurs, masse, height
    Phrases: "monte tous ces murs à 3 m", "uniformise les hauteurs",
             "bulk set wall heights", "ajuste la hauteur de ces murs"
    Similar: walls_set_height, walls_move_many, openings_set_sill_height_many

    Args:
        items: liste de specs `{llm_id: str, height_m: float}`. Au moins
            un item, chaque `llm_id` doit pointer sur un Wall vivant.

    Returns:
        Réponse compacte (`_helpers.bulk_setter_summary`) :
        `{ok, count, drifted_count, drifts: [{llm_id, note}, ...],
          revit_modified}`. Seuls les murs où Revit a refusé / décalé la
        hauteur (Top Constraint, contrainte de niveau) apparaissent dans
        `drifts` — les autres ont été commités tels que demandés.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [_validate_set_height_item(kg, it, i) for i, it in enumerate(items)]

    if doc is None:
        for spec in specs:
            kg.modify_node(spec["llm_id"], {"height": spec["height_m"]})
        return bulk_setter_summary([], count=len(specs), revit_modified=False)

    # Binding pre-checks before any Revit import : fail fast on missing
    # _revit_id rather than crash inside the Tx.
    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["llm_id"]) is None:
            raise ValueError(
                "items[{}]: Wall {} has no Revit binding — run Refresh KG.".format(
                    i, spec["llm_id"],
                )
            )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    drifts: List[Dict[str, Any]] = []
    with rp.transaction(doc, "walls.set_height_many"):
        for spec in specs:
            eid = ElementId(kg.get_revit_id(spec["llm_id"]))
            wall = doc.GetElement(eid)
            param = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)
            if param is None:
                raise ValueError(
                    "Wall {} has no WALL_USER_HEIGHT_PARAM — type may not be a "
                    "Basic Wall (curtain walls don't expose this parameter).".format(
                        spec["llm_id"],
                    )
                )
            ok = param.Set(rp.meters_to_internal(spec["height_m"]))
            if not ok:
                raise ValueError(
                    "Setting WALL_USER_HEIGHT_PARAM on {} returned False — "
                    "parameter may be read-only (Top Constraint).".format(
                        spec["llm_id"],
                    )
                )
            kg_sync.refresh_node_from_revit(kg, doc, spec["llm_id"])
            actual = kg.get_node(spec["llm_id"]).get("height", spec["height_m"])
            drift, drift_note = kg_sync.detect_drift(
                spec["height_m"], actual, field="height_m",
            )
            if drift:
                drifts.append({
                    "llm_id": spec["llm_id"],
                    "note": (
                        (drift_note or "")
                        + " (probable Top Constraint)"
                    ).strip(),
                })

    return bulk_setter_summary(drifts, count=len(specs), revit_modified=True)


def _validate_move_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> Dict[str, Any]:
    """Preflight one item from `walls_move_many` : `{llm_id, dx, dy}`."""
    if not isinstance(item, dict):
        raise ValueError(
            "items[{}] must be a dict, got {}".format(index, type(item).__name__)
        )
    llm_id = item.get("llm_id")
    dx = item.get("dx")
    dy = item.get("dy")
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Wall":
        raise ValueError(
            "items[{}]: {} is a {}, not a Wall".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is soft-deleted".format(index, llm_id)
        )
    if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
        raise ValueError(
            "items[{}]: dx and dy must be numeric".format(index)
        )
    return {"llm_id": llm_id, "dx": float(dx), "dy": float(dy)}


@tool(name="walls_move_many", tier=1)
def move_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Translate N murs (chacun de son `(dx, dy)` propre) en **une seule**
    Tx Revit + une seule Tx KG.

    Cas typique : décaler tous les murs d'une trame de 50 mm, ajuster
    une rangée de murs à une nouvelle ligne d'alignement. Préférer ce
    tool à N appels séparés. Transactionnel.

    Concepts: mur, déplacement, translation, bulk, batch, move, masse
    Phrases: "déplace ces murs", "décale cette trame", "translate walls",
             "bulk move"
    Similar: walls_move, walls_set_height_many, elements_translate

    Args:
        items: liste de specs `{llm_id: str, dx: float, dy: float}`. Au
            moins un item. Les déplacements sont *par item* — pour un
            déplacement uniforme sur N murs, c'est plus compact d'appeler
            `elements_translate` avec la liste de llm_ids.

    Returns:
        Réponse compacte (`_helpers.bulk_setter_summary`). Drifts
        signalent les murs dont Revit a snappé / refusé le déplacement
        (alignement verrouillé, contrainte d'extrémité, etc.).
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [_validate_move_item(kg, it, i) for i, it in enumerate(items)]

    if doc is None:
        for spec in specs:
            node = kg.get_node(spec["llm_id"])
            new_p1 = [node["p1"][0] + spec["dx"], node["p1"][1] + spec["dy"]]
            new_p2 = [node["p2"][0] + spec["dx"], node["p2"][1] + spec["dy"]]
            kg.modify_node(spec["llm_id"], {"p1": new_p1, "p2": new_p2})
        return bulk_setter_summary([], count=len(specs), revit_modified=False)

    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["llm_id"]) is None:
            raise ValueError(
                "items[{}]: Wall {} has no Revit binding — run Refresh KG.".format(
                    i, spec["llm_id"],
                )
            )

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, ElementTransformUtils, XYZ

    drifts: List[Dict[str, Any]] = []
    with rp.transaction(doc, "walls.move_many"):
        for spec in specs:
            node = kg.get_node(spec["llm_id"])
            requested_p1 = [
                node["p1"][0] + spec["dx"],
                node["p1"][1] + spec["dy"],
            ]
            requested_p2 = [
                node["p2"][0] + spec["dx"],
                node["p2"][1] + spec["dy"],
            ]
            eid = ElementId(kg.get_revit_id(spec["llm_id"]))
            translation = XYZ(
                rp.meters_to_internal(spec["dx"]),
                rp.meters_to_internal(spec["dy"]),
                0.0,
            )
            ElementTransformUtils.MoveElement(doc, eid, translation)
            kg_sync.refresh_node_from_revit(kg, doc, spec["llm_id"])
            refreshed = kg.get_node(spec["llm_id"])
            drift_p1, note_p1 = kg_sync.detect_drift(
                requested_p1, refreshed["p1"], field="p1",
            )
            drift_p2, note_p2 = kg_sync.detect_drift(
                requested_p2, refreshed["p2"], field="p2",
            )
            if drift_p1 or drift_p2:
                drifts.append({
                    "llm_id": spec["llm_id"],
                    "note": note_p1 or note_p2,
                })

    return bulk_setter_summary(drifts, count=len(specs), revit_modified=True)


def _validate_delete_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> str:
    """Preflight one item from `walls_delete_many` : `{llm_id}` (string ou dict).
    Tolère les deux formats : `{llm_id: "..."}` ou directement `"..."` —
    le LLM oscille entre les deux selon le contexte. Retourne le llm_id
    après validation."""
    if isinstance(item, dict):
        llm_id = item.get("llm_id")
    else:
        llm_id = item
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Wall":
        raise ValueError(
            "items[{}]: {} is a {}, not a Wall".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is already soft-deleted".format(index, llm_id)
        )
    return llm_id


@tool(name="walls_delete_many", tier=1)
def delete_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Any],
) -> Dict[str, Any]:
    """Supprime N murs en **une seule** Tx Revit + une seule Tx KG.

    Tolère les ElementId périmés : si Revit n'a plus l'élément (orphelin
    KG), on soft-delete le KG seul et on signale `revit_already_gone` dans
    le payload. Évite le crash NoneType.

    Concepts: mur, suppression, bulk, batch, plusieurs, masse, delete
    Phrases: "supprime tous ces murs", "delete walls", "vire ces murs",
             "remove walls"
    Similar: walls_delete, walls_set_height_many

    Args:
        items: liste de llm_ids ou de specs `{"llm_id": str}`. Tolérant
            aux deux formats.

    Returns:
        {"ok": bool, "count": int, "deleted_revit": int,
         "deleted_kg_only": int, "revit_already_gone": [llm_id, …],
         "deleted_at_turn": int}
        `deleted_kg_only` compte les soft-deletes du KG sans Revit
        correspondant (orphelins purifiés).
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
    with rp.transaction(doc, "walls.delete_many"):
        for nid in llm_ids:
            eid_raw = kg.get_revit_id(nid)
            if eid_raw is None:
                # Pas de binding — soft-delete KG seulement.
                kg.soft_delete(nid)
                revit_already_gone.append(nid)
                continue
            element = doc.GetElement(ElementId(eid_raw))
            if element is None:
                # Binding périmé — soft-delete KG seulement (pas de crash).
                kg.soft_delete(nid)
                revit_already_gone.append(nid)
                continue
            try:
                doc.Delete(ElementId(eid_raw))
                kg.soft_delete(nid)
                deleted_revit += 1
            except Exception:  # noqa: BLE001 — Revit refuse parfois (relations).
                # On laisse le KG soft-deleter quand même mais on signale.
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


# ----- DXF custom WallType creation (Phase 2 étape 3) ------------------
#
# Phase 2 import : pour chaque épaisseur observée dans le plan DXF, on
# crée (ou réutilise) un WallType nommé `DXF_WALL_<cm>cm`. Pas de
# matching avec les types existants du template Revit — l'user refinera
# après import (cf. mémoire `project-phase2-custom-types`).
#
# Convention naming : `DXF_WALL_<cm>cm` (cm bucketed au cm près par
# défaut, paramétrable). Recognizable + filtrable au post-traitement.


_DXF_WALL_TYPE_PREFIX = "DXF_WALL_"


def _dxf_wall_type_name(thickness_m: float, bucket_cm: int = 1) -> str:
    """Construit le nom canonique `DXF_WALL_<cm>cm` pour une épaisseur.

    `bucket_cm` : granularité du bucketing en cm (défaut 1 — i.e. arrondi
    au cm près). Mettre 5 pour bucketer aux 5 cm.
    """
    if bucket_cm < 1:
        raise ValueError("bucket_cm must be >= 1")
    cm = int(round(thickness_m * 100 / bucket_cm)) * bucket_cm
    return "{}{}cm".format(_DXF_WALL_TYPE_PREFIX, cm)


def _find_dxf_wall_type_in_kg(
    kg: ProjectKG, target_name: str,
) -> Optional[str]:
    """Cherche un WallType vivant nommé `target_name` dans le KG.
    Retourne son llm_id ou None.
    """
    for nid in kg.find_by_type("WallType"):
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is not None:
            continue
        if node.get("name") == target_name:
            return nid
    return None


def _create_dxf_wall_type_kg_only(
    kg: ProjectKG, target_name: str, thickness_m: float,
) -> str:
    """Crée le node WallType (KG-only path, doc=None)."""
    return kg.add_node("WallType", {
        "name": target_name,
        "total_thickness": float(thickness_m),
    })


def _find_simple_basic_wall_type(doc: Any) -> Any:
    """Trouve un WallType template pour duplication.

    Préférence : WallKind.Basic avec exactement 1 layer (plus prévisible
    à ajuster via SetLayerWidth). Fallback : 1er WallKind.Basic trouvé.
    None si aucun BasicWall dispo (cas extrême — projet sans wall types
    de base, à corriger côté template Revit).
    """
    from . import _helpers  # noqa: F401  (parent module loaded)
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import WallKind

    fallback_basic = None
    for wt in rp.wall_types(doc):
        try:
            if wt.Kind != WallKind.Basic:
                continue
        except Exception:  # noqa: BLE001
            continue
        if fallback_basic is None:
            fallback_basic = wt
        try:
            cs = wt.GetCompoundStructure()
        except Exception:  # noqa: BLE001
            continue
        if cs is None:
            continue
        if cs.LayerCount == 1:
            return wt
    return fallback_basic


def _create_dxf_wall_type_in_revit(
    doc: Any, base_wt: Any, target_name: str, thickness_m: float,
) -> int:
    """Duplique `base_wt`, renomme `target_name`, ajuste la layer principale.

    **Doit être appelé à l'intérieur d'une `rp.transaction`** — pas de
    gestion de transaction ici. Retourne le `Id.Value` du nouveau type.

    Stratégie d'ajustement :
    - Si 1 layer : ajuste sa width directement.
    - Si N layers : ajuste la layer Core (BoundaryLayerType.Core).
      Si pas de Core layer, ajuste la layer d'index `StructuralMaterialIndex`.
    - Fallback : ajuste la layer 0.
    """
    from .. import revit_primitives as rp

    new_wt = base_wt.Duplicate(target_name)
    cs = new_wt.GetCompoundStructure()
    new_width_ft = rp.meters_to_internal(thickness_m)

    if cs is not None and cs.LayerCount > 0:
        layer_idx = 0
        if cs.LayerCount > 1:
            # Cherche la layer Structural (cœur du mur).
            struct_idx = -1
            try:
                struct_idx = int(cs.StructuralMaterialIndex)
            except Exception:  # noqa: BLE001
                struct_idx = -1
            if struct_idx >= 0:
                layer_idx = struct_idx
            else:
                # Cherche par fonction (Structure).
                try:
                    from Autodesk.Revit.DB import MaterialFunctionAssignment
                    for i in range(cs.LayerCount):
                        if cs.GetLayerFunction(i) == MaterialFunctionAssignment.Structure:
                            layer_idx = i
                            break
                except Exception:  # noqa: BLE001
                    pass

            # Pour les types multi-layer, on doit ajuster pour que la
            # somme des widths matche `new_width_ft`. Stratégie simple :
            # mettre toutes les autres layers à un minimum et la Core/
            # Structure layer à `new_width - sum(autres)`.
            other_total_ft = 0.0
            for i in range(cs.LayerCount):
                if i == layer_idx:
                    continue
                other_total_ft += float(cs.GetLayerWidth(i))
            target_struct_ft = max(new_width_ft - other_total_ft, rp.meters_to_internal(0.01))
            cs.SetLayerWidth(layer_idx, target_struct_ft)
        else:
            cs.SetLayerWidth(0, new_width_ft)
        new_wt.SetCompoundStructure(cs)

    return int(new_wt.Id.Value)


@tool(name="walls_get_or_create_dxf_type", tier=1)
def get_or_create_dxf_type(
    kg: ProjectKG,
    doc: Any,
    thickness_m: float,
    bucket_cm: int = 1,
    base_type_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Cherche ou crée un WallType custom `DXF_WALL_<cm>cm` (Phase 2 étape 3).

    Use case : import DXF avec épaisseurs variables — chaque épaisseur
    unique observée dans le plan se mappe à un WallType custom dédié,
    nommé selon la convention `DXF_WALL_<cm>cm` (cm bucketé au cm près
    par défaut). Si le type existe déjà (re-import ou type déjà créé
    précédemment), il est retourné tel quel (idempotent).

    **Pas de matching avec les types existants** du template Revit
    (cf. mémoire `project-phase2-custom-types`) : on crée toujours un
    type DXF dédié pour rester traçable. L'user refinera après import
    en remappant les murs vers ses propres types.

    Concepts: mur, type, walltype, dxf, import, phase 2, épaisseur,
              thickness, custom, generic
    Phrases: "crée le type DXF pour épaisseur X", "get or create wall type",
             "type custom pour ce mur dxf"
    Similar: walls_get_or_create_dxf_type_many, walls_create_many,
             dwg_import_walls_typed

    Args:
        thickness_m: épaisseur en mètres.
        bucket_cm: granularité du bucketing (défaut 1). Le nom et la
            valeur stockée sont arrondis à `bucket_cm` près.
        base_type_ref: llm_id d'un WallType template à dupliquer en
            Revit. Si None, le tool cherche automatiquement un BasicWall
            simple (1 layer si possible).

    Returns:
        {"ok": bool, "llm_id": str, "name": str, "thickness_m": float,
         "created": bool, "revit_id": int | None}
        `created=False` si le type existait déjà.
    """
    if not isinstance(thickness_m, (int, float)) or thickness_m <= 0:
        raise ValueError("thickness_m must be a positive number")
    bucketed_cm = int(round(thickness_m * 100 / bucket_cm)) * bucket_cm
    bucketed_m = bucketed_cm / 100.0
    target_name = _dxf_wall_type_name(thickness_m, bucket_cm=bucket_cm)

    existing = _find_dxf_wall_type_in_kg(kg, target_name)
    if existing is not None:
        node = kg.get_node(existing)
        return {
            "ok": True,
            "llm_id": existing,
            "name": target_name,
            "thickness_m": float(node.get("total_thickness", bucketed_m)),
            "created": False,
            "revit_id": kg.get_revit_id(existing),
        }

    if doc is None:
        llm_id = _create_dxf_wall_type_kg_only(kg, target_name, bucketed_m)
        return {
            "ok": True,
            "llm_id": llm_id,
            "name": target_name,
            "thickness_m": bucketed_m,
            "created": True,
            "revit_id": None,
        }

    # Revit-backed path.
    if base_type_ref is not None:
        if not kg.has_node(base_type_ref):
            raise ValueError("Unknown base_type_ref: {}".format(base_type_ref))
        base_eid_raw = kg.get_revit_id(base_type_ref)
        if base_eid_raw is None:
            raise ValueError(
                "base_type_ref {} has no Revit binding".format(base_type_ref)
            )
        from .. import revit_primitives as rp
        from Autodesk.Revit.DB import ElementId
        base_wt = doc.GetElement(ElementId(base_eid_raw))
    else:
        base_wt = _find_simple_basic_wall_type(doc)
        if base_wt is None:
            raise ValueError(
                "No BasicWall template available in this project. "
                "Provide `base_type_ref` explicitly or add a simple "
                "WallType to your template."
            )

    from .. import revit_primitives as rp
    with rp.transaction(doc, "walls.get_or_create_dxf_type"):
        revit_id = _create_dxf_wall_type_in_revit(
            doc, base_wt, target_name, bucketed_m,
        )
        llm_id = _create_dxf_wall_type_kg_only(kg, target_name, bucketed_m)
        kg.set_revit_id(llm_id, revit_id)

    return {
        "ok": True,
        "llm_id": llm_id,
        "name": target_name,
        "thickness_m": bucketed_m,
        "created": True,
        "revit_id": revit_id,
    }


@tool(name="walls_get_or_create_dxf_type_many", tier=1)
def get_or_create_dxf_type_many(
    kg: ProjectKG,
    doc: Any,
    thicknesses_m: List[float],
    bucket_cm: int = 1,
    base_type_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Crée (ou réutilise) N WallType custom `DXF_WALL_<cm>cm` en **une
    seule** Tx Revit.

    Pattern bulk : pour `dwg_import_walls_typed`, on a typiquement 2-5
    épaisseurs uniques à traiter. Bulk = 1 Tx, 1 round-trip API,
    économie tokens. Dédup interne sur les buckets — appeler avec
    `[0.20, 0.205, 0.21]` et `bucket_cm=1` crée 1 type `DXF_WALL_20cm`
    (les 3 buckets convergent à 20cm).

    Concepts: mur, type, walltype, bulk, batch, dxf, import, plusieurs,
              épaisseurs
    Phrases: "crée tous les types DXF nécessaires", "bulk wall types"
    Similar: walls_get_or_create_dxf_type, walls_create_many,
             dwg_import_walls_typed

    Args:
        thicknesses_m: liste d'épaisseurs en mètres (peut contenir
            des doublons après bucketing).
        bucket_cm: granularité du bucketing (défaut 1).
        base_type_ref: voir `walls_get_or_create_dxf_type`.

    Returns:
        {"ok": bool, "types": [{thickness_m, name, llm_id, created,
            revit_id}, ...], "created_count": int, "reused_count": int}
    """
    if not isinstance(thicknesses_m, list) or not thicknesses_m:
        raise ValueError("thicknesses_m must be a non-empty list")
    for i, t in enumerate(thicknesses_m):
        if not isinstance(t, (int, float)) or t <= 0:
            raise ValueError(
                "thicknesses_m[{}]: must be positive number, got {!r}".format(i, t)
            )

    # Dédup par bucket.
    unique_buckets_m: List[float] = []
    seen_names: Set[str] = set()
    for t in thicknesses_m:
        name = _dxf_wall_type_name(t, bucket_cm=bucket_cm)
        if name in seen_names:
            continue
        seen_names.add(name)
        bucketed_cm = int(round(t * 100 / bucket_cm)) * bucket_cm
        unique_buckets_m.append(bucketed_cm / 100.0)

    types_payload: List[Dict[str, Any]] = []
    created_count = 0
    reused_count = 0

    if doc is None:
        for tm in unique_buckets_m:
            name = _dxf_wall_type_name(tm, bucket_cm=bucket_cm)
            existing = _find_dxf_wall_type_in_kg(kg, name)
            if existing is not None:
                node = kg.get_node(existing)
                types_payload.append({
                    "thickness_m": float(node.get("total_thickness", tm)),
                    "name": name,
                    "llm_id": existing,
                    "created": False,
                    "revit_id": None,
                })
                reused_count += 1
                continue
            nid = _create_dxf_wall_type_kg_only(kg, name, tm)
            types_payload.append({
                "thickness_m": tm,
                "name": name,
                "llm_id": nid,
                "created": True,
                "revit_id": None,
            })
            created_count += 1
        return {
            "ok": True,
            "types": types_payload,
            "created_count": created_count,
            "reused_count": reused_count,
        }

    # Revit-backed path : 1 Tx pour tous les nouveaux types.
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    # Pre-validate base wall type.
    if base_type_ref is not None:
        if not kg.has_node(base_type_ref):
            raise ValueError("Unknown base_type_ref: {}".format(base_type_ref))
        base_eid_raw = kg.get_revit_id(base_type_ref)
        if base_eid_raw is None:
            raise ValueError(
                "base_type_ref {} has no Revit binding".format(base_type_ref)
            )
        base_wt = doc.GetElement(ElementId(base_eid_raw))
    else:
        base_wt = _find_simple_basic_wall_type(doc)
        if base_wt is None:
            raise ValueError(
                "No BasicWall template available in this project. "
                "Provide `base_type_ref` explicitly or add a simple "
                "WallType to your template."
            )

    with rp.transaction(doc, "walls.get_or_create_dxf_type_many"):
        for tm in unique_buckets_m:
            name = _dxf_wall_type_name(tm, bucket_cm=bucket_cm)
            existing = _find_dxf_wall_type_in_kg(kg, name)
            if existing is not None:
                node = kg.get_node(existing)
                types_payload.append({
                    "thickness_m": float(node.get("total_thickness", tm)),
                    "name": name,
                    "llm_id": existing,
                    "created": False,
                    "revit_id": kg.get_revit_id(existing),
                })
                reused_count += 1
                continue
            revit_id = _create_dxf_wall_type_in_revit(doc, base_wt, name, tm)
            nid = _create_dxf_wall_type_kg_only(kg, name, tm)
            kg.set_revit_id(nid, revit_id)
            types_payload.append({
                "thickness_m": tm,
                "name": name,
                "llm_id": nid,
                "created": True,
                "revit_id": revit_id,
            })
            created_count += 1

    return {
        "ok": True,
        "types": types_payload,
        "created_count": created_count,
        "reused_count": reused_count,
    }
