"""tools/floors.py — création, suppression, batch des sols / dalles.

Pattern doc-aware standard (cf. `walls.py` / `rooms.py`) : si `doc is None`
(CLI / pytest), seule la mutation KG est appliquée et `revit_id: None` est
retourné. Sinon, branche Revit ouvre une `revit_primitives.transaction`
qui enveloppe `Floor.Create` + mutations KG + `stamp_llm_id`. Le
`kg.transaction()` externe (ouvert par le dispatcher) fournit la
rollback symétrique.

**Boundary**. Le contour du sol est `[[x, y], …]` (mètres) — polygone
fermé (le 1er sommet est implicitement reconnecté au dernier). Côté
Revit, un `CurveLoop` est assemblé via `Line.CreateBound` entre paires
de sommets successifs. Validation préalable : ≥ 3 sommets distincts,
pas de doublon adjacent.

**Aire**. Calculée par formule shoelace côté KG (V0, déterministe). Côté
Revit, on relit `HOST_AREA_COMPUTED` après création (read-back
discipline) — la valeur fait foi en cas de drift (Revit peut ajuster le
contour si une grid lock ou un alignement le contraint).

**Pas de move / set_thickness en V0**. `ElementTransformUtils.MoveElement`
marche sur Floor mais la `refresh_node_from_revit` doit ré-extraire la
géométrie via les CurveLoops du Floor — pas encore outillé. Idem pour
changer l'épaisseur (passe par le FloorType, pas l'instance). À
implémenter quand le runtime le demande.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._helpers import bulk_summary, stamp_llm_id
from ..llm_protocol import tool
from ..project_kg import ProjectKG


# ----- Geometry helpers --------------------------------------------------


def _shoelace_area(boundary: List[List[float]]) -> float:
    """Aire signée du polygone (mètres²) par formule de Gauss.

    Retourne l'absolu (orientation indifférente : Revit gère l'orientation
    lui-même via le sens des CurveLoops). Le polygone est traité comme
    fermé même sans répétition explicite du 1er point en fin.
    """
    n = len(boundary)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = boundary[i][0], boundary[i][1]
        x2, y2 = boundary[(i + 1) % n][0], boundary[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _validate_boundary(boundary: Any, item_idx: Optional[int] = None) -> List[List[float]]:
    """Validate + normalise: list of [x, y] pairs in m.

    Drops a final duplicate of the 1st point if present (polygons fermés
    en source mais Revit veut un CurveLoop ouvert). Refuse doublons
    adjacents (zero-length segment = Revit InvalidOperationException).
    """
    prefix = "items[{}]: ".format(item_idx) if item_idx is not None else ""
    if not isinstance(boundary, list):
        raise ValueError(prefix + "boundary must be a list of [x, y] points")
    cleaned: List[List[float]] = []
    for i, pt in enumerate(boundary):
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            raise ValueError(
                prefix + "boundary[{}] must be [x, y] in m".format(i)
            )
        cleaned.append([float(pt[0]), float(pt[1])])
    # Auto-strip trailing duplicate.
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1]:
        cleaned = cleaned[:-1]
    if len(cleaned) < 3:
        raise ValueError(
            prefix + "boundary must have ≥ 3 distinct points "
            "(got {})".format(len(cleaned))
        )
    # No adjacent duplicates.
    for i in range(len(cleaned)):
        a = cleaned[i]
        b = cleaned[(i + 1) % len(cleaned)]
        if a[0] == b[0] and a[1] == b[1]:
            raise ValueError(
                prefix + "boundary has zero-length segment "
                "at index {} → {}".format(i, (i + 1) % len(cleaned))
            )
    return cleaned


def _record_in_kg(
    kg: ProjectKG,
    *,
    level_ref: str,
    floor_type_ref: str,
    boundary: List[List[float]],
    area_m2: float,
) -> str:
    """KG-side creation. Returns the new floor's llm_id."""
    llm_id = kg.add_node("Floor", {
        "type_ref": floor_type_ref,
        "level_ref": level_ref,
        "boundary": [list(p) for p in boundary],
        "area_m2": float(area_m2),
    })
    kg.add_edge(llm_id, level_ref, "at_level")
    kg.add_edge(llm_id, floor_type_ref, "is_type")
    return llm_id


def _require_live_floor(kg: ProjectKG, llm_id: str) -> Dict[str, Any]:
    """Preflight: node exists, is a Floor, not soft-deleted. Returns attrs."""
    if not kg.has_node(llm_id):
        raise ValueError("Unknown llm_id: {}".format(llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") != "Floor":
        raise ValueError(
            "llm_id {} is a {}, not a Floor".format(llm_id, node.get("_type"))
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError("Floor {} is already soft-deleted".format(llm_id))
    return node


def _validate_floor_item(kg: ProjectKG, item: Any, idx: int) -> Dict[str, Any]:
    """Validate one bulk item. Returns the normalised spec dict."""
    if not isinstance(item, dict):
        raise ValueError(
            "items[{}] must be a dict, got {}".format(idx, type(item).__name__)
        )
    level_ref = item.get("level_ref")
    floor_type_ref = item.get("floor_type_ref")
    boundary = item.get("boundary")
    if not level_ref or not isinstance(level_ref, str):
        raise ValueError("items[{}]: level_ref required (str)".format(idx))
    if not floor_type_ref or not isinstance(floor_type_ref, str):
        raise ValueError("items[{}]: floor_type_ref required (str)".format(idx))
    if not kg.has_node(level_ref):
        raise ValueError(
            "items[{}]: unknown level_ref {}".format(idx, level_ref)
        )
    if not kg.has_node(floor_type_ref):
        raise ValueError(
            "items[{}]: unknown floor_type_ref {}".format(idx, floor_type_ref)
        )
    normalised_boundary = _validate_boundary(boundary, item_idx=idx)
    return {
        "level_ref": level_ref,
        "floor_type_ref": floor_type_ref,
        "boundary": normalised_boundary,
    }


def _build_curve_loop(boundary: List[List[float]]) -> Any:
    """Lazy-import + assemble un Revit CurveLoop depuis le polygone.

    Hors-Revit, ImportError remontera — appelé uniquement dans la branche
    `doc is not None`.
    """
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import CurveLoop, Line, XYZ

    loop = CurveLoop()
    n = len(boundary)
    for i in range(n):
        a = boundary[i]
        b = boundary[(i + 1) % n]
        p1 = XYZ(rp.meters_to_internal(a[0]), rp.meters_to_internal(a[1]), 0.0)
        p2 = XYZ(rp.meters_to_internal(b[0]), rp.meters_to_internal(b[1]), 0.0)
        loop.Append(Line.CreateBound(p1, p2))
    return loop


# ----- floors_create -----------------------------------------------------


@tool(name="floors_create", tier=1)
def create(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    floor_type_ref: str,
    boundary: List[List[float]],
) -> Dict[str, Any]:
    """Crée un sol / une dalle (Floor) au niveau donné, sur le contour fourni.

    `boundary` est une polyligne fermée `[[x, y], …]` en mètres dans le
    plan du niveau. L'orientation (CW / CCW) n'a pas d'importance —
    Revit ré-oriente le CurveLoop si besoin. Un doublon en fin de liste
    (premier point répété) est toléré et stripé.

    Côté Revit, le sol est créé via `Floor.Create(doc, [CurveLoop],
    floor_type_id, level_id)` (API 2023+, disponible en 2025). Aucun
    offset n'est posé : la face supérieure du floor s'aligne sur le
    `level_ref` (convention par défaut Revit).

    Concepts: sol, dalle, floor, slab, plancher, dallage, contour
    Phrases: "crée un sol", "ajoute une dalle", "plancher",
             "place a floor", "create a slab", "dalle de la pièce X"
    Similar: floors_create_many, floors_delete, catalog_list_floor_types

    Args:
        level_ref: llm_id du Level cible (obtenu via `catalog_list_levels`).
        floor_type_ref: llm_id du FloorType (via `catalog_list_floor_types`).
        boundary: liste de `[x, y]` en mètres, ≥ 3 sommets distincts.
            Polygone fermé implicite — pas besoin de répéter le 1er
            point en fin.

    Returns:
        {"ok": bool, "llm_id": str, "area_m2": float, "revit_id": int | None}
    """
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if not kg.has_node(floor_type_ref):
        raise ValueError("Unknown floor_type_ref: {}".format(floor_type_ref))

    normalised_boundary = _validate_boundary(boundary)
    area_m2 = _shoelace_area(normalised_boundary)

    if doc is None:
        llm_id = _record_in_kg(
            kg,
            level_ref=level_ref,
            floor_type_ref=floor_type_ref,
            boundary=normalised_boundary,
            area_m2=area_m2,
        )
        return {
            "ok": True,
            "llm_id": llm_id,
            "area_m2": round(area_m2, 3),
            "revit_id": None,
        }

    # Revit-backed path.
    level_eid_raw = kg.get_revit_id(level_ref)
    ft_eid_raw = kg.get_revit_id(floor_type_ref)
    if level_eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(level_ref)
        )
    if ft_eid_raw is None:
        raise ValueError(
            "FloorType {} has no Revit binding — run Refresh KG.".format(floor_type_ref)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, Floor
    from System.Collections.Generic import List as ClrList
    from Autodesk.Revit.DB import CurveLoop

    level_eid = ElementId(level_eid_raw)
    ft_eid = ElementId(ft_eid_raw)
    loop = _build_curve_loop(normalised_boundary)
    loops = ClrList[CurveLoop]()
    loops.Add(loop)

    revit_id: Optional[int] = None
    with rp.transaction(doc, "floors.create"):
        floor = Floor.Create(doc, loops, ft_eid, level_eid)
        revit_id = int(floor.Id.Value)
        llm_id = _record_in_kg(
            kg,
            level_ref=level_ref,
            floor_type_ref=floor_type_ref,
            boundary=normalised_boundary,
            area_m2=area_m2,
        )
        kg.set_revit_id(llm_id, revit_id)
        stamp_llm_id(floor, llm_id)
        # Read-back discipline (2026-05-11 session 5) : Revit computes
        # HOST_AREA_COMPUTED on the actual placed loops — mirror that.
        from .. import kg_sync as _kg_sync
        _kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual = kg.get_node(llm_id)
    return {
        "ok": True,
        "llm_id": llm_id,
        "area_m2": round(actual.get("area_m2", area_m2), 3),
        "boundary": actual.get("boundary"),
        "revit_id": revit_id,
    }


# ----- floors_create_many ------------------------------------------------


@tool(name="floors_create_many", tier=1)
def create_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Crée N sols en **une seule** transaction Revit + une seule transaction
    KG. Préférer ce tool dès qu'on a plusieurs sols à créer (typiquement
    un sol par étage en début de projet).

    Transactionnel : si un item échoue, **aucune création** n'est commitée.

    Concepts: sol, dalle, floor, batch, plusieurs, série
    Phrases: "crée tous les sols", "batch floors", "dalles de chaque étage"
    Similar: floors_create, walls_create_many

    Args:
        items: liste de specs. Chaque entrée :
            - `level_ref` (str, requis)
            - `floor_type_ref` (str, requis)
            - `boundary` (list[[x,y]], requis) — ≥ 3 sommets distincts.

    Returns:
        Réponse compacte (`bulk_summary`). Détails par item via
        `catalog_list_floors`.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")

    specs = [_validate_floor_item(kg, item, i) for i, item in enumerate(items)]

    if doc is None:
        llm_ids: List[str] = []
        for spec in specs:
            area = _shoelace_area(spec["boundary"])
            llm_ids.append(_record_in_kg(
                kg,
                level_ref=spec["level_ref"],
                floor_type_ref=spec["floor_type_ref"],
                boundary=spec["boundary"],
                area_m2=area,
            ))
        return bulk_summary(llm_ids)

    # Revit path — bindings upfront.
    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["level_ref"]) is None:
            raise ValueError(
                "items[{}]: Level {} has no Revit binding — run Refresh KG.".format(
                    i, spec["level_ref"],
                )
            )
        if kg.get_revit_id(spec["floor_type_ref"]) is None:
            raise ValueError(
                "items[{}]: FloorType {} has no Revit binding — run Refresh KG.".format(
                    i, spec["floor_type_ref"],
                )
            )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import CurveLoop, ElementId, Floor
    from System.Collections.Generic import List as ClrList

    llm_ids: List[str] = []
    with rp.transaction(doc, "floors.create_many"):
        for spec in specs:
            level_eid = ElementId(kg.get_revit_id(spec["level_ref"]))
            ft_eid = ElementId(kg.get_revit_id(spec["floor_type_ref"]))
            loops = ClrList[CurveLoop]()
            loops.Add(_build_curve_loop(spec["boundary"]))
            floor = Floor.Create(doc, loops, ft_eid, level_eid)
            revit_id = int(floor.Id.Value)
            area = _shoelace_area(spec["boundary"])
            llm_id = _record_in_kg(
                kg,
                level_ref=spec["level_ref"],
                floor_type_ref=spec["floor_type_ref"],
                boundary=spec["boundary"],
                area_m2=area,
            )
            kg.set_revit_id(llm_id, revit_id)
            stamp_llm_id(floor, llm_id)
            llm_ids.append(llm_id)
        from .. import kg_sync as _kg_sync
        for nid in llm_ids:
            _kg_sync.refresh_node_from_revit(kg, doc, nid)

    return bulk_summary(llm_ids)


# ----- floors_delete -----------------------------------------------------


@tool(name="floors_delete", tier=1)
def delete(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
) -> Dict[str, Any]:
    """Supprime un sol du projet (Revit + KG soft delete).

    Concepts: sol, dalle, suppression, delete, supprime, floor
    Phrases: "supprime le sol", "delete the floor", "enlève la dalle"
    Similar: floors_create, floors_delete_many

    Args:
        llm_id: llm_id du sol à supprimer.

    Returns:
        {"ok": bool, "llm_id": str, "deleted_at_turn": int, "revit_deleted": bool}
    """
    _require_live_floor(kg, llm_id)

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
            "Floor {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "floors.delete"):
        doc.Delete(eid)
        kg.soft_delete(llm_id)

    return {
        "ok": True,
        "llm_id": llm_id,
        "deleted_at_turn": kg.turn,
        "revit_deleted": True,
    }


# ----- floors_delete_many ------------------------------------------------


@tool(name="floors_delete_many", tier=1)
def delete_many(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
) -> Dict[str, Any]:
    """Supprime N sols en **une seule** transaction Revit + KG.

    Transactionnel : si un id échoue, **aucune** suppression n'est commitée.

    Concepts: sol, dalle, suppression, batch, plusieurs, série
    Phrases: "supprime tous les sols", "delete these floors", "vire les dalles"
    Similar: floors_delete, floors_create_many

    Args:
        llm_ids: liste de llm_ids de sols à supprimer.

    Returns:
        {"ok", "count", "llm_ids"} compact via bulk_summary.
    """
    if not isinstance(llm_ids, list) or not llm_ids:
        raise ValueError("llm_ids must be a non-empty list")
    for lid in llm_ids:
        _require_live_floor(kg, lid)

    if doc is None:
        for lid in llm_ids:
            kg.soft_delete(lid)
        return bulk_summary(llm_ids)

    eids_raw: List[int] = []
    for lid in llm_ids:
        eid_raw = kg.get_revit_id(lid)
        if eid_raw is None:
            raise ValueError(
                "Floor {} has no Revit binding — run Refresh KG.".format(lid)
            )
        eids_raw.append(eid_raw)

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    with rp.transaction(doc, "floors.delete_many"):
        for lid, eid_raw in zip(llm_ids, eids_raw):
            doc.Delete(ElementId(eid_raw))
            kg.soft_delete(lid)

    return bulk_summary(llm_ids)
