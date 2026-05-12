"""tools/openings.py — création et modification des ouvertures hostées (portes, fenêtres).

Pattern doc-aware identique à `walls.py` / `columns.py` (§Phase 8 du
JOURNAL) : si `doc is None` (CLI / pytest), seule la mutation KG est
appliquée et `revit_id: None` est explicitement retourné. Sinon, la
branche Revit ouvre une `revit_primitives.transaction` qui enveloppe
`Document.Create.NewFamilyInstance` + `kg.add_node`/`set_revit_id` +
`stamp_llm_id` ; le `kg.transaction()` externe (ouvert par le
dispatcher) fournit la rollback symétrique en cas d'exception.

**Position** : `[x, y]` en mètres dans le plan du niveau. La 3ᵉ
coordonnée est dérivée de l'élévation du niveau hôte (le mur impose
son `Level`). Revit projette l'ouverture sur la `LocationCurve` du
mur ; un point hors-mur déclenche une `ArgumentException` Revit que
le shell défensif remonte intacte.

**Sill / Head height** : optionnels à la création (la famille a son
propre défaut). Réglables a posteriori via `openings_set_sill_height`
et `openings_set_head_height` qui touchent `INSTANCE_SILL_HEIGHT_PARAM`
et `INSTANCE_HEAD_HEIGHT_PARAM` respectivement.

**Type de famille** : `family_type_ref` est le llm_id d'un node
`FamilyType` dont l'attribut `category` vaut "Doors" ou "Windows" —
résolu via `catalog_list_door_types` / `catalog_list_window_types`. Le
tool refuse un type dont la catégorie ne correspond pas à l'ouverture
créée (pas de fenêtre via `openings_create_door`).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG
from ._helpers import bulk_setter_summary, bulk_summary, stamp_llm_id


# ----- Internal helpers --------------------------------------------------


def _resolve_host_level_ref(kg: ProjectKG, host_wall_ref: str) -> str:
    """Return the level_ref of the host wall — used to set `at_level` edge
    on the opening (inherits its level from the host)."""
    wall_attrs = kg.get_node(host_wall_ref)
    if wall_attrs.get("_type") != "Wall":
        raise ValueError(
            "host_wall_ref {} is a {}, not a Wall".format(
                host_wall_ref, wall_attrs.get("_type"),
            )
        )
    level_ref = wall_attrs.get("level_ref")
    if level_ref is None:
        raise ValueError(
            "Wall {} has no level_ref — KG inconsistent.".format(host_wall_ref)
        )
    return level_ref


def _require_opening_type(kg: ProjectKG, family_type_ref: str, expected_category: str) -> Dict[str, Any]:
    """Pre-flight check on the family_type_ref → it must be a FamilyType
    node in the right category ("Doors" or "Windows"). Returns the node
    attrs so the caller can read `family_name` / `type_name` for the
    response without re-fetching.
    """
    if not kg.has_node(family_type_ref):
        raise ValueError("Unknown family_type_ref: {}".format(family_type_ref))
    attrs = kg.get_node(family_type_ref)
    if attrs.get("_type") != "FamilyType":
        raise ValueError(
            "family_type_ref {} is a {}, not a FamilyType".format(
                family_type_ref, attrs.get("_type"),
            )
        )
    category = attrs.get("category")
    if category != expected_category:
        raise ValueError(
            "family_type_ref {} has category={}, expected {}".format(
                family_type_ref, category, expected_category,
            )
        )
    return attrs


def _record_in_kg(
    kg: ProjectKG,
    *,
    node_type: str,             # "Door" | "Window"
    host_wall_ref: str,
    family_type_ref: str,
    position: List[float],
    sill_height: float,
    head_height: float,
    level_ref: str,
) -> str:
    """Mutate the KG side. Returns the new opening's llm_id.

    `host_wall_ref → llm_id` edge is `hosts` (wall hosts opening) ; the
    opening also gets `is_type` and `at_level` for symmetry with walls
    / columns. Three edges total per opening.
    """
    llm_id = kg.add_node(node_type, {
        "type_ref": family_type_ref,
        "host_wall_ref": host_wall_ref,
        "position": list(position),
        "sill_height": float(sill_height),
        "head_height": float(head_height),
    })
    kg.add_edge(host_wall_ref, llm_id, "hosts")
    kg.add_edge(llm_id, family_type_ref, "is_type")
    kg.add_edge(llm_id, level_ref, "at_level")
    return llm_id


def _require_live_opening(kg: ProjectKG, llm_id: str) -> Dict[str, Any]:
    """Common preflight for mutating opening tools.

    Raises ValueError if the node is missing, not a Door/Window, or
    already soft-deleted. Returns the node attrs dict so the caller can
    use it (e.g. read current sill/head values).
    """
    if not kg.has_node(llm_id):
        raise ValueError("Unknown llm_id: {}".format(llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") not in ("Door", "Window"):
        raise ValueError(
            "llm_id {} is a {}, not a Door or Window".format(
                llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError("Opening {} is already soft-deleted".format(llm_id))
    return node


def _create_opening_revit_path(
    kg: ProjectKG,
    doc: Any,
    *,
    node_type: str,
    host_wall_ref: str,
    family_type_ref: str,
    position: List[float],
    sill_height: Optional[float],
    level_ref: str,
    tx_name: str,
) -> Dict[str, Any]:
    """Revit-backed creation path. Caller resolves refs upfront (host_wall_ref,
    family_type_ref, level_ref) so we can pop a clean ValueError before
    importing `Autodesk.Revit.DB` (otherwise a hors-Revit caller with a
    sentinel `doc` would see ImportError instead).

    Returns `{ok, llm_id, revit_id, sill_height_m, head_height_m}`.
    """
    host_eid_raw = kg.get_revit_id(host_wall_ref)
    type_eid_raw = kg.get_revit_id(family_type_ref)
    if host_eid_raw is None:
        raise ValueError(
            "Host wall {} has no Revit binding — run Refresh KG.".format(host_wall_ref)
        )
    if type_eid_raw is None:
        raise ValueError(
            "FamilyType {} has no Revit binding — run Refresh KG.".format(family_type_ref)
        )
    level_node = kg.get_node(level_ref)
    level_elev_m = float(level_node.get("elevation", 0.0))

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId, XYZ
    from Autodesk.Revit.DB.Structure import StructuralType

    host_eid = ElementId(host_eid_raw)
    type_eid = ElementId(type_eid_raw)

    revit_id: Optional[int] = None
    sill_height_out = 0.0
    head_height_out = 0.0
    llm_id_out: Optional[str] = None

    with rp.transaction(doc, tx_name):
        host = doc.GetElement(host_eid)
        symbol = doc.GetElement(type_eid)
        # FamilySymbols must be active before placement (§Phase 2 of
        # REVIT_API_NOTES). Batch-style activate to avoid one regen per
        # bulk creation later — here the cost is negligible solo too.
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()

        # World XYZ — z = host level elevation; Revit handles the rest
        # (projects to host wall's location curve + applies sill height).
        x_ft = rp.meters_to_internal(position[0])
        y_ft = rp.meters_to_internal(position[1])
        z_ft = rp.meters_to_internal(level_elev_m)
        point = XYZ(x_ft, y_ft, z_ft)

        instance = doc.Create.NewFamilyInstance(
            point, symbol, host, StructuralType.NonStructural,
        )
        revit_id = int(instance.Id.Value)

        if sill_height is not None:
            sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
            if sill_param is not None and not sill_param.IsReadOnly:
                sill_param.Set(rp.meters_to_internal(sill_height))

        # Read back the post-creation sill/head so the KG mirrors what
        # Revit actually committed (family default may override user
        # input on some types).
        sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        head_param = instance.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
        sill_height_out = rp.internal_to_meters(sill_param.AsDouble()) if sill_param else 0.0
        head_height_out = rp.internal_to_meters(head_param.AsDouble()) if head_param else 0.0

        llm_id_out = _record_in_kg(
            kg,
            node_type=node_type,
            host_wall_ref=host_wall_ref,
            family_type_ref=family_type_ref,
            position=position,
            sill_height=sill_height_out,
            head_height=head_height_out,
            level_ref=level_ref,
        )
        kg.set_revit_id(llm_id_out, revit_id)
        stamp_llm_id(instance, llm_id_out)

    return {
        "ok": True,
        "llm_id": llm_id_out,
        "revit_id": revit_id,
        "sill_height_m": round(sill_height_out, 3),
        "head_height_m": round(head_height_out, 3),
    }


# ----- Tools -------------------------------------------------------------


@tool(name="openings_create_door", tier=1)
def create_door(
    kg: ProjectKG,
    doc: Any,
    host_wall_ref: str,
    family_type_ref: str,
    position: List[float],
    sill_height: Optional[float] = None,
) -> Dict[str, Any]:
    """Crée une porte hostée dans un mur à la position donnée.

    L'ouverture est projetée par Revit sur la `LocationCurve` du mur ;
    le z est dérivé du niveau hôte. Si `sill_height` est fourni, le
    paramètre `INSTANCE_SILL_HEIGHT_PARAM` est posé après création.

    Concepts: porte, door, ouverture, création, hosted, mur
    Phrases: "ajoute une porte", "crée une porte dans ce mur",
             "place a door at", "ouvre une porte"
    Similar: openings_create_window, openings_create_many

    Args:
        host_wall_ref: llm_id du mur hôte.
        family_type_ref: llm_id d'un FamilyType de catégorie "Doors"
            (obtenu via `catalog_list_door_types`).
        position: point d'insertion [x, y] en mètres dans le plan du
            niveau. Doit être proche de la `LocationCurve` du mur
            hôte sinon Revit refuse.
        sill_height: hauteur d'allège en mètres (par défaut : valeur
            de la famille, souvent 0 pour les portes).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None,
         "sill_height_m": float, "head_height_m": float}
        L'`llm_id` retourné est l'unique source de vérité pour
        identifier cette porte dans les modifications ultérieures
        (`openings_set_sill_height`, `openings_delete`, etc.).
    """
    level_ref = _resolve_host_level_ref(kg, host_wall_ref)
    _require_opening_type(kg, family_type_ref, "Doors")

    if doc is None:
        llm_id = _record_in_kg(
            kg,
            node_type="Door",
            host_wall_ref=host_wall_ref,
            family_type_ref=family_type_ref,
            position=position,
            sill_height=float(sill_height) if sill_height is not None else 0.0,
            head_height=0.0,
            level_ref=level_ref,
        )
        return {
            "ok": True,
            "llm_id": llm_id,
            "revit_id": None,
            "sill_height_m": round(float(sill_height) if sill_height is not None else 0.0, 3),
            "head_height_m": 0.0,
        }

    return _create_opening_revit_path(
        kg, doc,
        node_type="Door",
        host_wall_ref=host_wall_ref,
        family_type_ref=family_type_ref,
        position=position,
        sill_height=sill_height,
        level_ref=level_ref,
        tx_name="openings.create_door",
    )


@tool(name="openings_create_window", tier=1)
def create_window(
    kg: ProjectKG,
    doc: Any,
    host_wall_ref: str,
    family_type_ref: str,
    position: List[float],
    sill_height: Optional[float] = None,
) -> Dict[str, Any]:
    """Crée une fenêtre hostée dans un mur à la position donnée.

    Même mécanique que `openings_create_door`, mais le `family_type_ref`
    doit être de catégorie "Windows".

    Concepts: fenêtre, window, ouverture, création, hosted, allège, mur
    Phrases: "ajoute une fenêtre", "crée une fenêtre dans ce mur",
             "place a window", "perce une fenêtre"
    Similar: openings_create_door, openings_create_many, openings_set_sill_height

    Args:
        host_wall_ref: llm_id du mur hôte.
        family_type_ref: llm_id d'un FamilyType de catégorie "Windows"
            (obtenu via `catalog_list_window_types`).
        position: point d'insertion [x, y] en mètres dans le plan du
            niveau.
        sill_height: hauteur d'allège en mètres (par défaut : valeur
            de la famille, souvent 900 mm).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None,
         "sill_height_m": float, "head_height_m": float}
    """
    level_ref = _resolve_host_level_ref(kg, host_wall_ref)
    _require_opening_type(kg, family_type_ref, "Windows")

    if doc is None:
        llm_id = _record_in_kg(
            kg,
            node_type="Window",
            host_wall_ref=host_wall_ref,
            family_type_ref=family_type_ref,
            position=position,
            sill_height=float(sill_height) if sill_height is not None else 0.9,
            head_height=0.0,
            level_ref=level_ref,
        )
        return {
            "ok": True,
            "llm_id": llm_id,
            "revit_id": None,
            "sill_height_m": round(float(sill_height) if sill_height is not None else 0.9, 3),
            "head_height_m": 0.0,
        }

    return _create_opening_revit_path(
        kg, doc,
        node_type="Window",
        host_wall_ref=host_wall_ref,
        family_type_ref=family_type_ref,
        position=position,
        sill_height=sill_height,
        level_ref=level_ref,
        tx_name="openings.create_window",
    )


def _validate_opening_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> Dict[str, Any]:
    """Pre-flight one item from `openings_create_many`. Raises ValueError
    on bad input (with item index for diagnostics). Returns a normalised
    dict ready for the create loop."""
    required = ("kind", "host_wall_ref", "family_type_ref", "position")
    missing = [k for k in required if k not in item]
    if missing:
        raise ValueError(
            "Item #{}: missing keys {}".format(index, sorted(missing))
        )
    kind = item["kind"]
    if kind not in ("door", "window"):
        raise ValueError(
            "Item #{}: kind must be 'door' or 'window', got {!r}".format(
                index, kind,
            )
        )
    host_wall_ref = item["host_wall_ref"]
    family_type_ref = item["family_type_ref"]
    expected_cat = "Doors" if kind == "door" else "Windows"
    # These calls also raise ValueError with context-rich messages —
    # let them propagate so the caller knows exactly which item failed.
    level_ref = _resolve_host_level_ref(kg, host_wall_ref)
    _require_opening_type(kg, family_type_ref, expected_cat)
    return {
        "kind": kind,
        "node_type": "Door" if kind == "door" else "Window",
        "host_wall_ref": host_wall_ref,
        "family_type_ref": family_type_ref,
        "position": list(item["position"]),
        "sill_height": item.get("sill_height"),
        "level_ref": level_ref,
    }


@tool(name="openings_create_many", tier=1)
def create_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Crée plusieurs ouvertures (portes / fenêtres) en une seule transaction.

    Chaque item est un dict avec `kind` ("door" | "window"),
    `host_wall_ref`, `family_type_ref`, `position` (mètres),
    optionnellement `sill_height`. La validation se fait *upfront* sur
    tous les items avant la moindre mutation Revit : un item mal formé
    fait échouer la batch entière (atomicité — rien de créé côté Revit
    ni côté KG).

    Concepts: ouvertures, bulk, batch, multi, portes, fenêtres, masse
    Phrases: "ajoute toutes les portes", "crée 5 fenêtres",
             "perce des ouvertures sur cette série de murs"
    Similar: openings_create_door, openings_create_window, walls_create_many

    Args:
        items: liste de dicts, ex :
            `[{"kind": "door", "host_wall_ref": "wall_001",
               "family_type_ref": "family_type_004", "position": [1.0, 0.0]},
              {"kind": "window", "host_wall_ref": "wall_002",
               "family_type_ref": "family_type_007", "position": [3.0, 0.5],
               "sill_height": 0.9}]`

    Returns:
        Réponse compacte (`lib.tools._helpers.bulk_summary`) — soit
        liste explicite d'`llm_ids`, soit `first_llm_id`/`last_llm_id`
        + `contiguous: True` pour les batches contigus.
    """
    if not items:
        return bulk_summary([])

    specs = [_validate_opening_item(kg, it, i) for i, it in enumerate(items)]

    if doc is None:
        # KG-only path : créer tout sans toucher Revit.
        llm_ids: List[str] = []
        for spec in specs:
            sill = (
                float(spec["sill_height"])
                if spec["sill_height"] is not None
                else (0.0 if spec["kind"] == "door" else 0.9)
            )
            llm_id = _record_in_kg(
                kg,
                node_type=spec["node_type"],
                host_wall_ref=spec["host_wall_ref"],
                family_type_ref=spec["family_type_ref"],
                position=spec["position"],
                sill_height=sill,
                head_height=0.0,
                level_ref=spec["level_ref"],
            )
            llm_ids.append(llm_id)
        return bulk_summary(llm_ids)

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId, XYZ
    from Autodesk.Revit.DB.Structure import StructuralType

    llm_ids = []
    with rp.transaction(doc, "openings.create_many"):
        # Activate every distinct FamilySymbol once upfront — avoids one
        # Regenerate() per NewFamilyInstance on bulk batches (mirrors the
        # `columns_create_many` pattern, §Phase 14 of the journal addendum).
        activated_symbols = set()
        for spec in specs:
            type_eid_raw = kg.get_revit_id(spec["family_type_ref"])
            if type_eid_raw is None:
                raise ValueError(
                    "FamilyType {} has no Revit binding".format(spec["family_type_ref"])
                )
            type_eid = ElementId(type_eid_raw)
            if type_eid_raw not in activated_symbols:
                symbol = doc.GetElement(type_eid)
                if not symbol.IsActive:
                    symbol.Activate()
                activated_symbols.add(type_eid_raw)
        if activated_symbols:
            doc.Regenerate()

        for spec in specs:
            host_eid_raw = kg.get_revit_id(spec["host_wall_ref"])
            type_eid_raw = kg.get_revit_id(spec["family_type_ref"])
            if host_eid_raw is None:
                raise ValueError(
                    "Host wall {} has no Revit binding".format(spec["host_wall_ref"])
                )
            host_eid = ElementId(host_eid_raw)
            type_eid = ElementId(type_eid_raw)
            host = doc.GetElement(host_eid)
            symbol = doc.GetElement(type_eid)

            level_node = kg.get_node(spec["level_ref"])
            level_elev_m = float(level_node.get("elevation", 0.0))
            point = XYZ(
                rp.meters_to_internal(spec["position"][0]),
                rp.meters_to_internal(spec["position"][1]),
                rp.meters_to_internal(level_elev_m),
            )
            instance = doc.Create.NewFamilyInstance(
                point, symbol, host, StructuralType.NonStructural,
            )
            revit_id = int(instance.Id.Value)

            if spec["sill_height"] is not None:
                sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
                if sill_param is not None and not sill_param.IsReadOnly:
                    sill_param.Set(rp.meters_to_internal(spec["sill_height"]))

            sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
            head_param = instance.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
            sill_out = rp.internal_to_meters(sill_param.AsDouble()) if sill_param else 0.0
            head_out = rp.internal_to_meters(head_param.AsDouble()) if head_param else 0.0

            llm_id = _record_in_kg(
                kg,
                node_type=spec["node_type"],
                host_wall_ref=spec["host_wall_ref"],
                family_type_ref=spec["family_type_ref"],
                position=spec["position"],
                sill_height=sill_out,
                head_height=head_out,
                level_ref=spec["level_ref"],
            )
            kg.set_revit_id(llm_id, revit_id)
            stamp_llm_id(instance, llm_id)
            llm_ids.append(llm_id)

    return bulk_summary(llm_ids)


# Threshold below which we consider a re-read value to match the
# requested one — half a millimetre tolerates the feet↔metres
# round-trip without flagging false drifts.
_DRIFT_EPSILON_M = 5e-4


def _read_sill_head_m(element: Any) -> Dict[str, Optional[float]]:
    """Read both sill / head heights from a Revit Door / Window. Returns
    `{sill_height_m, head_height_m}` with None for any param not exposed
    on the family. Caller must be inside an open Revit transaction.
    """
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter

    out: Dict[str, Optional[float]] = {
        "sill_height_m": None,
        "head_height_m": None,
    }
    sill = element.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
    if sill is not None:
        try:
            out["sill_height_m"] = rp.internal_to_meters(sill.AsDouble())
        except Exception:  # noqa: BLE001
            out["sill_height_m"] = None
    head = element.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
    if head is not None:
        try:
            out["head_height_m"] = rp.internal_to_meters(head.AsDouble())
        except Exception:  # noqa: BLE001
            out["head_height_m"] = None
    return out


def _drift_note(
    requested_field: str,
    requested_value: float,
    actual_sill: Optional[float],
    actual_head: Optional[float],
) -> Optional[str]:
    """Build a human-readable drift note when Revit recomputed the value
    away from what we asked. Returns None if no drift, or if the
    actual value isn't readable.

    The note explains the Revit constraint to the LLM in plain language
    so it can warn the user rather than parrot the demanded value.
    """
    actual_value = actual_sill if requested_field == "sill_height" else actual_head
    if actual_value is None:
        return None
    if abs(actual_value - requested_value) <= _DRIFT_EPSILON_M:
        return None
    # The committed sill/head differs from the requested one — Revit
    # used the family's opening_height (type-level) to recompute the
    # other endpoint, and the param we set ended up overridden.
    return (
        "Revit a commit {actual:.3f} m au lieu de {req:.3f} m demandé. "
        "La famille de cet élément a une hauteur d'ouverture fixée par "
        "le type ; tu peux probablement obtenir la valeur visée en "
        "changeant de type via openings_set_type (cherche un type dont "
        "dimensions.height_m = head − sill voulus) ou en créant une "
        "variante via openings_create_type_variant."
    ).format(actual=actual_value, req=requested_value)


@tool(name="openings_set_sill_height", tier=1)
def set_sill_height(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    sill_height_m: float,
) -> Dict[str, Any]:
    """Règle la hauteur d'allège (`INSTANCE_SILL_HEIGHT_PARAM`) d'une porte
    ou d'une fenêtre.

    **Couplage sill ↔ head côté Revit.** La hauteur d'ouverture
    `head − sill` est presque toujours un paramètre de TYPE (fixé par
    le FamilySymbol). Setter le sill décale donc automatiquement le
    head. Si l'utilisateur veut sill ET head indépendants, il faut
    changer de type (`openings_set_type`) ou dupliquer le type avec
    une hauteur d'ouverture compatible (`openings_create_type_variant`).
    Quand Revit refuse silencieusement de tenir la valeur demandée
    (cas où sill + opening_height violerait le contour), le champ
    `drift` du résultat est True et une `drift_note` explique le
    contournement.

    Concepts: allège, sill, hauteur, modification, porte, fenêtre
    Phrases: "passe l'allège à X cm", "lève l'allège",
             "set the sill at", "abaisse la fenêtre"
    Similar: openings_set_head_height, openings_set_type,
             openings_create_type_variant

    Args:
        llm_id: llm_id de la porte ou fenêtre à modifier.
        sill_height_m: nouvelle hauteur d'allège en mètres (distance au
            niveau hôte).

    Returns:
        {"ok": bool, "llm_id": str,
         "sill_height_m": float,        # valeur réellement committée
         "head_height_m": float,        # head après recompute Revit
         "requested_sill_height_m": float,
         "drift": bool, "drift_note": str | None,
         "revit_modified": bool}
    """
    node = _require_live_opening(kg, llm_id)
    sill_value = float(sill_height_m)

    if doc is None:
        # Hors-Revit : pas de recompute familial, le KG trust la valeur
        # demandée. Drift toujours False.
        kg.modify_node(llm_id, {"sill_height": sill_value})
        return {
            "ok": True,
            "llm_id": llm_id,
            "sill_height_m": sill_value,
            "head_height_m": node.get("head_height", 0.0),
            "requested_sill_height_m": sill_value,
            "drift": False,
            "drift_note": None,
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "{} {} has no Revit binding — run Refresh KG.".format(
                node.get("_type"), llm_id,
            )
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    eid = ElementId(eid_raw)
    actual_sill_m: Optional[float] = None
    actual_head_m: Optional[float] = None
    with rp.transaction(doc, "openings.set_sill_height"):
        element = doc.GetElement(eid)
        param = element.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if param is None or param.IsReadOnly:
            raise ValueError(
                "{} {} doesn't expose INSTANCE_SILL_HEIGHT_PARAM (read-only "
                "or not on this family).".format(node.get("_type"), llm_id)
            )
        ok = bool(param.Set(rp.meters_to_internal(sill_value)))
        if not ok:
            raise ValueError(
                "Revit refused to set sill height on {} {} — check the "
                "family constraints.".format(node.get("_type"), llm_id)
            )
        # Re-read BOTH parameters post-Set : the family's opening_height
        # (type-level) may have forced Revit to recompute head_height
        # to preserve `head − sill = opening_height`. We mirror the
        # actual committed values in the KG, not the requested ones —
        # otherwise the KG drifts silently from Revit (cf. JOURNAL.md
        # 2026-05-11 session 5).
        reread = _read_sill_head_m(element)
        actual_sill_m = reread["sill_height_m"]
        actual_head_m = reread["head_height_m"]
        kg_updates: Dict[str, Any] = {}
        if actual_sill_m is not None:
            kg_updates["sill_height"] = float(actual_sill_m)
        if actual_head_m is not None:
            kg_updates["head_height"] = float(actual_head_m)
        if kg_updates:
            kg.modify_node(llm_id, kg_updates)

    note = _drift_note(
        "sill_height", sill_value, actual_sill_m, actual_head_m,
    )
    return {
        "ok": True,
        "llm_id": llm_id,
        "sill_height_m": (
            round(actual_sill_m, 3) if actual_sill_m is not None else sill_value
        ),
        "head_height_m": (
            round(actual_head_m, 3)
            if actual_head_m is not None else node.get("head_height", 0.0)
        ),
        "requested_sill_height_m": round(sill_value, 3),
        "drift": note is not None,
        "drift_note": note,
        "revit_modified": True,
    }


@tool(name="openings_set_head_height", tier=1)
def set_head_height(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    head_height_m: float,
) -> Dict[str, Any]:
    """Règle la hauteur de linteau (`INSTANCE_HEAD_HEIGHT_PARAM`) d'une
    porte ou d'une fenêtre.

    **Couplage sill ↔ head.** Voir `openings_set_sill_height` — même
    contrainte symétrique. Setter le head décale le sill via la hauteur
    d'ouverture du type. Drift signalé dans la réponse quand Revit
    n'a pas tenu la valeur demandée.

    Concepts: linteau, lintel, head, hauteur, modification, porte, fenêtre
    Phrases: "passe le linteau à X cm", "lève le linteau",
             "set the head at", "abaisse le haut de la porte"
    Similar: openings_set_sill_height, openings_set_type

    Args:
        llm_id: llm_id de la porte ou fenêtre à modifier.
        head_height_m: nouvelle hauteur de linteau en mètres (distance
            au niveau hôte, mesurée au haut de l'ouverture).

    Returns:
        {"ok": bool, "llm_id": str,
         "head_height_m": float,        # valeur réellement committée
         "sill_height_m": float,        # sill après recompute Revit
         "requested_head_height_m": float,
         "drift": bool, "drift_note": str | None,
         "revit_modified": bool}
    """
    node = _require_live_opening(kg, llm_id)
    head_value = float(head_height_m)

    if doc is None:
        kg.modify_node(llm_id, {"head_height": head_value})
        return {
            "ok": True,
            "llm_id": llm_id,
            "head_height_m": head_value,
            "sill_height_m": node.get("sill_height", 0.0),
            "requested_head_height_m": head_value,
            "drift": False,
            "drift_note": None,
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "{} {} has no Revit binding — run Refresh KG.".format(
                node.get("_type"), llm_id,
            )
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    eid = ElementId(eid_raw)
    actual_sill_m: Optional[float] = None
    actual_head_m: Optional[float] = None
    with rp.transaction(doc, "openings.set_head_height"):
        element = doc.GetElement(eid)
        param = element.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
        if param is None or param.IsReadOnly:
            raise ValueError(
                "{} {} doesn't expose INSTANCE_HEAD_HEIGHT_PARAM (read-only "
                "or not on this family).".format(node.get("_type"), llm_id)
            )
        ok = bool(param.Set(rp.meters_to_internal(head_value)))
        if not ok:
            raise ValueError(
                "Revit refused to set head height on {} {} — check the "
                "family constraints.".format(node.get("_type"), llm_id)
            )
        reread = _read_sill_head_m(element)
        actual_sill_m = reread["sill_height_m"]
        actual_head_m = reread["head_height_m"]
        kg_updates: Dict[str, Any] = {}
        if actual_sill_m is not None:
            kg_updates["sill_height"] = float(actual_sill_m)
        if actual_head_m is not None:
            kg_updates["head_height"] = float(actual_head_m)
        if kg_updates:
            kg.modify_node(llm_id, kg_updates)

    note = _drift_note(
        "head_height", head_value, actual_sill_m, actual_head_m,
    )
    return {
        "ok": True,
        "llm_id": llm_id,
        "head_height_m": (
            round(actual_head_m, 3) if actual_head_m is not None else head_value
        ),
        "sill_height_m": (
            round(actual_sill_m, 3)
            if actual_sill_m is not None else node.get("sill_height", 0.0)
        ),
        "requested_head_height_m": round(head_value, 3),
        "drift": note is not None,
        "drift_note": note,
        "revit_modified": True,
    }


@tool(name="openings_set_type", tier=1)
def set_type(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    new_family_type_ref: str,
) -> Dict[str, Any]:
    """Change le type (FamilySymbol) d'une porte ou fenêtre existante.

    Le nouveau type doit appartenir à la **même catégorie** que l'ancien
    (un Door reste un Door, une Window reste une Window) — l'agent doit
    re-créer l'élément s'il veut changer de catégorie. Cas d'usage
    principal : découpler `sill_height` et `head_height` quand le type
    courant impose une hauteur d'ouverture incompatible — on switche
    vers un type qui a la bonne `dimensions.height_m`, et sill / head
    deviennent assignables à des valeurs indépendantes.

    Concepts: type, swap, FamilySymbol, type d'ouverture, hauteur,
              variant
    Phrases: "change le type de cette porte", "swap window type",
             "remplace par un type plus haut",
             "découple l'allège du linteau"
    Similar: openings_create_type_variant, openings_set_sill_height

    Args:
        llm_id: llm_id de la porte ou fenêtre à modifier.
        new_family_type_ref: llm_id du nouveau FamilyType (obtenu via
            `catalog_list_door_types` ou `_window_types`).

    Returns:
        {"ok": bool, "llm_id": str, "old_type_ref": str,
         "new_type_ref": str, "sill_height_m": float,
         "head_height_m": float, "revit_modified": bool}
        Les `sill_height_m` / `head_height_m` sont re-lus *après* le
        swap (un nouveau type avec une autre hauteur d'ouverture les
        décale).
    """
    node = _require_live_opening(kg, llm_id)
    if not kg.has_node(new_family_type_ref):
        raise ValueError(
            "Unknown new_family_type_ref: {}".format(new_family_type_ref)
        )
    new_type = kg.get_node(new_family_type_ref)
    if new_type.get("_type") != "FamilyType":
        raise ValueError(
            "new_family_type_ref {} is a {}, not a FamilyType".format(
                new_family_type_ref, new_type.get("_type"),
            )
        )
    expected_category = "Doors" if node.get("_type") == "Door" else "Windows"
    if new_type.get("category") != expected_category:
        raise ValueError(
            "new_family_type_ref {} has category={}, expected {} (a {} "
            "instance can't switch to a {} type)".format(
                new_family_type_ref, new_type.get("category"),
                expected_category, node.get("_type"),
                new_type.get("category"),
            )
        )

    old_type_ref = node.get("type_ref")

    if doc is None:
        # KG-only path : re-route is_type edge + update attr, no
        # sill/head re-read (no Revit available to query post-swap).
        kg.remove_edge(llm_id, old_type_ref, "is_type")
        kg.modify_node(llm_id, {"type_ref": new_family_type_ref})
        kg.add_edge(llm_id, new_family_type_ref, "is_type")
        return {
            "ok": True,
            "llm_id": llm_id,
            "old_type_ref": old_type_ref,
            "new_type_ref": new_family_type_ref,
            "sill_height_m": float(node.get("sill_height", 0.0)),
            "head_height_m": float(node.get("head_height", 0.0)),
            "revit_modified": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    new_type_eid_raw = kg.get_revit_id(new_family_type_ref)
    if eid_raw is None:
        raise ValueError(
            "{} {} has no Revit binding — run Refresh KG.".format(
                node.get("_type"), llm_id,
            )
        )
    if new_type_eid_raw is None:
        raise ValueError(
            "FamilyType {} has no Revit binding — run Refresh KG.".format(
                new_family_type_ref,
            )
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    eid = ElementId(eid_raw)
    new_type_eid = ElementId(new_type_eid_raw)

    sill_out = float(node.get("sill_height", 0.0))
    head_out = float(node.get("head_height", 0.0))
    with rp.transaction(doc, "openings.set_type"):
        instance = doc.GetElement(eid)
        new_symbol = doc.GetElement(new_type_eid)
        if not new_symbol.IsActive:
            new_symbol.Activate()
            doc.Regenerate()
        instance.Symbol = new_symbol
        # Re-read post-swap : the new symbol may bring a different
        # opening height, shifting head_height while sill stays put
        # (or vice-versa depending on family parameter setup).
        sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        head_param = instance.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
        if sill_param is not None:
            sill_out = rp.internal_to_meters(sill_param.AsDouble())
        if head_param is not None:
            head_out = rp.internal_to_meters(head_param.AsDouble())

        kg.remove_edge(llm_id, old_type_ref, "is_type")
        kg.modify_node(llm_id, {
            "type_ref": new_family_type_ref,
            "sill_height": float(sill_out),
            "head_height": float(head_out),
        })
        kg.add_edge(llm_id, new_family_type_ref, "is_type")

    return {
        "ok": True,
        "llm_id": llm_id,
        "old_type_ref": old_type_ref,
        "new_type_ref": new_family_type_ref,
        "sill_height_m": round(sill_out, 3),
        "head_height_m": round(head_out, 3),
        "revit_modified": True,
    }


@tool(name="openings_create_type_variant", tier=1)
def create_type_variant(
    kg: ProjectKG,
    doc: Any,
    source_type_ref: str,
    new_name: str,
    opening_height_m: float,
    opening_width_m: Optional[float] = None,
) -> Dict[str, Any]:
    """Duplique un FamilyType existant en lui donnant une nouvelle hauteur
    d'ouverture (et optionnellement une nouvelle largeur).

    Cas d'usage typique : « j'ai besoin d'une variante de cette fenêtre
    en 1.20 m de haut au lieu de 1.50 ». On duplique le `FamilySymbol`,
    règle sa hauteur d'ouverture via la cascade
    `BuiltInParameter.WINDOW_HEIGHT` / `DOOR_HEIGHT` /
    `FAMILY_HEIGHT_PARAM` puis `LookupParameter("Height" | "Hauteur")` ;
    si rien n'est trouvé sur cette famille, le tool remonte un message
    actionnable plutôt que de créer un type silencieusement non
    paramétré.

    Le nouveau FamilyType est immédiatement utilisable via
    `openings_create_*` ou `openings_set_type`.

    Concepts: type, variant, duplication, dimensions, height, hauteur,
              opening
    Phrases: "crée une variante de ce type", "duplique cette fenêtre en
             1.20m", "make a 90cm tall door type"
    Similar: openings_set_type, openings_create_door, openings_create_window

    Args:
        source_type_ref: llm_id du FamilyType à dupliquer.
        new_name: nom du nouveau type (doit être unique dans la famille).
        opening_height_m: hauteur d'ouverture en mètres (paramètre de
            type, sera héritée par toutes les instances de ce type).
        opening_width_m: largeur d'ouverture en mètres (optionnel ;
            laisser à `None` conserve la largeur du source).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None,
         "family_name": str, "type_name": str, "category": str,
         "dimensions": {"height_m": float, "width_m": float|None}}
    """
    if not kg.has_node(source_type_ref):
        raise ValueError("Unknown source_type_ref: {}".format(source_type_ref))
    source_node = kg.get_node(source_type_ref)
    if source_node.get("_type") != "FamilyType":
        raise ValueError(
            "source_type_ref {} is a {}, not a FamilyType".format(
                source_type_ref, source_node.get("_type"),
            )
        )
    category = source_node.get("category")
    new_name = str(new_name).strip()
    if not new_name:
        raise ValueError("new_name must be a non-empty string")

    if doc is None:
        # KG-only path : create the FamilyType node with the requested
        # dimensions, no Revit symbol behind it (revit_id stays None).
        dims: Dict[str, Any] = {"height_m": float(opening_height_m)}
        if opening_width_m is not None:
            dims["width_m"] = float(opening_width_m)
        new_llm_id = kg.add_node("FamilyType", {
            "family_name": source_node.get("family_name"),
            "type_name": new_name,
            "category": category,
            "dimensions": dims,
        })
        return {
            "ok": True,
            "llm_id": new_llm_id,
            "revit_id": None,
            "family_name": source_node.get("family_name"),
            "type_name": new_name,
            "category": category,
            "dimensions": dims,
        }

    source_eid_raw = kg.get_revit_id(source_type_ref)
    if source_eid_raw is None:
        raise ValueError(
            "FamilyType {} has no Revit binding — run Refresh KG.".format(
                source_type_ref,
            )
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    source_eid = ElementId(source_eid_raw)
    revit_id: Optional[int] = None
    new_llm_id: Optional[str] = None
    final_dims: Dict[str, Any] = {}

    with rp.transaction(doc, "openings.create_type_variant"):
        source_symbol = doc.GetElement(source_eid)
        new_symbol = source_symbol.Duplicate(new_name)
        revit_id = int(new_symbol.Id.Value)

        height_ok = rp.opening_set_height(new_symbol, opening_height_m)
        if not height_ok:
            raise ValueError(
                "Family {} doesn't expose a writable opening height "
                "parameter (cascade tried WINDOW_HEIGHT / DOOR_HEIGHT / "
                "FAMILY_HEIGHT_PARAM and LookupParameter('Height' | "
                "'Hauteur')). The duplicate has been created but its "
                "height couldn't be set.".format(source_node.get("family_name"))
            )
        width_ok = True
        if opening_width_m is not None:
            width_ok = rp.opening_set_width(new_symbol, opening_width_m)
            if not width_ok:
                raise ValueError(
                    "Family {} doesn't expose a writable opening width "
                    "parameter.".format(source_node.get("family_name"))
                )
        # Activate so the new symbol is immediately usable for
        # NewFamilyInstance / Symbol assignment.
        if not new_symbol.IsActive:
            new_symbol.Activate()

        # Re-read the dimensions we just wrote so the KG mirrors what
        # Revit committed (rounding / family driver constraints can
        # nudge the value).
        height_m = rp.opening_read_height_m(new_symbol)
        width_m = rp.opening_read_width_m(new_symbol)
        final_dims = {}
        if height_m is not None:
            final_dims["height_m"] = round(height_m, 6)
        if width_m is not None:
            final_dims["width_m"] = round(width_m, 6)

        new_llm_id = kg.add_node("FamilyType", {
            "family_name": source_node.get("family_name"),
            "type_name": new_name,
            "category": category,
            "dimensions": final_dims,
        })
        kg.set_revit_id(new_llm_id, revit_id)
        stamp_llm_id(new_symbol, new_llm_id)

    return {
        "ok": True,
        "llm_id": new_llm_id,
        "revit_id": revit_id,
        "family_name": source_node.get("family_name"),
        "type_name": new_name,
        "category": category,
        "dimensions": final_dims,
    }


@tool(name="openings_delete", tier=1)
def delete(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
) -> Dict[str, Any]:
    """Supprime une porte ou fenêtre du projet (Revit + KG).

    Côté KG c'est un soft delete : le nœud reste avec `deleted_at_turn=N`
    posé. Côté Revit c'est une suppression dure (`Document.Delete`).
    Les edges `hosts` / `is_type` / `at_level` restent dans le KG mais
    pointent vers un nœud filtré par défaut — la traçabilité est
    préservée pour les audits.

    Concepts: porte, fenêtre, suppression, delete, enlève
    Phrases: "supprime la porte", "enlève cette fenêtre",
             "delete the door", "vire l'ouverture"
    Similar: walls_delete, openings_create_door

    Args:
        llm_id: llm_id de la porte ou fenêtre à supprimer.

    Returns:
        {"ok": bool, "llm_id": str, "deleted_at_turn": int,
         "revit_deleted": bool}
    """
    _require_live_opening(kg, llm_id)
    kg.soft_delete(llm_id)

    if doc is None:
        return {
            "ok": True,
            "llm_id": llm_id,
            "deleted_at_turn": kg.turn,
            "revit_deleted": False,
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Opening {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    with rp.transaction(doc, "openings.delete"):
        doc.Delete(eid)

    return {
        "ok": True,
        "llm_id": llm_id,
        "deleted_at_turn": kg.turn,
        "revit_deleted": True,
    }


# ----- Bulk setters (V0 session 2026-05-12 b — dette setters_many) ----------


def _validate_sill_or_head_item(
    kg: ProjectKG, item: Dict[str, Any], index: int, field: str,
) -> Dict[str, Any]:
    """Preflight one item from `openings_set_sill_height_many` /
    `_head_height_many`. `field` is `"sill_height_m"` or `"head_height_m"`."""
    if not isinstance(item, dict):
        raise ValueError(
            "items[{}] must be a dict, got {}".format(index, type(item).__name__)
        )
    llm_id = item.get("llm_id")
    value = item.get(field)
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") not in ("Door", "Window"):
        raise ValueError(
            "items[{}]: {} is a {}, not a Door or Window".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is soft-deleted".format(index, llm_id)
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            "items[{}]: {} must be numeric".format(index, field)
        )
    return {"llm_id": llm_id, field: float(value)}


@tool(name="openings_set_sill_height_many", tier=1)
def set_sill_height_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Règle l'allège (`INSTANCE_SILL_HEIGHT_PARAM`) de N ouvertures en
    **une seule** Tx Revit + une seule Tx KG.

    Couplage sill ↔ head identique au tool solo : la hauteur d'ouverture
    est fixée par le TYPE (FamilySymbol), donc setter le sill décale le
    head. Pour découpler, switcher de type via `openings_set_type_many`
    (ou créer une variante avec `openings_create_type_variant`).

    Concepts: allège, sill, bulk, batch, plusieurs, masse
    Phrases: "passe toutes ces allèges à 0.80 m", "uniformise les sill",
             "bulk set sill heights"
    Similar: openings_set_sill_height, openings_set_head_height_many,
             openings_set_type_many

    Args:
        items: liste de specs `{llm_id: str, sill_height_m: float}`. Au
            moins un item, chaque `llm_id` pointe sur une Door ou Window
            vivante.

    Returns:
        Réponse compacte (`_helpers.bulk_setter_summary`). Drifts pointent
        les ouvertures où sill final ≠ sill demandé (contrainte
        `opening_height` du type incompatible).
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [
        _validate_sill_or_head_item(kg, it, i, "sill_height_m")
        for i, it in enumerate(items)
    ]

    if doc is None:
        for spec in specs:
            kg.modify_node(spec["llm_id"], {"sill_height": spec["sill_height_m"]})
        return bulk_setter_summary([], count=len(specs), revit_modified=False)

    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["llm_id"]) is None:
            raise ValueError(
                "items[{}]: {} has no Revit binding — run Refresh KG.".format(
                    i, spec["llm_id"],
                )
            )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    drifts: List[Dict[str, Any]] = []
    with rp.transaction(doc, "openings.set_sill_height_many"):
        for spec in specs:
            eid = ElementId(kg.get_revit_id(spec["llm_id"]))
            element = doc.GetElement(eid)
            param = element.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
            if param is None or param.IsReadOnly:
                raise ValueError(
                    "{} doesn't expose writable INSTANCE_SILL_HEIGHT_PARAM "
                    "(read-only or not on this family).".format(spec["llm_id"])
                )
            ok = bool(param.Set(rp.meters_to_internal(spec["sill_height_m"])))
            if not ok:
                raise ValueError(
                    "Revit refused to set sill on {} — check family constraints.".format(
                        spec["llm_id"],
                    )
                )
            reread = _read_sill_head_m(element)
            kg_updates: Dict[str, Any] = {}
            if reread["sill_height_m"] is not None:
                kg_updates["sill_height"] = float(reread["sill_height_m"])
            if reread["head_height_m"] is not None:
                kg_updates["head_height"] = float(reread["head_height_m"])
            if kg_updates:
                kg.modify_node(spec["llm_id"], kg_updates)
            note = _drift_note(
                "sill_height", spec["sill_height_m"],
                reread["sill_height_m"], reread["head_height_m"],
            )
            if note is not None:
                drifts.append({"llm_id": spec["llm_id"], "note": note})

    return bulk_setter_summary(drifts, count=len(specs), revit_modified=True)


@tool(name="openings_set_head_height_many", tier=1)
def set_head_height_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Règle le linteau (`INSTANCE_HEAD_HEIGHT_PARAM`) de N ouvertures en
    **une seule** Tx Revit + une seule Tx KG.

    Symétrique de `openings_set_sill_height_many`. Couplage sill ↔ head
    identique : changer le head décale le sill via la hauteur
    d'ouverture du type.

    Concepts: linteau, head, lintel, bulk, batch, plusieurs, masse
    Phrases: "passe tous ces linteaux à 2.10 m", "uniformise les head",
             "bulk set head heights"
    Similar: openings_set_head_height, openings_set_sill_height_many,
             openings_set_type_many

    Args:
        items: liste de specs `{llm_id: str, head_height_m: float}`.

    Returns:
        Réponse compacte (`_helpers.bulk_setter_summary`).
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [
        _validate_sill_or_head_item(kg, it, i, "head_height_m")
        for i, it in enumerate(items)
    ]

    if doc is None:
        for spec in specs:
            kg.modify_node(spec["llm_id"], {"head_height": spec["head_height_m"]})
        return bulk_setter_summary([], count=len(specs), revit_modified=False)

    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["llm_id"]) is None:
            raise ValueError(
                "items[{}]: {} has no Revit binding — run Refresh KG.".format(
                    i, spec["llm_id"],
                )
            )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    drifts: List[Dict[str, Any]] = []
    with rp.transaction(doc, "openings.set_head_height_many"):
        for spec in specs:
            eid = ElementId(kg.get_revit_id(spec["llm_id"]))
            element = doc.GetElement(eid)
            param = element.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
            if param is None or param.IsReadOnly:
                raise ValueError(
                    "{} doesn't expose writable INSTANCE_HEAD_HEIGHT_PARAM "
                    "(read-only or not on this family).".format(spec["llm_id"])
                )
            ok = bool(param.Set(rp.meters_to_internal(spec["head_height_m"])))
            if not ok:
                raise ValueError(
                    "Revit refused to set head on {} — check family constraints.".format(
                        spec["llm_id"],
                    )
                )
            reread = _read_sill_head_m(element)
            kg_updates: Dict[str, Any] = {}
            if reread["sill_height_m"] is not None:
                kg_updates["sill_height"] = float(reread["sill_height_m"])
            if reread["head_height_m"] is not None:
                kg_updates["head_height"] = float(reread["head_height_m"])
            if kg_updates:
                kg.modify_node(spec["llm_id"], kg_updates)
            note = _drift_note(
                "head_height", spec["head_height_m"],
                reread["sill_height_m"], reread["head_height_m"],
            )
            if note is not None:
                drifts.append({"llm_id": spec["llm_id"], "note": note})

    return bulk_setter_summary(drifts, count=len(specs), revit_modified=True)


def _validate_set_type_item(
    kg: ProjectKG, item: Dict[str, Any], index: int,
) -> Dict[str, Any]:
    """Preflight one item from `openings_set_type_many` :
    `{llm_id, new_family_type_ref}`. Vérifie la catégorie (Door reste
    Door, Window reste Window) — pas de mélange par accident."""
    if not isinstance(item, dict):
        raise ValueError(
            "items[{}] must be a dict, got {}".format(index, type(item).__name__)
        )
    llm_id = item.get("llm_id")
    new_type_ref = item.get("new_family_type_ref")
    if not isinstance(llm_id, str) or not kg.has_node(llm_id):
        raise ValueError("items[{}]: unknown llm_id {!r}".format(index, llm_id))
    node = kg.get_node(llm_id)
    if node.get("_type") not in ("Door", "Window"):
        raise ValueError(
            "items[{}]: {} is a {}, not a Door or Window".format(
                index, llm_id, node.get("_type"),
            )
        )
    if node.get("deleted_at_turn") is not None:
        raise ValueError(
            "items[{}]: {} is soft-deleted".format(index, llm_id)
        )
    if not isinstance(new_type_ref, str) or not kg.has_node(new_type_ref):
        raise ValueError(
            "items[{}]: unknown new_family_type_ref {!r}".format(index, new_type_ref)
        )
    new_type = kg.get_node(new_type_ref)
    if new_type.get("_type") != "FamilyType":
        raise ValueError(
            "items[{}]: new_family_type_ref {} is a {}, not a FamilyType".format(
                index, new_type_ref, new_type.get("_type"),
            )
        )
    expected_category = "Doors" if node.get("_type") == "Door" else "Windows"
    if new_type.get("category") != expected_category:
        raise ValueError(
            "items[{}]: new_family_type_ref {} has category={}, expected {}".format(
                index, new_type_ref, new_type.get("category"), expected_category,
            )
        )
    return {
        "llm_id": llm_id,
        "new_family_type_ref": new_type_ref,
        "old_type_ref": node.get("type_ref"),
    }


@tool(name="openings_set_type_many", tier=1)
def set_type_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Change le FamilySymbol de N ouvertures en une seule Tx Revit + KG.

    Cas d'usage central du scénario soir 2026-05-11 session 5 : on a 20
    fenêtres à passer sur une nouvelle variante de type pour découpler
    sill / head ; en solo c'est 20 round-trips, en bulk c'est un seul
    appel. Chaque item peut viser un type cible différent (utile pour
    re-typer un mix de portes intérieures / extérieures en un seul
    coup). Transactionnel : un item invalide → aucune mutation.

    Concepts: type, swap, FamilySymbol, bulk, batch, masse
    Phrases: "change le type de toutes ces fenêtres", "re-type ces portes",
             "bulk swap types", "swap window family type for all"
    Similar: openings_set_type, openings_set_sill_height_many,
             openings_create_type_variant

    Args:
        items: liste de specs `{llm_id: str, new_family_type_ref: str}`.
            La catégorie du nouveau type doit matcher celle de
            l'ouverture (Door ↔ Doors, Window ↔ Windows).

    Returns:
        Réponse compacte (`_helpers.bulk_setter_summary`). Drift n'est
        PAS le concept ici (le swap est binaire : il a lieu ou il
        échoue) — `drifts` reste à `[]` sur cette implémentation. Une
        future itération pourrait flagger les ouvertures dont les
        sill/head ont décalé post-swap, mais c'est attendu et déjà
        signalé par les `_set_sill/head_height_*` suivants.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [_validate_set_type_item(kg, it, i) for i, it in enumerate(items)]

    if doc is None:
        for spec in specs:
            kg.remove_edge(spec["llm_id"], spec["old_type_ref"], "is_type")
            kg.modify_node(spec["llm_id"], {
                "type_ref": spec["new_family_type_ref"],
            })
            kg.add_edge(spec["llm_id"], spec["new_family_type_ref"], "is_type")
        return bulk_setter_summary([], count=len(specs), revit_modified=False)

    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["llm_id"]) is None:
            raise ValueError(
                "items[{}]: {} has no Revit binding — run Refresh KG.".format(
                    i, spec["llm_id"],
                )
            )
        if kg.get_revit_id(spec["new_family_type_ref"]) is None:
            raise ValueError(
                "items[{}]: FamilyType {} has no Revit binding.".format(
                    i, spec["new_family_type_ref"],
                )
            )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    # Group items by new_family_type_ref so we can activate each symbol
    # at most once per batch (Activate + Regenerate is expensive).
    activated: set = set()
    with rp.transaction(doc, "openings.set_type_many"):
        for spec in specs:
            inst_eid = ElementId(kg.get_revit_id(spec["llm_id"]))
            new_type_eid = ElementId(kg.get_revit_id(spec["new_family_type_ref"]))
            instance = doc.GetElement(inst_eid)
            new_symbol = doc.GetElement(new_type_eid)
            if spec["new_family_type_ref"] not in activated:
                if not new_symbol.IsActive:
                    new_symbol.Activate()
                    doc.Regenerate()
                activated.add(spec["new_family_type_ref"])
            instance.Symbol = new_symbol
            # Read sill/head post-swap so KG mirrors the new defaults.
            sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
            head_param = instance.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
            kg_updates: Dict[str, Any] = {
                "type_ref": spec["new_family_type_ref"],
            }
            if sill_param is not None:
                try:
                    kg_updates["sill_height"] = rp.internal_to_meters(sill_param.AsDouble())
                except Exception:  # noqa: BLE001
                    pass
            if head_param is not None:
                try:
                    kg_updates["head_height"] = rp.internal_to_meters(head_param.AsDouble())
                except Exception:  # noqa: BLE001
                    pass
            kg.remove_edge(spec["llm_id"], spec["old_type_ref"], "is_type")
            kg.modify_node(spec["llm_id"], kg_updates)
            kg.add_edge(spec["llm_id"], spec["new_family_type_ref"], "is_type")

    return bulk_setter_summary([], count=len(specs), revit_modified=True)
