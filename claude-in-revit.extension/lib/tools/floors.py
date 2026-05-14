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

from typing import Any, Dict, List, Optional, Set

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


def _net_floor_area(
    outer: List[List[float]],
    holes: Optional[List[List[List[float]]]] = None,
) -> float:
    """Aire nette = outer_area − Σ hole_areas. Approximation : suppose que
    chaque hole est entièrement contenu dans l'outer (pas de débordement) ;
    Revit fail de toute façon à la création si ce n'est pas le cas.
    """
    a = _shoelace_area(outer)
    if holes:
        a -= sum(_shoelace_area(h) for h in holes)
    return max(a, 0.0)


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
    holes: Optional[List[List[List[float]]]] = None,
) -> str:
    """KG-side creation. Returns the new floor's llm_id.

    `holes` : optionnel, liste de polylignes fermées dans le plan. Stocké
    en attr optionnel si non-vide ; sinon omis (dalle pleine).
    """
    attrs: Dict[str, Any] = {
        "type_ref": floor_type_ref,
        "level_ref": level_ref,
        "boundary": [list(p) for p in boundary],
        "area_m2": float(area_m2),
    }
    if holes:
        attrs["holes"] = [[list(p) for p in h] for h in holes]
    llm_id = kg.add_node("Floor", attrs)
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
    holes_raw = item.get("holes") or []
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
    # holes : optional list of polylines (each closed, ≥ 3 vertices). Validés
    # via le même _validate_boundary que l'outer (mêmes contraintes).
    if not isinstance(holes_raw, list):
        raise ValueError("items[{}]: holes must be a list".format(idx))
    normalised_holes: List[List[List[float]]] = []
    for h_idx, h in enumerate(holes_raw):
        try:
            normalised_holes.append(_validate_boundary(h, item_idx=idx))
        except ValueError as exc:
            raise ValueError(
                "items[{}].holes[{}]: {}".format(idx, h_idx, exc)
            ) from exc
    return {
        "level_ref": level_ref,
        "floor_type_ref": floor_type_ref,
        "boundary": normalised_boundary,
        "holes": normalised_holes,
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
            area = _net_floor_area(spec["boundary"], spec.get("holes"))
            llm_ids.append(_record_in_kg(
                kg,
                level_ref=spec["level_ref"],
                floor_type_ref=spec["floor_type_ref"],
                boundary=spec["boundary"],
                area_m2=area,
                holes=spec.get("holes") or None,
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
            # Outer boundary en premier (convention Floor.Create), holes ensuite.
            loops.Add(_build_curve_loop(spec["boundary"]))
            for h in spec.get("holes") or []:
                loops.Add(_build_curve_loop(h))
            floor = Floor.Create(doc, loops, ft_eid, level_eid)
            revit_id = int(floor.Id.Value)
            area = _net_floor_area(spec["boundary"], spec.get("holes"))
            llm_id = _record_in_kg(
                kg,
                level_ref=spec["level_ref"],
                floor_type_ref=spec["floor_type_ref"],
                boundary=spec["boundary"],
                area_m2=area,
                holes=spec.get("holes") or None,
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


# ----- DXF custom FloorType creation (Phase 2c — sols) -----------------
#
# Parallèle de `walls_get_or_create_dxf_type[_many]` : pour chaque
# épaisseur observée dans les coupes du DXF, créer (ou réutiliser) un
# FloorType nommé `DXF_FLOOR_<cm>cm`. Pas de matching aux types
# existants — l'user remappe après import.


_DXF_FLOOR_TYPE_PREFIX = "DXF_FLOOR_"


def _dxf_floor_type_name(thickness_m: float, bucket_cm: int = 1) -> str:
    if bucket_cm < 1:
        raise ValueError("bucket_cm must be >= 1")
    cm = int(round(thickness_m * 100 / bucket_cm)) * bucket_cm
    return "{}{}cm".format(_DXF_FLOOR_TYPE_PREFIX, cm)


def _find_dxf_floor_type_in_kg(
    kg: ProjectKG, target_name: str,
) -> Optional[str]:
    for nid in kg.find_by_type("FloorType"):
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is not None:
            continue
        if node.get("name") == target_name:
            return nid
    return None


def _validate_or_drop_stale_floor_type_binding(
    kg: ProjectKG, doc: Any, llm_id: str,
) -> bool:
    """Mêmes garanties que `_validate_or_drop_stale_wall_type_binding`
    pour FloorType. Cf. runtime P7 session r."""
    if doc is None:
        return True
    revit_id_raw = kg.get_revit_id(llm_id)
    if revit_id_raw is None:
        kg.soft_delete(llm_id)
        return False
    from Autodesk.Revit.DB import ElementId, FloorType
    elem = doc.GetElement(ElementId(revit_id_raw))
    if elem is None:
        kg.soft_delete(llm_id)
        return False
    try:
        if not isinstance(elem, FloorType):
            kg.soft_delete(llm_id)
            return False
    except TypeError:
        try:
            cat_id = elem.Category.Id.Value
            if int(cat_id) != -2000032:  # OST_Floors category id
                kg.soft_delete(llm_id)
                return False
        except Exception:  # noqa: BLE001
            kg.soft_delete(llm_id)
            return False
    return True


def _find_simple_basic_floor_type(doc: Any) -> Any:
    """Trouve un FloorType template (1 layer si possible, sinon le 1er
    avec CompoundStructure non vide)."""
    from .. import revit_primitives as rp

    fallback = None
    for ft in rp.floor_types(doc):
        try:
            cs = ft.GetCompoundStructure()
        except Exception:  # noqa: BLE001
            continue
        if cs is None:
            continue
        if fallback is None:
            fallback = ft
        if cs.LayerCount == 1:
            return ft
    return fallback


def _create_dxf_floor_type_in_revit(
    doc: Any, base_ft: Any, target_name: str, thickness_m: float,
) -> int:
    """Duplique `base_ft`, renomme, ajuste sa layer principale.
    Doit être appelé à l'intérieur d'une `rp.transaction`."""
    from .. import revit_primitives as rp

    new_ft = base_ft.Duplicate(target_name)
    cs = new_ft.GetCompoundStructure()
    new_width_ft = rp.meters_to_internal(thickness_m)

    if cs is not None and cs.LayerCount > 0:
        layer_idx = 0
        if cs.LayerCount > 1:
            struct_idx = -1
            try:
                struct_idx = int(cs.StructuralMaterialIndex)
            except Exception:  # noqa: BLE001
                struct_idx = -1
            if struct_idx >= 0:
                layer_idx = struct_idx
            else:
                try:
                    from Autodesk.Revit.DB import MaterialFunctionAssignment
                    for i in range(cs.LayerCount):
                        if cs.GetLayerFunction(i) == MaterialFunctionAssignment.Structure:
                            layer_idx = i
                            break
                except Exception:  # noqa: BLE001
                    pass
            other_total_ft = 0.0
            for i in range(cs.LayerCount):
                if i == layer_idx:
                    continue
                other_total_ft += float(cs.GetLayerWidth(i))
            target_struct_ft = max(
                new_width_ft - other_total_ft, rp.meters_to_internal(0.01),
            )
            cs.SetLayerWidth(layer_idx, target_struct_ft)
        else:
            cs.SetLayerWidth(0, new_width_ft)
        new_ft.SetCompoundStructure(cs)

    return int(new_ft.Id.Value)


@tool(name="floors_get_or_create_dxf_type", tier=1)
def get_or_create_dxf_type(
    kg: ProjectKG,
    doc: Any,
    thickness_m: float,
    bucket_cm: int = 1,
    base_type_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Cherche ou crée un FloorType `DXF_FLOOR_<cm>cm` (Phase 2c).

    Parallèle de `walls_get_or_create_dxf_type`. Idempotent : si le type
    existe, le réutilise (avec validation Revit-binding). Sinon, duplique
    un BasicFloor du template et ajuste l'épaisseur de la layer Core.

    Concepts: sol, dalle, floor, type, floortype, dxf, import, phase 2,
              épaisseur, thickness, custom
    Phrases: "crée le type dxf de sol", "get or create floor type",
             "type de dalle pour l'import"
    Similar: floors_get_or_create_dxf_type_many, walls_get_or_create_dxf_type

    Args:
        thickness_m: épaisseur en mètres.
        bucket_cm: granularité du bucketing (défaut 1).
        base_type_ref: llm_id d'un FloorType template à dupliquer.
            Si None, recherche auto.

    Returns:
        {"ok", "llm_id", "name", "thickness_m", "created", "revit_id"}
    """
    if not isinstance(thickness_m, (int, float)) or thickness_m <= 0:
        raise ValueError("thickness_m must be a positive number")
    bucketed_cm = int(round(thickness_m * 100 / bucket_cm)) * bucket_cm
    bucketed_m = bucketed_cm / 100.0
    target_name = _dxf_floor_type_name(thickness_m, bucket_cm=bucket_cm)

    existing = _find_dxf_floor_type_in_kg(kg, target_name)
    if existing is not None:
        if _validate_or_drop_stale_floor_type_binding(kg, doc, existing):
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
        llm_id = kg.add_node("FloorType", {
            "name": target_name,
            "total_thickness": bucketed_m,
        })
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
        base_ft = doc.GetElement(ElementId(base_eid_raw))
    else:
        base_ft = _find_simple_basic_floor_type(doc)
        if base_ft is None:
            raise ValueError(
                "No FloorType template available. Provide `base_type_ref` "
                "or add a basic FloorType to your project template."
            )

    from .. import revit_primitives as rp
    with rp.transaction(doc, "floors.get_or_create_dxf_type"):
        revit_id = _create_dxf_floor_type_in_revit(
            doc, base_ft, target_name, bucketed_m,
        )
        llm_id = kg.add_node("FloorType", {
            "name": target_name,
            "total_thickness": bucketed_m,
        })
        kg.set_revit_id(llm_id, revit_id)

    return {
        "ok": True,
        "llm_id": llm_id,
        "name": target_name,
        "thickness_m": bucketed_m,
        "created": True,
        "revit_id": revit_id,
    }


@tool(name="floors_get_or_create_dxf_type_many", tier=1)
def get_or_create_dxf_type_many(
    kg: ProjectKG,
    doc: Any,
    thicknesses_m: List[float],
    bucket_cm: int = 1,
    base_type_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Variante bulk de `floors_get_or_create_dxf_type` (1 seule Tx Revit
    pour N épaisseurs). Réutilise et déduplique par bucket.

    Concepts: sol, dalle, floor, type, dxf, import, phase 2, bulk, many
    Phrases: "crée les types de dalle dxf", "get or create floor types many"
    Similar: floors_get_or_create_dxf_type, walls_get_or_create_dxf_type_many

    Returns:
        {"ok", "types": [{llm_id, name, thickness_m, created, revit_id}, ...],
         "created_count", "reused_count"}
    """
    if not isinstance(thicknesses_m, list) or not thicknesses_m:
        raise ValueError("thicknesses_m must be a non-empty list")

    # Dédup par bucket.
    seen_cm: Set[int] = set()
    unique: List[float] = []
    for t in thicknesses_m:
        if not isinstance(t, (int, float)) or t <= 0:
            raise ValueError("thicknesses_m must contain positive numbers")
        cm = int(round(float(t) * 100 / bucket_cm)) * bucket_cm
        if cm in seen_cm:
            continue
        seen_cm.add(cm)
        unique.append(cm / 100.0)

    out_types: List[Dict[str, Any]] = []
    created_count = 0
    reused_count = 0

    if doc is None:
        for t in unique:
            res = get_or_create_dxf_type(
                kg=kg, doc=None, thickness_m=t, bucket_cm=bucket_cm,
                base_type_ref=base_type_ref,
            )
            out_types.append(res)
            created_count += 1 if res["created"] else 0
            reused_count += 0 if res["created"] else 1
        return {
            "ok": True,
            "types": out_types,
            "created_count": created_count,
            "reused_count": reused_count,
        }

    # Revit-backed : 1 Tx pour toutes les créations.
    base_ft: Any = None
    if base_type_ref is not None:
        if not kg.has_node(base_type_ref):
            raise ValueError("Unknown base_type_ref: {}".format(base_type_ref))
        base_eid_raw = kg.get_revit_id(base_type_ref)
        if base_eid_raw is None:
            raise ValueError(
                "base_type_ref {} has no Revit binding".format(base_type_ref)
            )
        from Autodesk.Revit.DB import ElementId
        base_ft = doc.GetElement(ElementId(base_eid_raw))
    else:
        base_ft = _find_simple_basic_floor_type(doc)
        if base_ft is None:
            raise ValueError(
                "No FloorType template available. Provide `base_type_ref` "
                "or add a basic FloorType to your project template."
            )

    from .. import revit_primitives as rp
    to_create_in_revit: List[float] = []
    pre_existing: Dict[float, str] = {}  # thickness_m -> llm_id
    for t in unique:
        target_name = _dxf_floor_type_name(t, bucket_cm=bucket_cm)
        existing = _find_dxf_floor_type_in_kg(kg, target_name)
        if existing is not None and _validate_or_drop_stale_floor_type_binding(
            kg, doc, existing,
        ):
            pre_existing[t] = existing
        else:
            to_create_in_revit.append(t)

    new_bindings: Dict[float, int] = {}  # thickness_m -> revit_id
    if to_create_in_revit:
        with rp.transaction(doc, "floors.get_or_create_dxf_type_many"):
            for t in to_create_in_revit:
                target_name = _dxf_floor_type_name(t, bucket_cm=bucket_cm)
                new_bindings[t] = _create_dxf_floor_type_in_revit(
                    doc, base_ft, target_name, t,
                )

    for t in unique:
        target_name = _dxf_floor_type_name(t, bucket_cm=bucket_cm)
        if t in pre_existing:
            llm_id = pre_existing[t]
            node = kg.get_node(llm_id)
            out_types.append({
                "ok": True,
                "llm_id": llm_id,
                "name": target_name,
                "thickness_m": float(node.get("total_thickness", t)),
                "created": False,
                "revit_id": kg.get_revit_id(llm_id),
            })
            reused_count += 1
        else:
            revit_id = new_bindings[t]
            llm_id = kg.add_node("FloorType", {
                "name": target_name,
                "total_thickness": t,
            })
            kg.set_revit_id(llm_id, revit_id)
            out_types.append({
                "ok": True,
                "llm_id": llm_id,
                "name": target_name,
                "thickness_m": t,
                "created": True,
                "revit_id": revit_id,
            })
            created_count += 1

    return {
        "ok": True,
        "types": out_types,
        "created_count": created_count,
        "reused_count": reused_count,
    }
