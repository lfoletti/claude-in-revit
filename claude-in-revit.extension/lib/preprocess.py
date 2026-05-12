"""preprocess.py — prompt-side guards executed BEFORE the Claude API call.

Goal : convert *advisory* system-prompt rules into *deterministic* runtime
behaviour. The LLM may still skip an advisory rule under token pressure
or conversation momentum (cf. the 2026-05-11 incident where "toutes les
fenêtres" only hit 2 out of 3 because the model relied on its memory
instead of querying the KG). A pre-processor that detects exhaustive
quantifiers and injects the relevant `catalog_list_*` payload into the
user message closes that hole without depending on model compliance.

Currently exposed :

- `detect_exhaustive_collections(prompt)` — regex pass over the prompt,
  returns the list of `(tool_name, collection_key)` pairs to auto-scan.
- `autoscan_payload(prompt, kg)` — runs the detected catalog tools
  against `kg` and builds an `<auto_scan_kg>` preamble to prepend to the
  user message. Returns `""` when nothing matched.

Both are KG-only (read the live KG, no Revit transaction) and safe to
call hors-Revit (pytest harness).

The patterns target French + English with usual diacritic / plural
tolerance. New collection types added via `tools/openings.py` etc. plug
in by extending `_COLLECTION_MAP` here.
"""
from __future__ import annotations

import re
from typing import Any, List, Tuple


# Quantifier prefixes that imply « tous, sans exception ».
# Anchored as a non-capturing group inside `_pattern_for`. We accept
# common ASCII fallbacks for `é/è/à` in case the user types without
# diacritics (`totalite`, `ensemble`).
_QUANTIFIERS = (
    r"toutes?\s+les|tous\s+les|chaque|"
    r"l['’]ensemble\s+des|la\s+totalit[ée]\s+des|"
    r"all\s+(?:the\s+)?|every\s+|each\s+"
)


# Each entry is `(collection_noun_regex, tool_name, payload_key)`. The
# noun matches what the user typed (FR + EN, sing/plur) ; the tool is the
# `catalog_list_*` we'll dispatch ; the payload key is just for
# annotation in the injected block.
#
# **Order matters** : longer-noun entries (`types de mur`) come BEFORE
# the shorter overlapping ones (`murs`) so the regex match resolves to
# the more specific tool when both could apply.
_COLLECTION_MAP: List[Tuple[str, str, str]] = [
    # Type catalogs first (longer noun phrases).
    (r"types?\s+de\s+mur\w*|wall\s+types?", "catalog_list_wall_types", "wall_types"),
    (r"types?\s+de\s+poteau\w*|column\s+types?", "catalog_list_column_types", "column_types"),
    (r"types?\s+de\s+porte\w*|door\s+types?", "catalog_list_door_types", "door_types"),
    (r"types?\s+de\s+fen[êe]tre\w*|window\s+types?", "catalog_list_window_types", "window_types"),
    # Instance catalogs.
    (r"murs?\b|walls?\b", "catalog_list_walls", "walls"),
    (r"fen[êe]tres?\b|windows?\b", "catalog_list_windows", "windows"),
    (r"portes?\b|doors?\b", "catalog_list_doors", "doors"),
    (r"poteaux?\b|colonnes?\b|columns?\b", "catalog_list_columns", "columns"),
    (r"niveaux?\b|[ée]tages?\b|levels?\b|floors?\b", "catalog_list_levels", "levels"),
    (r"lignes?\b|lines?\b", "catalog_list_lines", "lines"),
]


def _pattern_for(noun_re: str) -> re.Pattern:
    """Build the full regex `(quantifier) (collection_noun)` for a noun."""
    return re.compile(
        r"\b(?:" + _QUANTIFIERS + r")\s*" + noun_re,
        re.IGNORECASE | re.UNICODE,
    )


# Compiled once at import — patterns are reused on every prompt.
_COMPILED: List[Tuple[re.Pattern, str, str]] = [
    (_pattern_for(noun_re), tool_name, key)
    for noun_re, tool_name, key in _COLLECTION_MAP
]


def detect_exhaustive_collections(prompt: str) -> List[Tuple[str, str]]:
    """Return `[(tool_name, payload_key), …]` for collections targeted by
    an exhaustive quantifier in `prompt`.

    De-duplicates : if the prompt mentions « toutes les fenêtres » twice
    or via both « fenêtres » and « windows », we only return
    `catalog_list_windows` once. Order follows the first match position
    in the prompt — stable, debuggable.
    """
    matches: List[Tuple[int, str, str]] = []  # (start_pos, tool, key)
    seen = set()
    for pattern, tool_name, key in _COMPILED:
        if tool_name in seen:
            continue
        m = pattern.search(prompt)
        if m is None:
            continue
        seen.add(tool_name)
        matches.append((m.start(), tool_name, key))
    matches.sort(key=lambda t: t[0])
    return [(tool, key) for _, tool, key in matches]


_AUTOSCAN_OPEN = "<auto_scan_kg>"
_AUTOSCAN_CLOSE = "</auto_scan_kg>"
_AUTOSCAN_NOTE = (
    "Cette énumération est exhaustive et à jour au moment de cette "
    "requête. Itère sur ces llm_ids directement — ne te fie pas à ta "
    "mémoire de turns précédents et ne rappelle PAS `catalog_list_*` "
    "pour ces collections-là."
)


def autoscan_payload(prompt: str, kg: Any) -> str:
    """Build the `<auto_scan_kg>` preamble for `prompt`, or `""` if no
    exhaustive expression was detected.

    The preamble is prepended to the user message in
    `prompt.pushbutton/script.py` so the LLM sees the live KG state of
    every targeted collection before deciding which tool to call. Token
    cost is paid only when an exhaustive quantifier actually appears —
    no overhead on regular prompts.
    """
    detected = detect_exhaustive_collections(prompt)
    if not detected:
        return ""

    # Lazy import — keeps this module testable without dragging the
    # full tool registry / Revit imports when only the regex layer
    # is being exercised.
    from . import llm_protocol

    blocks: List[str] = []
    for tool_name, _key in detected:
        try:
            result = llm_protocol.dispatch_tool_use(
                tool_name, {}, "autoscan", kg,
            )
        except Exception:  # noqa: BLE001 — never fail the turn on autoscan.
            continue
        if result.get("is_error"):
            continue
        content = result.get("content", "")
        blocks.append("[Auto-scan KG — {}]\n{}".format(tool_name, content))

    if not blocks:
        return ""

    return (
        _AUTOSCAN_OPEN + "\n"
        + "\n\n".join(blocks)
        + "\n\n" + _AUTOSCAN_NOTE + "\n"
        + _AUTOSCAN_CLOSE + "\n\n"
    )


# ----- Tier-2 routing (§9 DESIGN, dette infrastructure) ---------------------
#
# Le DESIGN doc prévoit un `ROUTING_RULES` dans `routing.py` pour charger
# conditionnellement les tools tier-2 selon des keywords du prompt. Pas
# encore implémenté en module dédié — version minimale ici en attendant.
# Approche : regex sur le prompt utilisateur pour détecter les domaines
# tier-2 dont les tools doivent devenir visibles ce tour-ci.

_TIER2_KEYWORDS = (
    # UC1 DWG ingest — tools/dwg_import.py
    r"\bdxf\b|\bdwg\b|"
    r"importer?\s+(?:depuis|le\s+)?(?:plan|cao|cad)|"
    r"(?:ingest|import)\s+(?:plan|cad|dwg|dxf)|"
    r"plan\s+(?:d['’]archi|cad|cao)"
)

_TIER2_RE = re.compile(_TIER2_KEYWORDS, re.IGNORECASE)


def infer_tier_max(prompt: str) -> int:
    """Renvoie le `tier_max` à passer à `LLMClient.run_turn` pour ce prompt.

    Défaut : 1 (tier-1 only, payload minimal). Si le prompt mentionne
    un domaine tier-2 (DWG ingest pour V0), monte à 2 → le LLM voit
    les tools `dwg_*`. À étendre quand d'autres tier-2 arrivent
    (compliance UC8, vision UC6, etc.).

    Trade-off accepté : bump global ce tour. Filtrage plus fin
    (« seulement les dwg_* mais pas d'autres tier-2 ») demanderait un
    paramétrage par catégorie de tools — pas justifié à V0 où il n'y
    a qu'un domaine tier-2.
    """
    if _TIER2_RE.search(prompt or ""):
        return 2
    return 1
