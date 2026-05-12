"""tools/bulk.py — filter-based bulk dispatch (§9 V0 Sem.4-5, UC7).

Pendant filter-based des `*_many` items-based livrés session b. Pattern :

    # items-based (session b — explicit)
    walls_set_height_many(items=[{llm_id, height_m}, …])

    # filter-based (cette session — implicit, résolu côté KG)
    bulk_apply_to_filter(
        filter={"type": "Wall", "level_ref": "level_001"},
        target_tool="walls_set_height_many",
        tool_args={"height_m": 3.0},
    )

Token saving marginal (le LLM ne ré-énumère pas les llm_ids) mais
ergonomie utilisateur supérieure (« passe toutes les fenêtres du N01
à sill=0.80 » devient un seul tool call).

**Dispatch direct via registry**, pas via `dispatch_tool_use` : la
boucle externe a déjà ouvert `kg.transaction()` ; ré-ouvrir une Tx
imbriquée écrirait `kg.persist()` à la fin de l'inner même si l'outer
rollback ensuite → divergence mémoire/disque. On appelle
`entry.fn(**kwargs)` directement, l'outer Tx couvre tout le batch.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .. import llm_protocol
from ..llm_protocol import tool
from ..project_kg import ProjectKG


# Keys autorisées dans un filter dict. Match strict : une clé non
# listée → ValueError (évite les fautes de frappe LLM qui matcheraient
# silencieusement zéro élément, ex : "levle_ref" → empty).
_FILTER_KEYS = frozenset({
    "type",            # _type attr (Wall, Window, Door, Room, …)
    "level_ref",       # at_level
    "type_ref",        # WallType for walls, FamilyType for openings
    "host_wall_ref",   # openings
    "category",        # FamilyType discriminator (Doors, Windows)
    "name",            # exact match
    "name_contains",   # substring (case-insensitive)
    "name_regex",      # regex (case-sensitive, raw pattern)
})


def _validate_filter(filter_dict: Dict[str, Any]) -> None:
    """Raise ValueError pour toute key inconnue. Permissif sur les
    valeurs (`None` ou non-string laissé passer — le matching ne
    fera tout simplement pas matcher)."""
    if not isinstance(filter_dict, dict):
        raise ValueError(
            "filter must be a dict, got {}".format(type(filter_dict).__name__)
        )
    unknown = set(filter_dict.keys()) - _FILTER_KEYS
    if unknown:
        raise ValueError(
            "Unknown filter keys: {}. Allowed: {}".format(
                sorted(unknown), sorted(_FILTER_KEYS),
            )
        )


def _match_node(attrs: Dict[str, Any], filter_dict: Dict[str, Any]) -> bool:
    """Predicate AND-fold sur toutes les keys du filter."""
    for key, expected in filter_dict.items():
        if key == "type":
            if attrs.get("_type") != expected:
                return False
        elif key == "name_contains":
            name = attrs.get("name") or ""
            if not isinstance(expected, str) or expected.lower() not in str(name).lower():
                return False
        elif key == "name_regex":
            name = attrs.get("name") or ""
            if not isinstance(expected, str):
                return False
            try:
                if not re.search(expected, str(name)):
                    return False
            except re.error as exc:
                raise ValueError(
                    "Invalid name_regex {!r}: {}".format(expected, exc)
                )
        else:
            # Comparaison directe sur l'attr du même nom.
            if attrs.get(key) != expected:
                return False
    return True


def _resolve_filter(kg: ProjectKG, filter_dict: Dict[str, Any]) -> List[str]:
    """Énumère les llm_ids des nodes vivants matchant le filter.

    Filtre soft-deleted automatique. Si `type` est fourni, optimise via
    `find_by_type` ; sinon itère tous les nodes (O(N) sur la KG, OK
    jusqu'à quelques milliers de nodes).
    """
    _validate_filter(filter_dict)
    if "type" in filter_dict:
        candidates = kg.find_by_type(filter_dict["type"])
    else:
        candidates = [
            nid for nid in kg._g.nodes()  # noqa: SLF001
            if kg.get_node(nid).get("deleted_at_turn") is None
        ]
    out: List[str] = []
    for nid in candidates:
        attrs = kg.get_node(nid)
        if attrs.get("deleted_at_turn") is not None:
            continue
        if _match_node(attrs, filter_dict):
            out.append(nid)
    return out


@tool(name="bulk_resolve_filter", tier=1)
def resolve_filter(
    kg: ProjectKG,
    filter: Dict[str, Any],
    preview_limit: int = 10,
) -> Dict[str, Any]:
    """Preview read-only : retourne les llm_ids matchant un filter.

    Utile avant `bulk_apply_to_filter` pour vérifier le périmètre. Pour
    les gros matchs (> `preview_limit`), tronque la liste et retourne
    un compteur — le LLM peut affiner le filter avant de muter.

    Concepts: filtre, filter, sélection, preview, périmètre, bulk
    Phrases: "combien de fenêtres au N01", "quels murs sont à 2.7m de haut",
             "preview filter", "lister les pièces du salon"
    Similar: bulk_apply_to_filter, catalog_list_walls, catalog_list_rooms

    Args:
        filter: dict avec keys parmi {type, level_ref, type_ref,
            host_wall_ref, category, name, name_contains, name_regex}.
            AND implicite entre keys. `type` recommandé pour la vitesse.
        preview_limit: nb max de llm_ids inlinés (défaut 10).

    Returns:
        {"ok", "count", "llm_ids": [...] | None,
         "first_llm_id", "last_llm_id" (si tronqué)}
    """
    ids = _resolve_filter(kg, filter)
    out: Dict[str, Any] = {"ok": True, "count": len(ids)}
    if len(ids) <= preview_limit:
        out["llm_ids"] = list(ids)
    else:
        out["llm_ids"] = list(ids[:preview_limit])
        out["first_llm_id"] = ids[0]
        out["last_llm_id"] = ids[-1]
        out["note"] = (
            "Truncated preview ({} of {}). Use the full filter when applying.".format(
                preview_limit, len(ids),
            )
        )
    return out


# Garde-fou : seuls les tools dont le param principal est `items: list[dict]`
# peuvent être appelés par `bulk_apply_to_filter`. Inferred via inspect au
# moment du dispatch — pas de liste hardcodée à maintenir.

def _is_many_tool(entry: Any) -> bool:
    """True si le tool accepte un paramètre `items` (sa signature *_many)."""
    import inspect
    try:
        sig = inspect.signature(entry.fn)
    except (TypeError, ValueError):
        return False
    return "items" in sig.parameters


@tool(name="bulk_apply_to_filter", tier=1)
def apply_to_filter(
    kg: ProjectKG,
    doc: Any,
    filter: Dict[str, Any],
    target_tool: str,
    tool_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Résout `filter` côté KG → construit `items` → dispatch un tool `*_many`.

    Pendant filter-based des `*_many` items-based. Cas typique : « passe
    toutes les fenêtres du N01 à sill=0.80 » devient un seul tool call,
    sans que le LLM doive d'abord lister les llm_ids via `catalog_list_*`
    puis construire les items un par un.

    **Dispatch interne direct** (pas via `dispatch_tool_use`) — la
    transaction KG externe couvre tout le batch atomiquement. Un item
    invalide dans le `*_many` cible fait rollback de tout, comme un
    appel direct du `*_many`.

    Concepts: bulk, filter, masse, plusieurs, série, dispatch
    Phrases: "passe toutes les fenêtres du N01 à 0.80",
             "uniformise les hauteurs de tous les murs salon",
             "set sill for all windows on level X",
             "renomme toutes les pièces qui contiennent salon"
    Similar: bulk_resolve_filter, walls_set_height_many,
             openings_set_sill_height_many

    Args:
        filter: dict de sélection. Voir `bulk_resolve_filter` pour les
            keys autorisées.
        target_tool: nom d'un tool `*_many` enregistré (ex :
            `walls_set_height_many`, `openings_set_sill_height_many`,
            `rooms_set_name_many`). Le tool doit accepter un param
            `items: list[dict]`.
        tool_args: dict des arguments non-`llm_id` à pousser dans chaque
            item. Ex : `{"height_m": 3.0}` pour `walls_set_height_many`,
            ou `{"sill_height_m": 0.80}` pour
            `openings_set_sill_height_many`. None = items contiennent
            uniquement `llm_id` (rare — cas d'un tool dont la seule
            entrée par item est l'identifiant, peu courant).

    Returns:
        {"ok": bool, "matched_count": int, "target_tool": str,
         "inner": <réponse du *_many cible>}
        Si `matched_count == 0`, n'appelle pas le tool cible (no-op clair)
        et `inner` est `None`.
    """
    matched = _resolve_filter(kg, filter)

    if not matched:
        return {
            "ok": True,
            "matched_count": 0,
            "target_tool": target_tool,
            "inner": None,
            "note": "Filter matched 0 nodes — no-op.",
        }

    registry = llm_protocol.get_registry()
    entry = registry.get(target_tool)
    if entry is None:
        raise ValueError("Unknown target_tool: {}".format(target_tool))
    if not _is_many_tool(entry):
        raise ValueError(
            "target_tool {} doesn't accept `items` — only `*_many` tools "
            "can be bulk-applied.".format(target_tool)
        )

    args = dict(tool_args) if tool_args else {}
    if "items" in args:
        raise ValueError(
            "tool_args must NOT contain `items` — items are built from "
            "the filter resolution."
        )
    if "llm_id" in args:
        raise ValueError(
            "tool_args must NOT contain `llm_id` — built per-item from "
            "the filter resolution."
        )

    items: List[Dict[str, Any]] = [
        {"llm_id": nid, **args} for nid in matched
    ]

    # Dispatch direct : pas de nested kg.transaction.
    import inspect
    sig = inspect.signature(entry.fn)
    call_kwargs: Dict[str, Any] = {"kg": kg, "items": items}
    if "doc" in sig.parameters:
        call_kwargs["doc"] = doc
    inner_result = entry.fn(**call_kwargs)

    return {
        "ok": True,
        "matched_count": len(matched),
        "target_tool": target_tool,
        "inner": inner_result,
    }
