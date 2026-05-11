"""columns.py — création et inventaire des poteaux (architecturaux + structurels).

Suit le pattern `walls.py` (Phase 8 / 11) : doc-aware, branche Revit qui
appelle `NewFamilyInstance`, branche KG-only pour CLI / tests. La hauteur
du poteau est posée via `FAMILY_TOP_LEVEL_OFFSET_PARAM` (mode "unconnected
height" — top level = base level, top offset = height). Les familles de
poteaux qui n'exposent pas ce paramètre lèvent un message actionnable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

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
