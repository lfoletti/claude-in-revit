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

import re
from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG
from ._helpers import bulk_setter_summary, bulk_summary, stamp_llm_id


# Regex de détection des variants auto-créés par `_maybe_decouple`. Le
# format est figé par `_variant_name` : `<src> [auto h<NN>cm]`. On match
# uniquement le suffixe pour rester tolérant aux renommages utilisateur
# qui auraient *préservé* la partie [auto h<NN>cm] (cas peu probable
# mais on reste robuste).
_AUTO_VARIANT_MARKER_RE = re.compile(r"\[auto h\d+cm\]")


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
    level_eid_raw = kg.get_revit_id(level_ref)
    if host_eid_raw is None:
        raise ValueError(
            "Host wall {} has no Revit binding — run Refresh KG.".format(host_wall_ref)
        )
    if type_eid_raw is None:
        raise ValueError(
            "FamilyType {} has no Revit binding — run Refresh KG.".format(family_type_ref)
        )
    if level_eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(level_ref)
        )
    level_node = kg.get_node(level_ref)
    level_elev_m = float(level_node.get("elevation", 0.0))

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId, XYZ
    from Autodesk.Revit.DB.Structure import StructuralType

    host_eid = ElementId(host_eid_raw)
    type_eid = ElementId(type_eid_raw)
    level_eid = ElementId(level_eid_raw)

    revit_id: Optional[int] = None
    sill_height_out = 0.0
    head_height_out = 0.0
    llm_id_out: Optional[str] = None

    with rp.transaction(doc, tx_name):
        host = doc.GetElement(host_eid)
        symbol = doc.GetElement(type_eid)
        level_elem = doc.GetElement(level_eid)
        # FamilySymbols must be active before placement (§Phase 2 of
        # REVIT_API_NOTES). Batch-style activate to avoid one regen per
        # bulk creation later — here the cost is negligible solo too.
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()

        # XYZ pour openings hostées avec overload 5-args
        # `NewFamilyInstance(XYZ, FamilySymbol, host, Level, StructuralType)` :
        # - x, y en mètres dans le plan du level
        # - **z = level_elev_m** en *coordonnées monde*. Revit calcule
        #   sill = XYZ.Z − Level.Elevation, puis on impose la sill voulue
        #   via `INSTANCE_SILL_HEIGHT_PARAM.Set` plus bas.
        # Le piège du 2026-05-12 (testé en runtime sur SS01=-3m) :
        # - si on passait z=0 avec Level=SS01, sill calculée = 0−(−3)=3m,
        #   au-dessus du top du mur → erreur Revit « ne coupent rien »
        #   et fenêtre invisible (suspendue hors mur).
        # - sur Niveau 1 (elev=0), z=0 et z=level_elev=0 coïncident, donc
        #   le bug ne se manifestait pas sur ce level — d'où la confusion.
        x_ft = rp.meters_to_internal(position[0])
        y_ft = rp.meters_to_internal(position[1])
        z_ft = rp.meters_to_internal(level_elev_m)
        point = XYZ(x_ft, y_ft, z_ft)

        # 5-args overload pour binder explicitement le Reference Level
        # de la fenêtre au level du host_wall. Sans Level explicite,
        # Revit choisit le 1er Level du projet (Niveau 1 typiquement)
        # comme reference, ce qui décale la fenêtre d'un étage.
        instance = doc.Create.NewFamilyInstance(
            point, symbol, host, level_elem, StructuralType.NonStructural,
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
            level_eid_raw = kg.get_revit_id(spec["level_ref"])
            if host_eid_raw is None:
                raise ValueError(
                    "Host wall {} has no Revit binding".format(spec["host_wall_ref"])
                )
            if level_eid_raw is None:
                raise ValueError(
                    "Level {} has no Revit binding".format(spec["level_ref"])
                )
            host_eid = ElementId(host_eid_raw)
            type_eid = ElementId(type_eid_raw)
            level_eid = ElementId(level_eid_raw)
            host = doc.GetElement(host_eid)
            symbol = doc.GetElement(type_eid)
            level_elem = doc.GetElement(level_eid)

            level_node = kg.get_node(spec["level_ref"])
            level_elev_m = float(level_node.get("elevation", 0.0))

            # XYZ.Z = level_elev_m (monde). Voir note `_create_opening_revit_path` :
            # avec l'overload 5-args, le Z monde + Level explicite
            # produisent la position correcte ; z=0 est faux sur les
            # levels d'élévation non-nulle (testé sur SS01=-3m, 2026-05-12).
            point = XYZ(
                rp.meters_to_internal(spec["position"][0]),
                rp.meters_to_internal(spec["position"][1]),
                rp.meters_to_internal(level_elev_m),
            )
            # 5-args overload pour binder explicitement le Reference Level
            # de la fenêtre au level du host_wall. Sans Level explicite,
            # Revit choisit le 1er Level du projet (Niveau 1 typique).
            instance = doc.Create.NewFamilyInstance(
                point, symbol, host, level_elem, StructuralType.NonStructural,
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


# ----- Auto-decouple helpers (session 2026-05-12 c) -------------------------
#
# Pré-flight applied by `set_sill_height` / `set_head_height` (+ leurs `_many`)
# pour empêcher la dérive sill ↔ head induite par la contrainte
# familiale `opening_height = head − sill`. Trois sous-problèmes :
#
# 1. Détecter le drift à venir via `FamilyType.dimensions.height_m`.
# 2. Trouver un variant compatible existant (idempotence — évite
#    l'explosion de FamilyTypes dans le browser Revit après N appels).
# 3. Sinon, créer un variant + swap.
#
# Le résultat est invisible côté Revit user-side (le swap se fait
# silencieusement) mais visible côté tool_result (`decoupled: bool`,
# `new_type_ref`, `auto_variant_created`).


def _create_type_variant_internal(
    kg: ProjectKG,
    doc: Any,
    *,
    source_type_ref: str,
    new_name: str,
    opening_height_m: float,
    opening_width_m: Optional[float] = None,
) -> str:
    """Logique commune entre `openings_create_type_variant` (tool) et
    l'auto-découple (helper). Crée un FamilyType variant et retourne
    son nouveau llm_id.

    Branchements KG-only / Revit symétriques au tool. Aucune
    `revit_primitives.transaction` ouverte ici — le caller (`set_*`)
    est *déjà* dans sa propre Tx. C'est ce découplage qui justifie
    d'extraire le helper plutôt que d'appeler le tool via dispatch.
    """
    source_node = kg.get_node(source_type_ref)
    family_name = source_node.get("family_name")
    category = source_node.get("category")

    if doc is None:
        dims: Dict[str, Any] = {"height_m": float(opening_height_m)}
        if opening_width_m is not None:
            dims["width_m"] = float(opening_width_m)
        return kg.add_node("FamilyType", {
            "family_name": family_name,
            "type_name": new_name,
            "category": category,
            "dimensions": dims,
        })

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
    source_symbol = doc.GetElement(source_eid)
    new_symbol = source_symbol.Duplicate(new_name)
    revit_id = int(new_symbol.Id.Value)

    height_ok = rp.opening_set_height(new_symbol, opening_height_m)
    if not height_ok:
        raise ValueError(
            "Family {} doesn't expose a writable opening height "
            "parameter — cannot auto-decouple.".format(family_name)
        )
    if opening_width_m is not None:
        width_ok = rp.opening_set_width(new_symbol, opening_width_m)
        if not width_ok:
            raise ValueError(
                "Family {} doesn't expose a writable opening width "
                "parameter.".format(family_name)
            )
    if not new_symbol.IsActive:
        new_symbol.Activate()

    height_m = rp.opening_read_height_m(new_symbol)
    width_m = rp.opening_read_width_m(new_symbol)
    final_dims: Dict[str, Any] = {}
    if height_m is not None:
        final_dims["height_m"] = round(height_m, 6)
    if width_m is not None:
        final_dims["width_m"] = round(width_m, 6)

    new_llm_id = kg.add_node("FamilyType", {
        "family_name": family_name,
        "type_name": new_name,
        "category": category,
        "dimensions": final_dims,
    })
    kg.set_revit_id(new_llm_id, revit_id)
    stamp_llm_id(new_symbol, new_llm_id)
    return new_llm_id


def _find_compatible_variant(
    kg: ProjectKG,
    *,
    source_type_ref: str,
    target_opening_height_m: float,
) -> Optional[str]:
    """Cherche dans le KG un FamilyType de la même famille + catégorie
    que `source_type_ref` dont `dimensions.height_m` matche
    `target_opening_height_m` (à `_DRIFT_EPSILON_M` près).

    Renvoie le llm_id du candidat, ou None si aucun match. Idempotence
    de l'auto-découple : N appels successifs réutilisent le même
    variant au lieu d'en créer N.
    """
    source_node = kg.get_node(source_type_ref)
    family_name = source_node.get("family_name")
    category = source_node.get("category")
    for nid in kg.find_by_type("FamilyType"):
        cand = kg.get_node(nid)
        if cand.get("family_name") != family_name:
            continue
        if cand.get("category") != category:
            continue
        dims = cand.get("dimensions") or {}
        cand_height = dims.get("height_m")
        if cand_height is None:
            continue
        if abs(float(cand_height) - float(target_opening_height_m)) <= _DRIFT_EPSILON_M:
            return nid
    return None


def _variant_name(source_type_name: str, opening_height_m: float) -> str:
    """Convention de nommage des variants auto-créés : `<src> [auto hNNNcm]`.

    Marqueur `[auto]` lisible dans le browser Revit, hauteur en cm pour
    la concision (Revit affiche les noms tronqués). Idempotent : pour
    une même hauteur, le nom est strictement identique → si jamais le
    KG est rechargé sans le variant, le rescan le matchera par name.
    """
    return "{} [auto h{}cm]".format(
        source_type_name, round(float(opening_height_m) * 100),
    )


def _swap_to_type_internal(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    new_family_type_ref: str,
) -> Dict[str, float]:
    """Swap le `Symbol` d'une opening + reroute l'edge `is_type` dans le KG.

    Caller *déjà dans une `rp.transaction` ouverte* — pas de Tx imbriquée
    ici. Renvoie `{sill_height_m, head_height_m}` lus post-swap depuis
    Revit (KG-only path : renvoie les valeurs courantes du KG).
    """
    node = kg.get_node(llm_id)
    old_type_ref = node.get("type_ref")

    if doc is None:
        kg.remove_edge(llm_id, old_type_ref, "is_type")
        kg.modify_node(llm_id, {"type_ref": new_family_type_ref})
        kg.add_edge(llm_id, new_family_type_ref, "is_type")
        return {
            "sill_height_m": float(node.get("sill_height", 0.0)),
            "head_height_m": float(node.get("head_height", 0.0)),
        }

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import BuiltInParameter, ElementId

    instance = rp.get_element_or_raise(
        doc, kg.get_revit_id(llm_id), llm_id, kind=node.get("_type", "opening"),
    )
    new_symbol = rp.get_element_or_raise(
        doc, kg.get_revit_id(new_family_type_ref), new_family_type_ref,
        kind="FamilyType",
    )
    if not new_symbol.IsActive:
        new_symbol.Activate()
        doc.Regenerate()
    instance.Symbol = new_symbol

    sill_param = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
    head_param = instance.get_Parameter(BuiltInParameter.INSTANCE_HEAD_HEIGHT_PARAM)
    sill_m = rp.internal_to_meters(sill_param.AsDouble()) if sill_param else float(node.get("sill_height", 0.0))
    head_m = rp.internal_to_meters(head_param.AsDouble()) if head_param else float(node.get("head_height", 0.0))

    kg.remove_edge(llm_id, old_type_ref, "is_type")
    kg.modify_node(llm_id, {
        "type_ref": new_family_type_ref,
        "sill_height": float(sill_m),
        "head_height": float(head_m),
    })
    kg.add_edge(llm_id, new_family_type_ref, "is_type")
    return {"sill_height_m": float(sill_m), "head_height_m": float(head_m)}


def _maybe_decouple(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    *,
    new_head_m: Optional[float] = None,
    new_sill_m: Optional[float] = None,
) -> Dict[str, Any]:
    """Pré-flight pour `set_sill_height` / `set_head_height`.

    Exactement un de `new_head_m` / `new_sill_m` doit être fourni —
    c'est la valeur que l'utilisateur veut imposer. L'autre dimension
    est préservée à sa valeur courante.

    Logique :
    1. Lit `FamilyType.dimensions.height_m` du type actuel de l'opening.
       Si absent, on ne sait pas prédire → retourne `decoupled=False`
       et le caller fait le Set direct (le drift sera signalé au
       post-mortem comme avant).
    2. Calcule `target_opening = head − sill` que l'utilisateur veut
       *réellement* (en préservant l'autre dimension).
    3. Si `|target_opening − family_height| <= ε`, pas de drift attendu →
       retourne `decoupled=False`, Set direct.
    4. Sinon, cherche un variant compatible (`_find_compatible_variant`).
       Si trouvé → swap vers lui.
       Sinon → crée un nouveau variant + swap.
    5. Renvoie `{decoupled: True, new_type_ref, auto_variant_created}`.

    Idempotence : N appels successifs avec la même cible réutilisent le
    même variant (find puis swap, jamais 2 créations).
    """
    if (new_head_m is None) == (new_sill_m is None):
        raise ValueError(
            "_maybe_decouple: exactly one of new_head_m / new_sill_m must be set"
        )
    node = kg.get_node(llm_id)
    current_sill = float(node.get("sill_height", 0.0))
    current_head = float(node.get("head_height", 0.0))
    type_ref = node.get("type_ref")
    if not type_ref or not kg.has_node(type_ref):
        return {"decoupled": False, "new_type_ref": None, "auto_variant_created": False}
    type_node = kg.get_node(type_ref)
    dims = type_node.get("dimensions") or {}
    family_height = dims.get("height_m")
    if family_height is None:
        # Famille sans hauteur d'ouverture exposée — on ne peut pas
        # prédire le drift. Le Set direct se passera bien si la famille
        # est libre, ou signalera un drift sinon (chemin legacy).
        return {"decoupled": False, "new_type_ref": None, "auto_variant_created": False}

    if new_head_m is not None:
        target_opening = float(new_head_m) - current_sill
    else:
        target_opening = current_head - float(new_sill_m)

    if abs(target_opening - float(family_height)) <= _DRIFT_EPSILON_M:
        return {"decoupled": False, "new_type_ref": None, "auto_variant_created": False}

    # Drift prédit. Cherche un variant compatible.
    existing = _find_compatible_variant(
        kg,
        source_type_ref=type_ref,
        target_opening_height_m=target_opening,
    )
    if existing is not None:
        _swap_to_type_internal(kg, doc, llm_id, existing)
        return {
            "decoupled": True,
            "new_type_ref": existing,
            "auto_variant_created": False,
        }

    # Crée un nouveau variant + swap.
    new_name = _variant_name(type_node.get("type_name", "type"), target_opening)
    # Préserve la largeur d'origine (None = duplicate sans changer la
    # largeur, comportement déjà géré par `_create_type_variant_internal`).
    new_type_ref = _create_type_variant_internal(
        kg, doc,
        source_type_ref=type_ref,
        new_name=new_name,
        opening_height_m=target_opening,
        opening_width_m=None,
    )
    _swap_to_type_internal(kg, doc, llm_id, new_type_ref)
    return {
        "decoupled": True,
        "new_type_ref": new_type_ref,
        "auto_variant_created": True,
    }


# ----- Purge helpers (session 2026-05-12 d — nettoyage variants [auto]) -----


def _is_auto_variant(node: Dict[str, Any]) -> bool:
    """True si le type_name du FamilyType contient le marqueur `[auto h<NN>cm]`.

    Conservateur — si l'utilisateur a renommé manuellement le variant en
    enlevant le marqueur, le tool ne le purgera pas (et c'est tant mieux :
    le renommage signale une réappropriation du type comme variant normal).
    """
    if node.get("_type") != "FamilyType":
        return False
    name = node.get("type_name") or ""
    return bool(_AUTO_VARIANT_MARKER_RE.search(name))


def _is_family_type_in_use(kg: ProjectKG, family_type_ref: str) -> bool:
    """True iff au moins une Door ou Window vivante référence ce
    FamilyType comme `type_ref`. Itère uniquement les nodes vivants
    (filtre soft-deleted automatique via `find_by_type`).

    O(N) sur le nombre d'openings — typiquement < 100, négligeable même
    appelé en boucle dans `purge_unused_variants`.
    """
    for opening_type in ("Door", "Window"):
        for nid in kg.find_by_type(opening_type):
            attrs = kg.get_node(nid)
            if attrs.get("type_ref") == family_type_ref:
                return True
    return False


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
    preserve_head: bool = True,
) -> Dict[str, Any]:
    """Règle la hauteur d'allège (`INSTANCE_SILL_HEIGHT_PARAM`) d'une porte
    ou d'une fenêtre, en préservant par défaut la hauteur de linteau.

    **Auto-découple sill ↔ head (défaut, session 2026-05-12 c).** La
    hauteur d'ouverture `head − sill` est fixée par le TYPE (`opening_height`
    du FamilySymbol). Sans découplage, setter le sill décalerait le
    head. Le tool détecte en pré-flight l'incompatibilité via
    `FamilyType.dimensions.height_m`, cherche dans le projet un variant
    de type compatible (même famille + bonne `opening_height`), sinon
    crée silencieusement un variant nommé `<type> [auto h<NN>cm]` et
    swap l'instance dessus. **Le head est préservé**, le sill committé
    est exactement la valeur demandée. Variants réutilisés sur appels
    suivants → pas d'explosion du browser Revit.

    L'escape hatch `preserve_head=False` désactive le pré-flight :
    comportement legacy (Set direct, drift signalé dans la réponse si
    Revit a recomputé le head).

    Concepts: allège, sill, hauteur, modification, porte, fenêtre,
              découplage, variant
    Phrases: "passe l'allège à X cm", "lève l'allège",
             "set the sill at", "abaisse la fenêtre"
    Similar: openings_set_head_height, openings_set_type,
             openings_create_type_variant

    Args:
        llm_id: llm_id de la porte ou fenêtre à modifier.
        sill_height_m: nouvelle hauteur d'allège en mètres (distance au
            niveau hôte).
        preserve_head: si True (défaut), bascule auto vers un variant de
            type avec la bonne `opening_height` pour conserver le head.
            Si False, Set direct legacy (head peut dériver).

    Returns:
        {"ok": bool, "llm_id": str,
         "sill_height_m": float,        # valeur réellement committée
         "head_height_m": float,        # head après recompute Revit
         "requested_sill_height_m": float,
         "drift": bool, "drift_note": str | None,
         "decoupled": bool,             # True si swap automatique
         "new_type_ref": str | None,    # type swap-é si decoupled
         "auto_variant_created": bool,  # True si variant créé (pas réutilisé)
         "revit_modified": bool}
    """
    node = _require_live_opening(kg, llm_id)
    sill_value = float(sill_height_m)

    decouple_info: Dict[str, Any] = {
        "decoupled": False, "new_type_ref": None, "auto_variant_created": False,
    }

    if doc is None:
        # KG-only : `_maybe_decouple` ici aussi (le helper a un path
        # KG-only via `_create_type_variant_internal` doc=None). Pas
        # besoin de Tx Revit côté KG.
        if preserve_head:
            decouple_info = _maybe_decouple(kg, doc, llm_id, new_sill_m=sill_value)
            node = kg.get_node(llm_id)
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
            **decouple_info,
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
        # IMPORTANT : `_maybe_decouple` doit être DANS la Tx Revit
        # parce qu'il duplique le FamilySymbol et swap l'instance. Hors-Tx,
        # les mutations Revit retournent silencieusement None → cascade
        # `AttributeError: NoneType`. Bug runtime 2026-05-12 (post auto-
        # découple session c) — caught par les tests unitaires KG-only
        # qui ne nécessitent pas de Tx.
        if preserve_head:
            decouple_info = _maybe_decouple(kg, doc, llm_id, new_sill_m=sill_value)
            # Re-load node attrs après swap potentiel (sill/head ont pu
            # bouger pendant le swap ; le Set explicite ci-dessous réaligne
            # sill exactement à la valeur demandée).
            node = kg.get_node(llm_id)
            # Re-bind eid si le swap a affecté l'instance (en théorie pas,
            # `instance.Symbol = ...` préserve l'ElementId, mais on est défensif).
            eid_raw_after = kg.get_revit_id(llm_id)
            if eid_raw_after is not None and eid_raw_after != eid_raw:
                eid = ElementId(eid_raw_after)
        element = rp.get_element_or_raise(
            doc, eid, llm_id, kind=node.get("_type", "opening"),
        )
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
        **decouple_info,
    }


@tool(name="openings_set_head_height", tier=1)
def set_head_height(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
    head_height_m: float,
    preserve_sill: bool = True,
) -> Dict[str, Any]:
    """Règle la hauteur de linteau (`INSTANCE_HEAD_HEIGHT_PARAM`) d'une
    porte ou d'une fenêtre, en préservant par défaut la hauteur d'allège.

    **Auto-découple sill ↔ head (défaut, session 2026-05-12 c).**
    Symétrique de `openings_set_sill_height`. Sans découplage, setter le
    head décalerait le sill via la contrainte familiale
    `opening_height`. Pré-flight via `FamilyType.dimensions.height_m` :
    si la cible `head − sill_courant` diverge de la `opening_height`
    familiale, le tool cherche / crée un variant compatible et swap
    l'instance dessus silencieusement avant le Set. **Le sill est
    préservé**, le head committé est exactement la valeur demandée.

    L'escape hatch `preserve_sill=False` désactive le pré-flight :
    comportement legacy.

    Concepts: linteau, lintel, head, hauteur, modification, porte,
              fenêtre, découplage, variant
    Phrases: "passe le linteau à X cm", "lève le linteau",
             "set the head at", "abaisse le haut de la porte"
    Similar: openings_set_sill_height, openings_set_type

    Args:
        llm_id: llm_id de la porte ou fenêtre à modifier.
        head_height_m: nouvelle hauteur de linteau en mètres (distance
            au niveau hôte, mesurée au haut de l'ouverture).
        preserve_sill: si True (défaut), bascule auto vers un variant de
            type avec la bonne `opening_height` pour conserver le sill.
            Si False, Set direct legacy (sill peut dériver).

    Returns:
        {"ok": bool, "llm_id": str,
         "head_height_m": float,        # valeur réellement committée
         "sill_height_m": float,        # sill après recompute Revit
         "requested_head_height_m": float,
         "drift": bool, "drift_note": str | None,
         "decoupled": bool,             # True si swap automatique
         "new_type_ref": str | None,    # type swap-é si decoupled
         "auto_variant_created": bool,  # True si variant créé (pas réutilisé)
         "revit_modified": bool}
    """
    node = _require_live_opening(kg, llm_id)
    head_value = float(head_height_m)

    decouple_info: Dict[str, Any] = {
        "decoupled": False, "new_type_ref": None, "auto_variant_created": False,
    }

    if doc is None:
        # KG-only : `_maybe_decouple` ici (path KG du helper).
        if preserve_sill:
            decouple_info = _maybe_decouple(kg, doc, llm_id, new_head_m=head_value)
            node = kg.get_node(llm_id)
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
            **decouple_info,
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
        # `_maybe_decouple` DANS la Tx Revit — cf. note dans set_sill_height.
        if preserve_sill:
            decouple_info = _maybe_decouple(kg, doc, llm_id, new_head_m=head_value)
            node = kg.get_node(llm_id)
            eid_raw_after = kg.get_revit_id(llm_id)
            if eid_raw_after is not None and eid_raw_after != eid_raw:
                eid = ElementId(eid_raw_after)
        element = rp.get_element_or_raise(
            doc, eid, llm_id, kind=node.get("_type", "opening"),
        )
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
        **decouple_info,
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
    preserve_head: bool = True,
) -> Dict[str, Any]:
    """Règle l'allège (`INSTANCE_SILL_HEIGHT_PARAM`) de N ouvertures en
    **une seule** Tx Revit + une seule Tx KG, en préservant par défaut
    la hauteur de linteau.

    **Auto-découple sill ↔ head (défaut, session 2026-05-12 c).** Pré-flight
    par item via `FamilyType.dimensions.height_m` : si la cible
    `head_courant − sill_demandé` diverge de la `opening_height` familiale,
    bascule l'instance sur un variant compatible (cherché dans le KG,
    créé sinon — voir `_variant_name`). Les variants sont réutilisés
    entre items et entre appels → pas d'explosion du browser Revit.
    `preserve_head=False` désactive le découplage (Set direct legacy).

    Concepts: allège, sill, bulk, batch, plusieurs, masse, découplage
    Phrases: "passe toutes ces allèges à 0.80 m", "uniformise les sill",
             "bulk set sill heights"
    Similar: openings_set_sill_height, openings_set_head_height_many,
             openings_set_type_many

    Args:
        items: liste de specs `{llm_id: str, sill_height_m: float}`. Au
            moins un item, chaque `llm_id` pointe sur une Door ou Window
            vivante.
        preserve_head: défaut True. Voir `openings_set_sill_height`.

    Returns:
        Compact summary (`_helpers.bulk_setter_summary`) enrichie de
        `decoupled_count` et `auto_variants_created` (compteurs agrégés
        sur le batch). `drifts` reste à `[]` quand le découplage a fait
        son travail.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [
        _validate_sill_or_head_item(kg, it, i, "sill_height_m")
        for i, it in enumerate(items)
    ]

    decoupled_count = 0
    auto_variants_created = 0

    if doc is None:
        # KG-only : _maybe_decouple en path KG (pas de Tx Revit nécessaire).
        if preserve_head:
            for spec in specs:
                info = _maybe_decouple(kg, doc, spec["llm_id"], new_sill_m=spec["sill_height_m"])
                if info["decoupled"]:
                    decoupled_count += 1
                    if info["auto_variant_created"]:
                        auto_variants_created += 1
        for spec in specs:
            kg.modify_node(spec["llm_id"], {"sill_height": spec["sill_height_m"]})
        out = bulk_setter_summary([], count=len(specs), revit_modified=False)
        out["decoupled_count"] = decoupled_count
        out["auto_variants_created"] = auto_variants_created
        return out

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
        # `_maybe_decouple` par item DANS la Tx Revit (cf. note solo).
        if preserve_head:
            for spec in specs:
                info = _maybe_decouple(kg, doc, spec["llm_id"], new_sill_m=spec["sill_height_m"])
                if info["decoupled"]:
                    decoupled_count += 1
                    if info["auto_variant_created"]:
                        auto_variants_created += 1
        for spec in specs:
            element = rp.get_element_or_raise(
                doc, kg.get_revit_id(spec["llm_id"]), spec["llm_id"],
                kind="opening",
            )
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

    out = bulk_setter_summary(drifts, count=len(specs), revit_modified=True)
    out["decoupled_count"] = decoupled_count
    out["auto_variants_created"] = auto_variants_created
    return out


@tool(name="openings_set_head_height_many", tier=1)
def set_head_height_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    preserve_sill: bool = True,
) -> Dict[str, Any]:
    """Règle le linteau (`INSTANCE_HEAD_HEIGHT_PARAM`) de N ouvertures en
    **une seule** Tx Revit + une seule Tx KG, en préservant par défaut
    la hauteur d'allège.

    **Auto-découple sill ↔ head (défaut, session 2026-05-12 c).** Pré-flight
    par item identique à `openings_set_sill_height_many`. Bascule auto
    sur un variant compatible si la cible diverge de la `opening_height`
    familiale. `preserve_sill=False` désactive le découplage.

    Concepts: linteau, head, lintel, bulk, batch, plusieurs, masse,
              découplage
    Phrases: "passe tous ces linteaux à 2.10 m", "uniformise les head",
             "bulk set head heights"
    Similar: openings_set_head_height, openings_set_sill_height_many,
             openings_set_type_many

    Args:
        items: liste de specs `{llm_id: str, head_height_m: float}`.
        preserve_sill: défaut True. Voir `openings_set_head_height`.

    Returns:
        Compact summary enrichie de `decoupled_count` et
        `auto_variants_created`.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    specs = [
        _validate_sill_or_head_item(kg, it, i, "head_height_m")
        for i, it in enumerate(items)
    ]

    decoupled_count = 0
    auto_variants_created = 0

    if doc is None:
        if preserve_sill:
            for spec in specs:
                info = _maybe_decouple(kg, doc, spec["llm_id"], new_head_m=spec["head_height_m"])
                if info["decoupled"]:
                    decoupled_count += 1
                    if info["auto_variant_created"]:
                        auto_variants_created += 1
        for spec in specs:
            kg.modify_node(spec["llm_id"], {"head_height": spec["head_height_m"]})
        out = bulk_setter_summary([], count=len(specs), revit_modified=False)
        out["decoupled_count"] = decoupled_count
        out["auto_variants_created"] = auto_variants_created
        return out

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
        if preserve_sill:
            for spec in specs:
                info = _maybe_decouple(kg, doc, spec["llm_id"], new_head_m=spec["head_height_m"])
                if info["decoupled"]:
                    decoupled_count += 1
                    if info["auto_variant_created"]:
                        auto_variants_created += 1
        for spec in specs:
            element = rp.get_element_or_raise(
                doc, kg.get_revit_id(spec["llm_id"]), spec["llm_id"],
                kind="opening",
            )
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

    out = bulk_setter_summary(drifts, count=len(specs), revit_modified=True)
    out["decoupled_count"] = decoupled_count
    out["auto_variants_created"] = auto_variants_created
    return out


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


# ----- Purge unused auto-variants (session 2026-05-12 d) --------------------


@tool(name="openings_purge_unused_variants", tier=1)
def purge_unused_variants(
    kg: ProjectKG,
    doc: Any,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Supprime les FamilyType auto-créés (marqueur `[auto h<NN>cm]`) qui
    ne sont plus référencés par aucune Door / Window vivante.

    Maintenance occasionnelle après plusieurs cycles d'auto-découple (cf.
    `openings_set_sill_height` / `_set_head_height`). Conservateur :
    purge UNIQUEMENT les variants reconnaissables par leur marqueur
    `[auto h<NN>cm]`. Un variant renommé manuellement par l'utilisateur
    (marqueur enlevé) est traité comme un type normal et préservé.

    Côté Revit : `doc.Delete(eid)` du FamilySymbol. Si Revit refuse
    (rare — l'usage est déjà vérifié côté KG), l'item reste dans
    `kept` avec la raison.

    Concepts: purge, cleanup, nettoyage, variants, auto, types orphelins
    Phrases: "nettoie les types orphelins", "purge les variants auto",
             "remove unused types", "delete orphan family types"
    Similar: openings_create_type_variant, openings_set_type

    Args:
        category: optionnel. "Doors" ou "Windows" pour filtrer. None
            (défaut) purge les deux.

    Returns:
        {"ok": bool, "scanned": int, "purged": int,
         "kept": [{"llm_id", "type_name", "reason"}, …],
         "revit_deleted": bool}
        `scanned` = nombre total de variants `[auto]` matchés (avant
        filtrage usage). `purged` = nombre effectivement supprimés.
        `kept` n'enumère que les variants conservés *pour une raison
        autre que "non-auto"* (typiquement `in_use`) — token compact.
    """
    if category is not None and category not in ("Doors", "Windows"):
        raise ValueError(
            "category must be 'Doors', 'Windows' or None, got {!r}".format(category)
        )

    # 1. Collecte les FamilyType auto, filtre par catégorie si demandée.
    candidates: List[str] = []
    for nid in kg.find_by_type("FamilyType"):
        attrs = kg.get_node(nid)
        if not _is_auto_variant(attrs):
            continue
        if category is not None and attrs.get("category") != category:
            continue
        candidates.append(nid)

    # 2. Sépare unused vs in_use.
    to_purge: List[str] = []
    kept: List[Dict[str, Any]] = []
    for nid in candidates:
        if _is_family_type_in_use(kg, nid):
            kept.append({
                "llm_id": nid,
                "type_name": kg.get_node(nid).get("type_name"),
                "reason": "in_use",
            })
        else:
            to_purge.append(nid)

    if doc is None:
        # KG-only : soft-delete les unused, pas de Revit-side.
        for nid in to_purge:
            kg.soft_delete(nid)
        return {
            "ok": True,
            "scanned": len(candidates),
            "purged": len(to_purge),
            "kept": kept,
            "revit_deleted": False,
        }

    # 3. Branche Revit : doc.Delete chaque FamilySymbol unused.
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    purged: List[str] = []
    with rp.transaction(doc, "openings.purge_unused_variants"):
        for nid in to_purge:
            eid_raw = kg.get_revit_id(nid)
            if eid_raw is None:
                # KG-only variant (pas de binding Revit) — soft-delete KG
                # uniquement, pas d'erreur.
                kg.soft_delete(nid)
                purged.append(nid)
                continue
            try:
                doc.Delete(ElementId(eid_raw))
                kg.soft_delete(nid)
                purged.append(nid)
            except Exception as exc:  # noqa: BLE001
                # Revit refuse la suppression (rare : usage caché par un
                # élément hors KG, par exemple). On garde le variant
                # côté KG et on remonte l'info au LLM.
                kept.append({
                    "llm_id": nid,
                    "type_name": kg.get_node(nid).get("type_name"),
                    "reason": "revit_refused_delete: {}".format(exc),
                })

    return {
        "ok": True,
        "scanned": len(candidates),
        "purged": len(purged),
        "kept": kept,
        "revit_deleted": True,
    }
