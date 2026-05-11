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
from typing import Any, Dict, List, Optional

from ._helpers import bulk_summary, stamp_llm_id
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

    return {
        "ok": True,
        "llm_id": llm_id,
        "length_m": round(length, 3),
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
            "revit_moved": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Wall {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, ElementTransformUtils, XYZ

    translation = XYZ(rp.meters_to_internal(dx), rp.meters_to_internal(dy), 0.0)
    eid = ElementId(eid_raw)
    with rp.transaction(doc, "walls.move"):
        ElementTransformUtils.MoveElement(doc, eid, translation)
        kg.modify_node(llm_id, {"p1": new_p1, "p2": new_p2})

    return {
        "ok": True,
        "llm_id": llm_id,
        "p1_m": new_p1,
        "p2_m": new_p2,
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
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Wall {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
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
        kg.modify_node(llm_id, {"height": height_m})

    return {
        "ok": True,
        "llm_id": llm_id,
        "height_m": height_m,
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
