"""columns.py — création et inventaire des poteaux (architecturaux + structurels).

Suit le pattern `walls.py` (Phase 8 / 11) : doc-aware, branche Revit qui
appelle `NewFamilyInstance`, branche KG-only pour CLI / tests. La hauteur
du poteau est posée via `FAMILY_TOP_LEVEL_OFFSET_PARAM` (mode "unconnected
height" — top level = base level, top offset = height). Les familles de
poteaux qui n'exposent pas ce paramètre lèvent un message actionnable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from ._helpers import bulk_summary, stamp_llm_id
from ..llm_protocol import tool
from ..project_kg import ProjectKG


@tool(name="catalog_list_column_types", tier=1)
def list_column_types(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les types de poteaux disponibles (architectural + structural).

    Concepts: type de poteau, column type, family symbol, inventaire
    Phrases: "quels types de poteau", "list column types", "catalogue poteaux"
    Similar: catalog_list_wall_types, catalog_list_columns

    Args:
        (aucun)

    Returns:
        {"column_types": [{llm_id, family_name, type_name, kind}, ...]}
        - `kind` vaut `"architectural"` ou `"structural"`.
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("ColumnType"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "family_name": attrs.get("family_name"),
            "type_name": attrs.get("type_name"),
            "kind": attrs.get("kind"),
        })
    return {"column_types": out}


@tool(name="catalog_list_columns", tier=1)
def list_columns(kg: ProjectKG) -> Dict[str, List[Dict[str, Any]]]:
    """Liste tous les poteaux vivants du projet avec position et hauteur.

    Concepts: poteau, column, inventaire, géométrie, position
    Phrases: "liste les poteaux", "quels poteaux", "all columns",
             "où sont les poteaux"
    Similar: catalog_list_walls, catalog_list_column_types, columns_create

    Args:
        (aucun)

    Returns:
        {"columns": [{llm_id, level_ref, type_ref, position, height, kind}, ...]}
        - `position` est `[x, y]` en mètres dans le plan du niveau.
        - `height` est la hauteur du poteau en mètres (offset top depuis le
          base level).
    """
    out: List[Dict[str, Any]] = []
    for nid in kg.find_by_type("Column"):
        attrs = kg.get_node(nid)
        out.append({
            "llm_id": nid,
            "level_ref": attrs.get("level_ref"),
            "type_ref": attrs.get("type_ref"),
            "position": attrs.get("position"),
            "height": attrs.get("height"),
            "kind": attrs.get("kind"),
        })
    return {"columns": out}


def _default_column_height(kg: ProjectKG, level_ref: str) -> float:
    """Story height: next level's elevation minus this level's, in metres.

    Raised by `columns_create` when no explicit height is given. If the
    base level is the top of the building (no level above), refuse rather
    than guess — the agent should ask the user for an explicit value.
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
            "explicitly when the column sits on the top level.".format(
                level_ref, base_elev,
            )
        )
    return min(above) - base_elev


def _record_in_kg(
    kg: ProjectKG,
    level_ref: str,
    type_ref: str,
    position: List[float],
    height: float,
    kind: str,
) -> str:
    llm_id = kg.add_node("Column", {
        "level_ref": level_ref,
        "type_ref": type_ref,
        "position": list(position),
        "height": height,
        "kind": kind,
    })
    kg.add_edge(llm_id, level_ref, "at_level")
    kg.add_edge(llm_id, type_ref, "is_type")
    return llm_id


@tool(name="columns_create", tier=1)
def create(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    column_type_ref: str,
    position: List[float],
    height: Optional[float] = None,
) -> Dict[str, Any]:
    """Crée un poteau au point donné sur un niveau.

    Le type (`column_type_ref`) détermine si le poteau est architectural
    ou structural — le `kind` est lu depuis le type, l'utilisateur n'a
    pas à le préciser. La hauteur est appliquée via
    `FAMILY_TOP_LEVEL_OFFSET_PARAM` (top level = base level, offset =
    height) ; ça donne un poteau « unconnected height » indépendant
    d'un éventuel top level supérieur.

    **Hauteur par défaut** : si `height` n'est pas fourni, on prend la
    hauteur d'étage = différence d'élévation entre le niveau cible et le
    niveau juste au-dessus. Si le niveau cible est le plus haut du
    projet, le tool lève une erreur explicite — il faut alors fournir
    `height` explicitement.

    Concepts: poteau, création, géométrie, column
    Phrases: "pose un poteau", "place un poteau", "ajoute une colonne",
             "create a column"
    Similar: walls_create, columns_delete, columns_move

    Args:
        level_ref: llm_id du Level cible (via catalog_list_levels).
        column_type_ref: llm_id du ColumnType (via catalog_list_column_types).
        position: [x, y] en mètres dans le plan du niveau.
        height: hauteur du poteau en mètres. Optionnel — par défaut
            on prend la hauteur d'étage (next_level.elevation -
            this_level.elevation).

    Returns:
        {"ok": bool, "llm_id": str, "revit_id": int | None, "kind": str,
         "height_m": float, "height_default": bool}
        - `height_default` indique si la hauteur vient du défaut
          (hauteur d'étage) ou a été fournie par l'appelant. Utile
          pour signaler à l'utilisateur quand le tool a choisi pour lui.
        **L'`llm_id` retourné est l'unique source de vérité pour identifier
        ce poteau dans toute opération ultérieure ; ne JAMAIS deviner
        par numérotation séquentielle.**
    """
    if not kg.has_node(level_ref):
        raise ValueError("Unknown level_ref: {}".format(level_ref))
    if not kg.has_node(column_type_ref):
        raise ValueError("Unknown column_type_ref: {}".format(column_type_ref))
    type_node = kg.get_node(column_type_ref)
    if type_node.get("_type") != "ColumnType":
        raise ValueError(
            "column_type_ref {} is not a ColumnType (got {})".format(
                column_type_ref, type_node.get("_type"),
            )
        )
    kind = type_node.get("kind", "architectural")

    height_default = height is None
    if height_default:
        height = _default_column_height(kg, level_ref)

    if doc is None:
        # Hors-Revit — KG seulement.
        llm_id = _record_in_kg(kg, level_ref, column_type_ref, position, height, kind)
        return {
            "ok": True,
            "llm_id": llm_id,
            "revit_id": None,
            "kind": kind,
            "height_m": height,
            "height_default": height_default,
        }

    # Revit-backed path. Check bindings BEFORE the Revit imports so a
    # hors-Revit sentinel doc gets a ValueError, not an ImportError.
    level_eid_raw = kg.get_revit_id(level_ref)
    type_eid_raw = kg.get_revit_id(column_type_ref)
    if level_eid_raw is None:
        raise ValueError(
            "Level {} has no Revit binding — run Refresh KG.".format(level_ref)
        )
    if type_eid_raw is None:
        raise ValueError(
            "ColumnType {} has no Revit binding — run Refresh KG.".format(
                column_type_ref,
            )
        )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import (
        BuiltInParameter,
        ElementId,
        XYZ,
    )
    from Autodesk.Revit.DB.Structure import StructuralType

    level_eid = ElementId(level_eid_raw)
    type_eid = ElementId(type_eid_raw)
    pt = XYZ(
        rp.meters_to_internal(position[0]),
        rp.meters_to_internal(position[1]),
        0.0,
    )
    structural_type = (
        StructuralType.Column if kind == "structural" else StructuralType.NonStructural
    )

    revit_id: Optional[int] = None
    with rp.transaction(doc, "columns.create"):
        symbol = doc.GetElement(type_eid)
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()
        level_elem = doc.GetElement(level_eid)
        # NewFamilyInstance overload (XYZ, FamilySymbol, Level, StructuralType).
        instance = doc.Create.NewFamilyInstance(
            pt, symbol, level_elem, structural_type,
        )
        # Set the height via the top-level-offset trick: top level = base
        # level, top offset = height. Gives an "unconnected" column whose
        # top doesn't track any superior level.
        top_lvl_param = instance.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
        top_off_param = instance.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
        if top_lvl_param is None or top_off_param is None:
            raise ValueError(
                "Column family {} doesn't expose FAMILY_TOP_LEVEL_* params; "
                "cannot set height directly. Place the column then adjust "
                "manually.".format(type_node.get("family_name"))
            )
        top_lvl_param.Set(level_eid)
        top_off_param.Set(rp.meters_to_internal(height))

        revit_id = int(instance.Id.Value)
        llm_id = _record_in_kg(
            kg, level_ref, column_type_ref, position, height, kind,
        )
        kg.set_revit_id(llm_id, revit_id)
        stamp_llm_id(instance, llm_id)
        # Read-back discipline (2026-05-11 session 5) : NewFamilyInstance
        # may snap the column to the nearest grid intersection or to a
        # locked alignment. Mirror Revit reality so subsequent queries
        # don't trust the requested position blindly.
        from .. import kg_sync as _kg_sync
        _kg_sync.refresh_node_from_revit(kg, doc, llm_id)

    actual = kg.get_node(llm_id)
    return {
        "ok": True,
        "llm_id": llm_id,
        "revit_id": revit_id,
        "kind": kind,
        "height_m": round(actual.get("height", height), 3),
        "position": actual.get("position"),
        "height_default": height_default,
    }


def _validate_item(kg: ProjectKG, item: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Pre-flight one item from columns_create_many. Raises ValueError on bad input.

    Returns a normalised dict with `level_ref`, `column_type_ref`, `position`,
    `height_or_none`, `kind` (derived from the type), ready for the bulk loop.
    """
    if not isinstance(item, dict):
        raise ValueError("items[{}] must be a dict, got {}".format(index, type(item).__name__))
    level_ref = item.get("level_ref")
    column_type_ref = item.get("column_type_ref")
    position = item.get("position")
    height = item.get("height")  # may be None.

    if not isinstance(level_ref, str) or not kg.has_node(level_ref):
        raise ValueError("items[{}]: invalid level_ref {!r}".format(index, level_ref))
    if not isinstance(column_type_ref, str) or not kg.has_node(column_type_ref):
        raise ValueError("items[{}]: invalid column_type_ref {!r}".format(index, column_type_ref))
    type_node = kg.get_node(column_type_ref)
    if type_node.get("_type") != "ColumnType":
        raise ValueError(
            "items[{}]: column_type_ref {} is a {}, not a ColumnType".format(
                index, column_type_ref, type_node.get("_type"),
            )
        )
    if not isinstance(position, list) or len(position) != 2:
        raise ValueError(
            "items[{}]: position must be [x, y] in metres".format(index)
        )
    if height is not None and not isinstance(height, (int, float)):
        raise ValueError("items[{}]: height must be a number or omitted".format(index))

    return {
        "level_ref": level_ref,
        "column_type_ref": column_type_ref,
        "position": [float(position[0]), float(position[1])],
        "height_or_none": float(height) if height is not None else None,
        "kind": type_node.get("kind", "architectural"),
    }


@tool(name="columns_create_many", tier=1)
def create_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Crée N poteaux en **une seule** transaction Revit + une seule transaction
    KG. Préférer ce tool dès qu'on a plusieurs poteaux à créer en une fois —
    typique speed-up 10-100× vs N appels séparés à `columns_create` parce
    que :
    - Le snapshot KG (deepcopy) est pris **une seule fois**, pas N fois.
    - Revit ne regenère qu'au commit final (pas après chaque
      `NewFamilyInstance`).
    - Le KG est persisté une seule fois en fin de bulk.

    Transactionnel : si un item échoue dans le batch, **aucune création**
    n'est commitée — l'utilisateur reçoit la liste exacte des erreurs et
    peut corriger puis relancer.

    Concepts: poteau, bulk, batch, plusieurs, série, grille, columns
    Phrases: "pose plusieurs poteaux", "crée une grille de poteaux",
             "100 poteaux", "tous ces poteaux", "batch columns"
    Similar: columns_create, walls_create

    Args:
        items: liste de specs de poteaux. Chaque entrée est un dict :
            - `level_ref` (str, requis) : llm_id du Level.
            - `column_type_ref` (str, requis) : llm_id du ColumnType.
            - `position` (list[float], requis) : [x, y] en mètres.
            - `height` (float, optionnel) : hauteur en mètres. Défaut =
              hauteur d'étage (voir `columns_create`).

    Returns:
        Réponse compacte de bulk (`lib.tools._helpers.bulk_summary`) :
        - `{ok, count, llm_ids: [...]}` si batch petit (≤ 8).
        - `{ok, count, first_llm_id, last_llm_id, contiguous: True, note}`
          si batch large et llm_ids contigus (cas usuel).
        - `{ok, count, llm_ids: [...], note}` si batch large non-contigu.
        Détails par item via `catalog_list_columns` ou `query_get_node`.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")

    # Validation upfront — refuse à 100 % avant toute mutation.
    specs = [_validate_item(kg, item, i) for i, item in enumerate(items)]
    # Default heights resolved after upfront validation so a single missing
    # `height` with no level-above gives a clean error before we touch Revit.
    for i, spec in enumerate(specs):
        if spec["height_or_none"] is None:
            try:
                spec["height_or_none"] = _default_column_height(kg, spec["level_ref"])
            except ValueError as exc:
                raise ValueError("items[{}]: {}".format(i, exc))

    if doc is None:
        # Hors-Revit — KG seulement.
        llm_ids: List[str] = []
        for spec in specs:
            llm_ids.append(_record_in_kg(
                kg,
                spec["level_ref"],
                spec["column_type_ref"],
                spec["position"],
                spec["height_or_none"],
                spec["kind"],
            ))
        return bulk_summary(llm_ids)

    # Revit-backed bulk path. All bindings resolved upfront before the
    # Revit imports — so a hors-Revit sentinel doc fails cleanly with
    # ValueError ("no Revit binding") instead of ImportError.
    for i, spec in enumerate(specs):
        if kg.get_revit_id(spec["level_ref"]) is None:
            raise ValueError(
                "items[{}]: Level {} has no Revit binding — run Refresh KG.".format(
                    i, spec["level_ref"],
                )
            )
        if kg.get_revit_id(spec["column_type_ref"]) is None:
            raise ValueError(
                "items[{}]: ColumnType {} has no Revit binding — run Refresh KG.".format(
                    i, spec["column_type_ref"],
                )
            )

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import (
        BuiltInParameter,
        ElementId,
        XYZ,
    )
    from Autodesk.Revit.DB.Structure import StructuralType

    llm_ids: List[str] = []
    with rp.transaction(doc, "columns.create_many"):
        # Activate every distinct ColumnType symbol once, then regenerate
        # once — `NewFamilyInstance` then sees them ready. Without this,
        # the first placement of an inactive symbol regens once *per*
        # placement, hammering performance.
        activated_symbols = set()
        for spec in specs:
            type_eid_raw = kg.get_revit_id(spec["column_type_ref"])
            if type_eid_raw in activated_symbols:
                continue
            symbol = doc.GetElement(ElementId(type_eid_raw))
            if not symbol.IsActive:
                symbol.Activate()
            activated_symbols.add(type_eid_raw)
        if activated_symbols:
            doc.Regenerate()

        for spec in specs:
            level_eid_raw = kg.get_revit_id(spec["level_ref"])
            type_eid_raw = kg.get_revit_id(spec["column_type_ref"])
            level_eid = ElementId(level_eid_raw)
            type_eid = ElementId(type_eid_raw)
            pt = XYZ(
                rp.meters_to_internal(spec["position"][0]),
                rp.meters_to_internal(spec["position"][1]),
                0.0,
            )
            structural_type = (
                StructuralType.Column if spec["kind"] == "structural"
                else StructuralType.NonStructural
            )
            symbol = doc.GetElement(type_eid)
            level_elem = doc.GetElement(level_eid)
            instance = doc.Create.NewFamilyInstance(
                pt, symbol, level_elem, structural_type,
            )
            top_lvl_param = instance.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
            top_off_param = instance.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
            if top_lvl_param is None or top_off_param is None:
                raise ValueError(
                    "Column family for {} doesn't expose FAMILY_TOP_LEVEL_* "
                    "params; cannot set height".format(spec["column_type_ref"])
                )
            top_lvl_param.Set(level_eid)
            top_off_param.Set(rp.meters_to_internal(spec["height_or_none"]))

            revit_id = int(instance.Id.Value)
            llm_id = _record_in_kg(
                kg,
                spec["level_ref"],
                spec["column_type_ref"],
                spec["position"],
                spec["height_or_none"],
                spec["kind"],
            )
            kg.set_revit_id(llm_id, revit_id)
            stamp_llm_id(instance, llm_id)
            llm_ids.append(llm_id)
        # Read-back discipline : mirror live Revit position/height on
        # every column post-creation.
        from .. import kg_sync as _kg_sync
        for nid in llm_ids:
            _kg_sync.refresh_node_from_revit(kg, doc, nid)

    return bulk_summary(llm_ids)


@tool(name="columns_create_grid", tier=1)
def create_grid(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    column_type_ref: str,
    origin: List[float],
    step_x: float,
    step_y: float,
    count_x: int,
    count_y: int,
    height: Optional[float] = None,
) -> Dict[str, Any]:
    """Crée une grille rectangulaire axis-aligned de poteaux.

    Pattern tool : le LLM passe **les paramètres de la grille** et Python
    génère les `count_x × count_y` positions localement avant de les
    transmettre à la mécanique bulk (`columns_create_many` interne).
    Économise ~50-100× de tokens de sortie vs énumérer les items à la
    main pour une grille de 100+ poteaux.

    Une fois les positions calculées, l'exécution profite du même
    speed-up Revit-side que `columns_create_many` (une seule transaction
    Revit + un seul snapshot KG + un seul persist disque).

    Concepts: grille, grid, trame, poteaux, batch, pattern, duplication
    Phrases: "trame de poteaux", "grille 10x10 de poteaux",
             "pose des poteaux en quinconce", "place 100 poteaux espacés",
             "column grid"
    Similar: columns_create_many, columns_create

    Args:
        level_ref: llm_id du Level cible (via catalog_list_levels).
        column_type_ref: llm_id du ColumnType (via catalog_list_column_types).
        origin: [x, y] en mètres — position du poteau de coin (i=0, j=0).
        step_x: pas en x en mètres (vers l'est si project north = +Y).
        step_y: pas en y en mètres.
        count_x: nombre de poteaux selon x (≥ 1).
        count_y: nombre de poteaux selon y (≥ 1).
        height: hauteur uniforme en mètres. Optionnel — par défaut hauteur
            d'étage. Si tu veux des hauteurs variables, passe par
            `columns_create_many` avec une liste d'items.

    Returns:
        Même schéma compact que `columns_create_many` :
        `{ok, count, ...}` avec soit `llm_ids` (petit batch) soit
        `first_llm_id`/`last_llm_id`/`contiguous: True` (grand batch).
        Détails par item via `catalog_list_columns` / `query_get_node`.
    """
    if not isinstance(count_x, int) or count_x < 1:
        raise ValueError("count_x must be a positive integer (got {})".format(count_x))
    if not isinstance(count_y, int) or count_y < 1:
        raise ValueError("count_y must be a positive integer (got {})".format(count_y))
    if not isinstance(origin, list) or len(origin) != 2:
        raise ValueError("origin must be [x, y] in metres")

    ox, oy = float(origin[0]), float(origin[1])
    items: List[Dict[str, Any]] = []
    for i in range(count_x):
        for j in range(count_y):
            items.append({
                "level_ref": level_ref,
                "column_type_ref": column_type_ref,
                "position": [ox + i * step_x, oy + j * step_y],
                "height": height,
            })
    # Reuse the bulk path. Validation, defaulting, single-transaction
    # semantics, and the unified return shape all come from there.
    return create_many(kg=kg, doc=doc, items=items)


@tool(name="columns_create_grid_irregular", tier=1)
def create_grid_irregular(
    kg: ProjectKG,
    doc: Any,
    level_ref: str,
    column_type_ref: str,
    origin: List[float],
    x_spacings: List[float],
    y_spacings: List[float],
    height: Optional[float] = None,
) -> Dict[str, Any]:
    """Crée une trame de poteaux à *entre-axes variables* sur X et Y.

    L'utilisateur fournit les listes d'entre-axes (gaps entre poteaux
    consécutifs), pas les coordonnées : N entre-axes en X ⇒ N+1 poteaux
    en X (idem pour Y). Le total est donc `(len(x_spacings)+1) ×
    (len(y_spacings)+1)`. Python fait la somme cumulée localement et
    appelle la mécanique bulk — l'LLM passe deux courtes listes, pas
    150+ coordonnées.

    Pour une trame **uniforme**, préférer `columns_create_grid`
    (count + step) — plus concis. Pour des positions complètement
    arbitraires, voir `columns_create_many`.

    Concepts: trame, grille, entre-axes, irrégulière, variable, axis grid
    Phrases: "trame 8-8-8-5-8-8-8 en X", "entre-axes variables",
             "réseau irrégulier", "axis grid", "irregular column grid"
    Similar: columns_create_grid, columns_create_many

    Args:
        level_ref: llm_id du Level (via catalog_list_levels).
        column_type_ref: llm_id du ColumnType (via catalog_list_column_types).
        origin: [x, y] en mètres — position du poteau de coin (i=0, j=0).
        x_spacings: liste des entre-axes en x, en mètres. Liste vide ⇒
            un seul poteau en x (à l'abscisse origin[0]).
        y_spacings: liste des entre-axes en y, en mètres. Liste vide ⇒
            un seul poteau en y.
        height: hauteur uniforme en mètres. Optionnel — défaut hauteur
            d'étage. Pour des hauteurs variables, passer par
            `columns_create_many`.

    Returns:
        Même schéma compact que `columns_create_many` —
        `bulk_summary` (range + count pour les grilles larges contiguës).
    """
    if not isinstance(origin, list) or len(origin) != 2:
        raise ValueError("origin must be [x, y] in metres")
    if not isinstance(x_spacings, list):
        raise ValueError("x_spacings must be a list of numbers")
    if not isinstance(y_spacings, list):
        raise ValueError("y_spacings must be a list of numbers")
    for k, dx in enumerate(x_spacings):
        if not isinstance(dx, (int, float)):
            raise ValueError("x_spacings[{}] must be a number, got {!r}".format(k, dx))
    for k, dy in enumerate(y_spacings):
        if not isinstance(dy, (int, float)):
            raise ValueError("y_spacings[{}] must be a number, got {!r}".format(k, dy))

    # Cumulative sums → absolute coordinates.
    xs: List[float] = [float(origin[0])]
    for dx in x_spacings:
        xs.append(xs[-1] + float(dx))
    ys: List[float] = [float(origin[1])]
    for dy in y_spacings:
        ys.append(ys[-1] + float(dy))

    items: List[Dict[str, Any]] = []
    for x in xs:
        for y in ys:
            items.append({
                "level_ref": level_ref,
                "column_type_ref": column_type_ref,
                "position": [x, y],
                "height": height,
            })
    return create_many(kg=kg, doc=doc, items=items)


# ----- DXF import : get-or-create ColumnType (placeholder DXF_COL_*) ---
#
# Phase 2d — création / réutilisation d'un `ColumnType` KG pour chaque
# paire `(family_name, type_name)` extraite d'un DXF (cf. `dwg_plan_
# columns.parse_column_block_name`).
#
# Stratégie alignée avec `DXF_WALL_<thk>cm` et `DXF_FLOOR_<thk>cm` :
# on crée un **placeholder générique** `DXF_COL_<famille>_<type>` en
# dupliquant un FamilySymbol de poteau générique du projet (le 1er
# trouvé dans les catégories `Columns` / `StructuralColumns`). Aucune
# assomption sur le matériau réel : HEA acier, poteau béton, bois, etc.
# encodés dans le nom mais matérialisés par une famille placeholder.
# L'user remappe vers les vraies familles après import (même flow que
# les types DXF_WALL_/DXF_FLOOR_).


def _dxf_column_type_name(family_name: str, type_name: str) -> str:
    """Forge le nom du ColumnType DXF placeholder : ``DXF_COL_<family>_<type>``.

    Le nom encode la métadonnée du block DXF original pour la traçabilité.
    Sanitize les caractères non-Revit-compatibles (``\\``, ``/``, ``:``,
    ``{``, ``}``, ``;``, ``<``, ``>``, ``?``, ``|``).
    """
    def _sanitize(s: str) -> str:
        forbidden = "\\/:;{}<>?|"
        return "".join(c if c not in forbidden else "_" for c in s).strip()
    return "DXF_COL_{}_{}".format(_sanitize(family_name), _sanitize(type_name))


def _find_dxf_column_type_in_kg_by_name(
    kg: ProjectKG, target_name: str,
) -> Optional[str]:
    """Cherche un ColumnType KG vivant dont le `type_name` (rebaptisé en
    DXF_COL_*) matche le placeholder cible."""
    for nid in kg.find_by_type("ColumnType"):
        attrs = kg.get_node(nid)
        if attrs.get("deleted_at_turn") is not None:
            continue
        if attrs.get("type_name") == target_name:
            return nid
    return None


def _find_generic_column_family_symbol(doc: Any):
    """Cherche un FamilySymbol de poteau dans le projet, n'importe lequel.

    Stratégie : le 1er FamilySymbol trouvé dans une catégorie
    `Columns` ou `StructuralColumns` (insensible à la casse, EN+FR).
    Préfère une catégorie `StructuralColumns` à `Columns` si les deux
    existent (poteau S-COLS = structurel par défaut).

    Retourne None si aucun poteau n'est chargé dans le projet (cas
    template vide — user doit charger au moins une famille).
    """
    from Autodesk.Revit.DB import FamilySymbol, FilteredElementCollector
    structural: Optional[Any] = None
    architectural: Optional[Any] = None
    for sym in FilteredElementCollector(doc).OfClass(FamilySymbol):
        try:
            cat = sym.Category
            if cat is None:
                continue
            cat_name = (cat.Name or "").lower()
            is_struct = (
                "structural column" in cat_name
                or "poteau" in cat_name and "structurel" in cat_name
                or "poteau structurel" in cat_name
                or "colonne structurel" in cat_name
            )
            is_arch = (
                "column" in cat_name and not is_struct
                or "poteau" in cat_name and not is_struct
                or "colonne" in cat_name and not is_struct
            )
            if is_struct and structural is None:
                structural = sym
            elif is_arch and architectural is None:
                architectural = sym
            if structural is not None and architectural is not None:
                break
        except Exception:  # noqa: BLE001
            continue
    return structural or architectural


def _create_dxf_column_type_kg_only(
    kg: ProjectKG, family_name: str, type_name: str, kind: str,
) -> str:
    """Crée un node KG ColumnType. Le `type_name` stocké est le nom
    placeholder (`DXF_COL_*`) ; `family_name` reste celui d'origine
    (= nom de la famille Revit du base placeholder)."""
    return kg.add_node("ColumnType", {
        "family_name": family_name,
        "type_name": type_name,
        "kind": kind,
    })


@tool(name="columns_get_or_create_dxf_type_many", tier=1)
def get_or_create_dxf_type_many(
    kg: ProjectKG,
    doc: Any,
    types: List[Dict[str, Any]],
    base_type_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Crée (ou réutilise) N ColumnType placeholder ``DXF_COL_<famille>_<type>``
    pour chaque paire `(family_name, type_name)` extraite d'un DXF.

    Use case : pendant `dwg_create_columns_many`, on extrait toutes les
    paires `(family_name, type_name)` distinctes apparaissant dans les
    plans (via `dwg_plan_columns.parse_column_block_name`). Ce tool
    crée un ColumnType placeholder pour chaque paire — pattern **strict-
    ement aligné avec `walls_get_or_create_dxf_type_many` et
    `floors_get_or_create_dxf_type_many`**.

    **Placeholder générique, pas de match sur familles réelles** : pour
    chaque paire, on **duplique un FamilySymbol de poteau générique**
    déjà chargé dans le projet (le 1er trouvé dans Columns/StructuralColumns).
    Le nouveau type est nommé `DXF_COL_<famille>_<type>` (e.g.
    `DXF_COL_Poteau HE-A_HEA160`, `DXF_COL_Poteau béton_30x30`,
    `DXF_COL_Poteau bois_BLC 200x200`). L'user remappe ensuite vers les
    vraies familles HEA acier / béton paramétrique / bois après import,
    comme pour les types DXF_WALL_*/DXF_FLOOR_*.

    **Aucune assomption sur le matériau** — même comportement pour HEA
    acier, béton, bois.

    Concepts: poteau, column type, dxf, import, phase 2d, placeholder,
              get or create, bulk, dxf_col, generic
    Phrases: "crée les types de poteaux DXF nécessaires",
             "get or create column types", "phase 2d types"
    Similar: walls_get_or_create_dxf_type_many,
             floors_get_or_create_dxf_type_many, columns_create_many

    Args:
        types: liste de dicts `{family_name, type_name, kind?}` :
            - `family_name` : nom famille Revit source DXF (e.g. "Poteau HE-A").
              Conservé comme métadonnée traçable dans le nom placeholder.
            - `type_name` : nom du type Revit source DXF (e.g. "HEA160").
            - `kind` (optionnel) : "structural" (défaut) ou "architectural".
        base_type_ref: llm_id d'un ColumnType template à dupliquer.
            Si None (défaut), cherche automatiquement un poteau générique
            chargé dans le projet (préférence structurel).

    Returns:
        ``{"ok": bool, "types": [{family_name, type_name, kind, llm_id,
            created, revit_id}, ...], "created_count": int,
            "reused_count": int}``
        - `type_name` dans le payload est le placeholder DXF_COL_<...>,
          pas le type_name d'entrée.
    """
    if not isinstance(types, list) or not types:
        raise ValueError("types must be a non-empty list")

    # Dédup par placeholder name.
    seen_names: Set[str] = set()
    unique_specs: List[Dict[str, Any]] = []
    for i, t in enumerate(types):
        if not isinstance(t, dict):
            raise ValueError("types[{}] must be a dict".format(i))
        family = t.get("family_name")
        type_ = t.get("type_name")
        kind = t.get("kind", "structural")
        width_m = t.get("width_m")
        depth_m = t.get("depth_m")
        if not isinstance(family, str) or not family.strip():
            raise ValueError(
                "types[{}]: family_name required (str)".format(i)
            )
        if not isinstance(type_, str) or not type_.strip():
            raise ValueError(
                "types[{}]: type_name required (str)".format(i)
            )
        if kind not in ("structural", "architectural"):
            raise ValueError(
                "types[{}]: kind must be structural|architectural, "
                "got {!r}".format(i, kind)
            )
        if width_m is not None and (not isinstance(width_m, (int, float)) or width_m <= 0):
            raise ValueError(
                "types[{}]: width_m must be a positive number or None".format(i)
            )
        if depth_m is not None and (not isinstance(depth_m, (int, float)) or depth_m <= 0):
            raise ValueError(
                "types[{}]: depth_m must be a positive number or None".format(i)
            )
        placeholder_name = _dxf_column_type_name(family.strip(), type_.strip())
        if placeholder_name in seen_names:
            continue
        seen_names.add(placeholder_name)
        unique_specs.append({
            "original_family": family.strip(),
            "original_type": type_.strip(),
            "placeholder_name": placeholder_name,
            "kind": kind,
            "width_m": float(width_m) if width_m is not None else None,
            "depth_m": float(depth_m) if depth_m is not None else None,
        })

    out: List[Dict[str, Any]] = []
    created_count = 0
    reused_count = 0

    # KG-only path (doc=None) : pas de duplication Revit, juste un node KG.
    if doc is None:
        for spec in unique_specs:
            existing = _find_dxf_column_type_in_kg_by_name(
                kg, spec["placeholder_name"],
            )
            if existing is not None:
                node = kg.get_node(existing)
                out.append({
                    "family_name": node.get("family_name", spec["original_family"]),
                    "type_name": spec["placeholder_name"],
                    "kind": node.get("kind", spec["kind"]),
                    "llm_id": existing,
                    "created": False,
                    "revit_id": None,
                })
                reused_count += 1
            else:
                nid = _create_dxf_column_type_kg_only(
                    kg, spec["original_family"], spec["placeholder_name"],
                    spec["kind"],
                )
                out.append({
                    "family_name": spec["original_family"],
                    "type_name": spec["placeholder_name"],
                    "kind": spec["kind"],
                    "llm_id": nid,
                    "created": True,
                    "revit_id": None,
                })
                created_count += 1
        return {
            "ok": True, "types": out,
            "created_count": created_count, "reused_count": reused_count,
        }

    # Revit-backed path : duplique FamilySymbol générique et bind.
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    # Résoud le base FamilySymbol une fois.
    if base_type_ref is not None:
        if not kg.has_node(base_type_ref):
            raise ValueError("Unknown base_type_ref: {}".format(base_type_ref))
        base_eid_raw = kg.get_revit_id(base_type_ref)
        if base_eid_raw is None:
            raise ValueError(
                "base_type_ref {} has no Revit binding".format(base_type_ref)
            )
        base_sym = doc.GetElement(ElementId(base_eid_raw))
    else:
        base_sym = _find_generic_column_family_symbol(doc)
        if base_sym is None:
            raise ValueError(
                "Aucun FamilySymbol de poteau (Columns / StructuralColumns) "
                "n'est chargé dans ce projet Revit. Charge au moins une "
                "famille de poteau générique (.rfa) avant l'import — "
                "elle servira de placeholder pour les DXF_COL_*."
            )

    # Auto-détecte les params b/h une fois sur le base symbol.
    base_wp_name, base_wp = _find_dimension_param(
        base_sym, _BA_COL_WIDTH_PARAM_CANDIDATES,
    )
    base_dp_name, base_dp = _find_dimension_param(
        base_sym, _BA_COL_DEPTH_PARAM_CANDIDATES,
    )

    with rp.transaction(doc, "columns.get_or_create_dxf_type_many"):
        for spec in unique_specs:
            existing = _find_dxf_column_type_in_kg_by_name(
                kg, spec["placeholder_name"],
            )
            if existing is not None:
                # Valider que le binding Revit est encore vivant.
                rid = kg.get_revit_id(existing)
                rid_alive = False
                if rid is not None:
                    try:
                        rid_alive = doc.GetElement(ElementId(rid)) is not None
                    except Exception:  # noqa: BLE001
                        rid_alive = False
                if rid_alive:
                    node = kg.get_node(existing)
                    out.append({
                        "family_name": node.get(
                            "family_name", spec["original_family"],
                        ),
                        "type_name": spec["placeholder_name"],
                        "kind": node.get("kind", spec["kind"]),
                        "llm_id": existing,
                        "created": False,
                        "revit_id": rid,
                    })
                    reused_count += 1
                    continue
                # Stale binding : on tombe en création fraîche ci-dessous.
            # Duplique le base FamilySymbol avec le placeholder name.
            new_sym = base_sym.Duplicate(spec["placeholder_name"])
            # Set les params dimensionnels si fournis (bbox bloc DXF).
            # Sinon, le placeholder garde les params par défaut du base
            # symbol (e.g. 30×30 cm) — l'user remappe manuellement.
            dims_applied = False
            if (spec.get("width_m") is not None
                    and spec.get("depth_m") is not None
                    and base_wp is not None and base_dp is not None):
                from .. import revit_primitives as rp_
                new_wp = new_sym.LookupParameter(base_wp_name)
                new_dp = new_sym.LookupParameter(base_dp_name)
                if new_wp is not None and new_dp is not None:
                    new_wp.Set(rp_.meters_to_internal(spec["width_m"]))
                    new_dp.Set(rp_.meters_to_internal(spec["depth_m"]))
                    dims_applied = True
            new_rid = int(new_sym.Id.Value)
            nid = _create_dxf_column_type_kg_only(
                kg, spec["original_family"], spec["placeholder_name"],
                spec["kind"],
            )
            kg.set_revit_id(nid, new_rid)
            out.append({
                "family_name": spec["original_family"],
                "type_name": spec["placeholder_name"],
                "kind": spec["kind"],
                "llm_id": nid,
                "created": True,
                "revit_id": new_rid,
                "dimensions_applied": dims_applied,
                "width_m": spec.get("width_m"),
                "depth_m": spec.get("depth_m"),
            })
            created_count += 1

    return {
        "ok": True, "types": out,
        "created_count": created_count, "reused_count": reused_count,
    }


# ----- BA_COL_<wxh> : types rectangulaires béton armé ------------------
#
# Tool utilitaire pour créer en bulk des types `BA_COL_<w>x<h>` dans
# une famille de poteau rectangulaire béton existante (typiquement
# `M_Concrete-Rectangular-Column` du template structural Revit, ou son
# équivalent FR `Poteau béton rectangulaire`). Prefix `BA_COL` = Béton
# Armé Colonne.
#
# Pourquoi pas créer une famille .rfa from scratch : trop coûteux pour
# le besoin (cf. discussion 2026-05-14). Une famille rectangulaire
# paramétrique existe par défaut dans tous les templates Revit
# structurels — on duplique des types dedans.


# Paramètres standards (insensible à la casse côté lookup) pour les
# dimensions b (largeur) et h (profondeur). Ordre = priorité. La 1re
# trouvée dans la famille est utilisée.
_BA_COL_WIDTH_PARAM_CANDIDATES = ("b", "Width", "Largeur", "Largeur (b)", "B")
_BA_COL_DEPTH_PARAM_CANDIDATES = (
    "h", "Depth", "Profondeur", "Hauteur", "Height",
    "Profondeur (h)", "H",
)


def _ba_col_type_name(width_cm: int, depth_cm: int) -> str:
    """Nom du type : ``BA_COL_<w>x<h>`` en cm (entiers)."""
    return "BA_COL_{}x{}".format(int(width_cm), int(depth_cm))


def _find_rectangular_concrete_column_family_symbol(doc: Any):
    """Cherche un FamilySymbol de poteau rectangulaire béton dans le
    projet. Priorité décroissante :

    1. Catégorie `StructuralColumns` + family name contient `concrete`/
       `béton`/`beton`/`b.a.` ET `rectangulaire`/`rectangular`/`rect`.
    2. Catégorie `StructuralColumns` + family name contient `rectangulaire`/
       `rectangular` (toute matière).
    3. Catégorie `StructuralColumns` (n'importe quel rectangle).
    4. Catégorie `Columns` (architectural) + name `concrete`/`béton`.

    Retourne None si rien de trouvé.
    """
    from Autodesk.Revit.DB import FamilySymbol, FilteredElementCollector
    candidates_struct_concrete: List[Any] = []
    candidates_struct_rect: List[Any] = []
    candidates_struct_any: List[Any] = []
    candidates_arch_concrete: List[Any] = []

    for sym in FilteredElementCollector(doc).OfClass(FamilySymbol):
        try:
            cat = sym.Category
            if cat is None:
                continue
            cat_name = (cat.Name or "").lower()
            is_struct = (
                "structural column" in cat_name
                or "poteau structurel" in cat_name
                or "poteaux porteurs" in cat_name
                or "colonne structurel" in cat_name
            )
            is_col_arch = (
                ("column" in cat_name or "poteau" in cat_name
                 or "colonne" in cat_name) and not is_struct
            )
            if not (is_struct or is_col_arch):
                continue
            fam_name = (sym.Family.Name or "").lower()
            is_concrete = (
                "concrete" in fam_name or "béton" in fam_name
                or "beton" in fam_name or "b.a." in fam_name
                or "b a " in fam_name
            )
            is_rect = (
                "rectangulaire" in fam_name or "rectangular" in fam_name
                or "rect" in fam_name
            )
            if is_struct and is_concrete and is_rect:
                candidates_struct_concrete.append(sym)
            elif is_struct and is_rect:
                candidates_struct_rect.append(sym)
            elif is_struct:
                candidates_struct_any.append(sym)
            elif is_col_arch and is_concrete:
                candidates_arch_concrete.append(sym)
        except Exception:  # noqa: BLE001
            continue
    for bucket in (
        candidates_struct_concrete,
        candidates_struct_rect,
        candidates_struct_any,
        candidates_arch_concrete,
    ):
        if bucket:
            return bucket[0]
    return None


def _find_dimension_param(sym: Any, candidates: tuple):
    """Cherche le 1er param trouvé dans `sym` (FamilySymbol) parmi
    `candidates`. Retourne (param_name, param_object) ou (None, None).
    """
    for name in candidates:
        try:
            p = sym.LookupParameter(name)
        except Exception:  # noqa: BLE001
            p = None
        if p is not None:
            return (name, p)
    return (None, None)


@tool(name="columns_create_rectangular_concrete_types_many", tier=1)
def create_rectangular_concrete_types_many(
    kg: ProjectKG,
    doc: Any,
    dimensions_cm: List[Dict[str, Any]],
    base_family_ref: Optional[str] = None,
    width_param: Optional[str] = None,
    depth_param: Optional[str] = None,
) -> Dict[str, Any]:
    """Crée (ou réutilise) N types ``BA_COL_<w>x<h>`` dans une famille de
    poteau rectangulaire béton existante.

    Use case : avant ou après import DXF (Phase 2d), l'user veut avoir
    une palette de types béton armé prêts à l'emploi (16x16, 22x22,
    30x30 typique pour villa / petit bâtiment). Ce tool évite de les
    créer un par un dans le UI Revit (Project Browser → Families →
    duplicate → rename → set params).

    **Pas de création de famille `.rfa` from scratch** : on duplique
    des types dans une famille existante (cf. discussion 2026-05-14 :
    create-from-scratch via API est 10-50× plus coûteux pour aucun
    gain sur du rectangulaire paramétrique standard).

    Pré-requis : une famille de poteau rectangulaire est chargée dans
    le projet (typique : `M_Concrete-Rectangular-Column.rfa` du
    template structural Revit, ou son équivalent FR). Si aucune n'est
    chargée, le tool lève une erreur actionnable.

    Pattern de nommage : ``BA_COL_<w>x<h>`` (w, h en cm entiers).
    Exemples : ``BA_COL_16x16``, ``BA_COL_22x22``, ``BA_COL_30x30``.

    Concepts: poteau, column, béton, beton, ba, b.a., armé, rectangulaire,
              type, bulk, batch, 16x16, 22x22, 30x30
    Phrases: "crée les types béton armé 16x16, 22x22, 30x30",
             "ajoute les types BA_COL", "bulk concrete column types"
    Similar: columns_get_or_create_dxf_type_many, columns_create_many

    Args:
        dimensions_cm: liste de dicts `{width_cm: int, depth_cm: int}`.
            Pour des poteaux carrés, mettre `width_cm == depth_cm`.
            Doublons internes auto-dédoublonnés.
        base_family_ref: llm_id d'un ColumnType template à utiliser.
            Si None (défaut), auto-détecte une famille rectangulaire
            structurelle béton dans le projet.
        width_param: nom du paramètre largeur (auto-détecté si absent
            parmi `b`/`Width`/`Largeur`).
        depth_param: nom du paramètre profondeur (auto-détecté parmi
            `h`/`Depth`/`Profondeur`/`Hauteur`).

    Returns:
        ``{"ok": bool, "types": [{width_cm, depth_cm, name, llm_id,
            created, revit_id}, ...], "created_count": int,
            "reused_count": int, "base_family_name": str,
            "width_param_used": str, "depth_param_used": str}``
    """
    if not isinstance(dimensions_cm, list) or not dimensions_cm:
        raise ValueError("dimensions_cm must be a non-empty list")

    # Validation + dédup interne.
    seen_keys: Set[Tuple[int, int]] = set()
    unique_dims: List[Tuple[int, int]] = []
    for i, d in enumerate(dimensions_cm):
        if not isinstance(d, dict):
            raise ValueError("dimensions_cm[{}] must be a dict".format(i))
        w = d.get("width_cm")
        h = d.get("depth_cm")
        if not isinstance(w, (int, float)) or w <= 0:
            raise ValueError(
                "dimensions_cm[{}]: width_cm must be positive number".format(i)
            )
        if not isinstance(h, (int, float)) or h <= 0:
            raise ValueError(
                "dimensions_cm[{}]: depth_cm must be positive number".format(i)
            )
        wi, hi = int(round(w)), int(round(h))
        if (wi, hi) in seen_keys:
            continue
        seen_keys.add((wi, hi))
        unique_dims.append((wi, hi))

    out: List[Dict[str, Any]] = []
    created_count = 0
    reused_count = 0

    # KG-only path : juste créer les nodes (utile pour test offline).
    if doc is None:
        for (w, h) in unique_dims:
            name = _ba_col_type_name(w, h)
            existing = _find_dxf_column_type_in_kg_by_name(kg, name)
            if existing is not None:
                node = kg.get_node(existing)
                out.append({
                    "width_cm": w, "depth_cm": h,
                    "name": name,
                    "family_name": node.get("family_name", "BA_COL"),
                    "llm_id": existing,
                    "created": False, "revit_id": None,
                })
                reused_count += 1
            else:
                nid = _create_dxf_column_type_kg_only(
                    kg, family_name="BA_COL", type_name=name,
                    kind="structural",
                )
                out.append({
                    "width_cm": w, "depth_cm": h,
                    "name": name, "family_name": "BA_COL",
                    "llm_id": nid,
                    "created": True, "revit_id": None,
                })
                created_count += 1
        return {
            "ok": True, "types": out,
            "created_count": created_count, "reused_count": reused_count,
            "base_family_name": "BA_COL",  # placeholder kg-only
            "width_param_used": None, "depth_param_used": None,
        }

    # Revit-backed path : duplique le base FamilySymbol, set les params.
    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId

    if base_family_ref is not None:
        if not kg.has_node(base_family_ref):
            raise ValueError(
                "Unknown base_family_ref: {}".format(base_family_ref)
            )
        base_eid_raw = kg.get_revit_id(base_family_ref)
        if base_eid_raw is None:
            raise ValueError(
                "base_family_ref {} has no Revit binding".format(base_family_ref)
            )
        base_sym = doc.GetElement(ElementId(base_eid_raw))
    else:
        base_sym = _find_rectangular_concrete_column_family_symbol(doc)
        if base_sym is None:
            raise ValueError(
                "Aucune famille de poteau rectangulaire béton (catégorie "
                "StructuralColumns + nom contenant 'rectangulaire'/'beton') "
                "n'est chargée dans ce projet. Charge `M_Concrete-"
                "Rectangular-Column.rfa` (ou ton équivalent FR) depuis "
                "la Revit Library, puis relance — OU passe `base_family_"
                "ref` explicitement."
            )

    # Activer le base symbol si nécessaire (pour pouvoir lire les params).
    if not base_sym.IsActive:
        # Activer demande une Tx — on ouvre la Tx ici pour tout.
        pass

    base_family_name = base_sym.Family.Name

    # Auto-detect ou utiliser les param names fournis.
    if width_param is not None:
        wp_name, wp_obj = width_param, base_sym.LookupParameter(width_param)
        if wp_obj is None:
            raise ValueError(
                "width_param {!r} non trouvé sur le FamilySymbol {!r}. "
                "Params disponibles : {}".format(
                    width_param, base_sym.Name,
                    [p.Definition.Name for p in base_sym.Parameters],
                )
            )
    else:
        wp_name, wp_obj = _find_dimension_param(
            base_sym, _BA_COL_WIDTH_PARAM_CANDIDATES,
        )
        if wp_obj is None:
            raise ValueError(
                "Aucun paramètre largeur trouvé sur {!r} parmi {}. "
                "Passe `width_param` explicitement.".format(
                    base_sym.Name, list(_BA_COL_WIDTH_PARAM_CANDIDATES),
                )
            )
    if depth_param is not None:
        dp_name, dp_obj = depth_param, base_sym.LookupParameter(depth_param)
        if dp_obj is None:
            raise ValueError(
                "depth_param {!r} non trouvé sur le FamilySymbol {!r}. "
                "Params disponibles : {}".format(
                    depth_param, base_sym.Name,
                    [p.Definition.Name for p in base_sym.Parameters],
                )
            )
    else:
        dp_name, dp_obj = _find_dimension_param(
            base_sym, _BA_COL_DEPTH_PARAM_CANDIDATES,
        )
        if dp_obj is None:
            raise ValueError(
                "Aucun paramètre profondeur trouvé sur {!r} parmi {}. "
                "Passe `depth_param` explicitement.".format(
                    base_sym.Name, list(_BA_COL_DEPTH_PARAM_CANDIDATES),
                )
            )

    with rp.transaction(doc, "columns.create_rectangular_concrete_types_many"):
        # Activer le base symbol si pas déjà fait.
        if not base_sym.IsActive:
            base_sym.Activate()
            doc.Regenerate()
        for (w, h) in unique_dims:
            name = _ba_col_type_name(w, h)
            existing = _find_dxf_column_type_in_kg_by_name(kg, name)
            if existing is not None:
                rid = kg.get_revit_id(existing)
                rid_alive = False
                if rid is not None:
                    try:
                        rid_alive = doc.GetElement(ElementId(rid)) is not None
                    except Exception:  # noqa: BLE001
                        rid_alive = False
                if rid_alive:
                    out.append({
                        "width_cm": w, "depth_cm": h,
                        "name": name,
                        "family_name": kg.get_node(existing).get(
                            "family_name", base_family_name,
                        ),
                        "llm_id": existing,
                        "created": False, "revit_id": rid,
                    })
                    reused_count += 1
                    continue
                # Stale binding → tombe en création.

            # Duplique le base symbol avec le nouveau nom.
            new_sym = base_sym.Duplicate(name)
            # Set les params dimensionnels (mètres → internal feet).
            w_internal = rp.meters_to_internal(w / 100.0)
            h_internal = rp.meters_to_internal(h / 100.0)
            # LookupParameter sur le NEW symbol (duplicate a ses propres params).
            new_wp = new_sym.LookupParameter(wp_name)
            new_dp = new_sym.LookupParameter(dp_name)
            if new_wp is None or new_dp is None:
                raise ValueError(
                    "Param {}/{} introuvable sur le type dupliqué {!r}. "
                    "Bug ou famille atypique.".format(wp_name, dp_name, name)
                )
            new_wp.Set(w_internal)
            new_dp.Set(h_internal)
            new_rid = int(new_sym.Id.Value)
            nid = _create_dxf_column_type_kg_only(
                kg, family_name=base_family_name, type_name=name,
                kind="structural",
            )
            kg.set_revit_id(nid, new_rid)
            out.append({
                "width_cm": w, "depth_cm": h, "name": name,
                "family_name": base_family_name,
                "llm_id": nid,
                "created": True, "revit_id": new_rid,
            })
            created_count += 1

    return {
        "ok": True, "types": out,
        "created_count": created_count, "reused_count": reused_count,
        "base_family_name": base_family_name,
        "width_param_used": wp_name, "depth_param_used": dp_name,
    }
