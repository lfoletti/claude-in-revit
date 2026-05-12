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


def _find_floor_plan_view_family_type(doc: Any) -> Any:
    """Renvoie le premier `ViewFamilyType` de famille FloorPlan, ou None.

    Pas de cache : itération O(N) sur les ViewFamilyType (≤ 10 dans un
    projet typique), négligeable. Appelé une fois par création de Level.

    Côté caller : si None, on ne génère pas de plan d'étage et on
    remonte un warning au LLM (`floor_plan_created: False, reason:
    "no FloorPlan ViewFamilyType in this project"`). Cas pathologique
    — un template Revit standard a toujours au moins un FloorPlan VFT.
    """
    from Autodesk.Revit.DB import FilteredElementCollector, ViewFamily, ViewFamilyType
    for vft in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if vft.ViewFamily == ViewFamily.FloorPlan:
            return vft
    return None


def _create_floor_plan_for_level(doc: Any, level: Any) -> Optional[int]:
    """Crée un FloorPlan ViewPlan pour `level`. Caller *déjà* dans une
    `rp.transaction` ouverte.

    Renvoie le `revit_id` du nouveau plan, ou None si aucun
    ViewFamilyType FloorPlan disponible dans le doc (cas extrême).

    Le ViewPlan n'est PAS bindé au KG en V0 — les vues n'ont pas de
    schéma node KG (deferred V1 via `catalog_list_views`). C'est un
    side-effect UX pur, comme `set_llm_id_on_element` sur les autres
    objets.
    """
    from Autodesk.Revit.DB import ViewPlan
    vft = _find_floor_plan_view_family_type(doc)
    if vft is None:
        return None
    view = ViewPlan.Create(doc, vft.Id, level.Id)
    # Le nom auto-généré par Revit reflète déjà le Level (cf. template
    # Revit standard). On laisse Revit nommer pour rester cohérent
    # avec l'UX du ruban.
    return int(view.Id.Value)


# ----- Tools -------------------------------------------------------------


@tool(name="levels_create", tier=1)
def create(
    kg: ProjectKG,
    doc: Any,
    name: str,
    elevation_m: float,
    create_floor_plan: bool = True,
) -> Dict[str, Any]:
    """Crée un nouveau niveau à l'altitude donnée, et **par défaut un
    plan d'étage (FloorPlan)** associé.

    Pourquoi le flag : `Level.Create(doc, elev)` côté API Revit crée
    seulement le Level, *pas* la vue Plan d'étage (contrairement à l'UI
    ruban). Sans `create_floor_plan=True`, le nouveau niveau apparaît
    en élévation et en arborescence Vue, mais pas dans la liste des
    Plans d'étage. C'était le bug runtime UX du 2026-05-12. Le défaut
    True restitue le comportement attendu de l'UI Revit.

    Concepts: niveau, level, étage, étages, plan d'étage, élévation,
              création, FloorPlan, vue
    Phrases: "ajoute un niveau", "crée un étage", "create a level",
             "nouveau niveau à X m"
    Similar: levels_create_floor_plan, levels_set_elevation,
             levels_set_name, catalog_list_levels

    Args:
        name: nom du niveau (ex: "N01", "Étage 1", "Toiture"). Unique
            dans le projet — Revit refuse les doublons.
        elevation_m: altitude en mètres (origine = niveau 0 du projet).
        create_floor_plan: si True (défaut), crée aussi un `ViewPlan`
            FloorPlan associé. Si False, seul le Level est créé (cas
            rare : niveaux structurels ou bornes sans vue plan).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None,
         "name": str, "elevation_m": float,
         "floor_plan_created": bool, "floor_plan_revit_id": int | None,
         "floor_plan_note": str | None}
        `floor_plan_note` est posé si la création du plan a été
        sautée (template Revit sans ViewFamilyType FloorPlan, cas
        exotique) — le LLM peut alerter l'utilisateur.
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
            "floor_plan_created": False,
            "floor_plan_revit_id": None,
            "floor_plan_note": None,
        }

    from .. import kg_sync, revit_primitives as rp
    from Autodesk.Revit.DB import Level

    elev_ft = rp.meters_to_internal(elev)

    revit_id: Optional[int] = None
    llm_id_out: Optional[str] = None
    floor_plan_revit_id: Optional[int] = None
    floor_plan_note: Optional[str] = None

    with rp.transaction(doc, "levels.create"):
        # Static factory: `Level.Create(doc, elevation_ft)` returns the
        # new Level instance. Auto-generated name like "Level 3" — we
        # rename afterward via the writable `Name` property.
        level = Level.Create(doc, elev_ft)
        revit_id = int(level.Id.Value)
        level.Name = new_name

        llm_id_out = _record_in_kg(kg, name=new_name, elevation=elev)
        kg.set_revit_id(llm_id_out, revit_id)
        stamp_llm_id(level, llm_id_out)
        kg_sync.refresh_node_from_revit(kg, doc, llm_id_out)

        # Création du FloorPlan dans la même Tx Revit (rollback symétrique
        # si la suite échoue). Side-effect UX, pas bindé au KG (vues =
        # V1, cf. dette catalog_list_views).
        if create_floor_plan:
            floor_plan_revit_id = _create_floor_plan_for_level(doc, level)
            if floor_plan_revit_id is None:
                floor_plan_note = (
                    "Aucun ViewFamilyType FloorPlan trouvé dans le template "
                    "Revit — le plan d'étage n'a pas été créé. Tu peux le "
                    "générer manuellement via Vue → Plan d'étage."
                )

    refreshed = kg.get_node(llm_id_out)
    return {
        "ok": True,
        "llm_id": llm_id_out,
        "revit_id": revit_id,
        "name": refreshed.get("name", new_name),
        "elevation_m": round(float(refreshed.get("elevation", elev)), 3),
        "floor_plan_created": floor_plan_revit_id is not None,
        "floor_plan_revit_id": floor_plan_revit_id,
        "floor_plan_note": floor_plan_note,
    }


@tool(name="levels_create_floor_plan", tier=1)
def create_floor_plan(
    kg: ProjectKG,
    doc: Any,
    llm_id: str,
) -> Dict[str, Any]:
    """Crée un plan d'étage (FloorPlan ViewPlan) pour un niveau existant.

    Utile pour réparer un niveau créé sans plan (ex : niveau venant d'un
    `levels_create` avec `create_floor_plan=False`, ou niveau pré-existant
    importé qui n'a pas son plan). Idempotent côté API Revit : appeler
    deux fois génère deux vues (Revit n'a pas de "FindOrCreate"
    natif). Caller responsable d'éviter les doublons s'il itère.

    Concepts: niveau, plan d'étage, vue, FloorPlan, ViewPlan, création
    Phrases: "ajoute un plan d'étage pour ce niveau", "crée la vue
             plan", "fais apparaître le plan d'étage", "create floor
             plan view"
    Similar: levels_create, catalog_list_levels

    Args:
        llm_id: llm_id d'un Level vivant.

    Returns:
        {"ok": bool, "llm_id": str, "floor_plan_revit_id": int | None,
         "note": str | None}
    """
    _require_live_level(kg, llm_id)

    if doc is None:
        # Pas de Revit en main — opération no-op KG (les vues ne sont
        # pas modélisées dans le KG en V0). Retour cohérent avec doc-aware.
        return {
            "ok": True,
            "llm_id": llm_id,
            "floor_plan_revit_id": None,
            "note": "doc is None — no Revit view created (KG-only path).",
        }

    eid_raw = kg.get_revit_id(llm_id)
    if eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(llm_id)
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    eid = ElementId(eid_raw)
    floor_plan_revit_id: Optional[int] = None
    note: Optional[str] = None
    with rp.transaction(doc, "levels.create_floor_plan"):
        level = doc.GetElement(eid)
        floor_plan_revit_id = _create_floor_plan_for_level(doc, level)
        if floor_plan_revit_id is None:
            note = (
                "Aucun ViewFamilyType FloorPlan trouvé dans le template "
                "Revit. Le plan n'a pas été créé."
            )

    return {
        "ok": True,
        "llm_id": llm_id,
        "floor_plan_revit_id": floor_plan_revit_id,
        "note": note,
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
