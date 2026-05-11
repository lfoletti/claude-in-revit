"""llm_protocol.py — tool registry, schema generation, and dispatcher.

Tools live in `lib/tools/*.py`. Each is a plain function decorated with
`@tool`. The decorator parses the docstring (sections `Concepts:`, `Phrases:`,
`Similar:`, `Args:`) and the type hints to build the JSON schema the Anthropic
API expects in `tools=[...]`.

Hidden context parameters (passed by the dispatcher, hidden from the
LLM-facing schema):
- `kg: ProjectKG` — required, must be the first parameter.
- `doc` — optional second parameter for Revit-touching tools. If the tool
  declares it, the dispatcher injects the live Revit `Document` (or `None`
  when running hors-Revit, e.g. CLI / tests). Tools should branch on
  `doc is None` to provide a KG-only fallback.

Remaining parameters become the tool's `input_schema` for the API.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
import textwrap
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import project_kg as _kg_mod  # noqa: F401  (re-exported via type hints)
from .project_kg import ProjectKG


# Python 3.8 has no types.UnionType — that's the `int | None` style. Fold it
# in conditionally so the dispatcher accepts both forms once we run on 3.10+.
_UNION_ORIGINS: Tuple[Any, ...] = (typing.Union,)
if sys.version_info >= (3, 10):
    import types as _types
    _UNION_ORIGINS = (typing.Union, _types.UnionType)


# ----- Docstring parsing ---------------------------------------------------

_SECTION_HEADER_RE = re.compile(
    # No trailing $ — section headers usually carry inline content
    # (`Concepts: a, b, c`); the `\s*` consumes spaces or the newline.
    r"^(Concepts|Phrases|Similar|Args|Returns):\s*",
    re.MULTILINE,
)
_QUOTED_PHRASE_RE = re.compile(r'"([^"]*)"')
_ARG_LINE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(.+)$")


def _split_csv(s: str) -> List[str]:
    return [item.strip() for item in s.replace("\n", " ").split(",") if item.strip()]


def _split_quoted_phrases(s: str) -> List[str]:
    return [m.group(1).strip() for m in _QUOTED_PHRASE_RE.finditer(s) if m.group(1).strip()]


def _parse_args(section_body: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in section_body.splitlines():
        m = _ARG_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_docstring(doc: Optional[str]) -> Dict[str, Any]:
    """Extract structured metadata from a tool's docstring.

    Sections: `Concepts:` (csv), `Phrases:` (quoted strings), `Similar:` (csv),
    `Args:` (one `name: description` per line), `Returns:` (free text).
    Description is everything before the first section header.
    """
    if not doc:
        return {
            "description": "",
            "concepts": [],
            "phrases": [],
            "similar": [],
            "args": {},
            "returns": "",
        }

    # `inspect.cleandoc` handles the standard docstring shape where the first
    # line has no indentation (right after `"""`) and the rest is indented to
    # match the surrounding code — which `textwrap.dedent` alone does not.
    body = inspect.cleandoc(doc)
    parts = _SECTION_HEADER_RE.split(body)
    description = parts[0].strip()
    sections: Dict[str, str] = {}
    for i in range(1, len(parts), 2):
        name = parts[i]
        chunk = parts[i + 1] if i + 1 < len(parts) else ""
        sections[name] = textwrap.dedent(chunk).strip()

    return {
        "description": description,
        "concepts": _split_csv(sections.get("Concepts", "")),
        "phrases": _split_quoted_phrases(sections.get("Phrases", "")),
        "similar": _split_csv(sections.get("Similar", "")),
        "args": _parse_args(sections.get("Args", "")),
        "returns": sections.get("Returns", ""),
    }


# ----- Type hint -> JSON schema --------------------------------------------

def _is_optional(annotation: Any) -> Tuple[bool, Any]:
    """If annotation is Optional[X] / Union[X, None], return (True, X)."""
    origin = typing.get_origin(annotation)
    if origin not in _UNION_ORIGINS:
        return False, annotation
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return True, args[0]
    return False, annotation


def _annotation_to_schema(annotation: Any, description: str = "") -> Dict[str, Any]:
    annotation = annotation if annotation is not inspect.Parameter.empty else str
    _, annotation = _is_optional(annotation)
    origin = typing.get_origin(annotation)

    if annotation is str:
        schema: Dict[str, Any] = {"type": "string"}
    elif annotation is int:
        schema = {"type": "integer"}
    elif annotation is float:
        schema = {"type": "number"}
    elif annotation is bool:
        schema = {"type": "boolean"}
    elif annotation is dict or origin in (dict,):
        schema = {"type": "object"}
    elif annotation is list or origin in (list,):
        schema = {"type": "array"}
        args = typing.get_args(annotation)
        if args:
            schema["items"] = _annotation_to_schema(args[0])
    else:
        # Fallback — unknown type, accept any string. Better to widen than 400.
        schema = {"type": "string"}

    if description:
        schema["description"] = description
    return schema


# ----- Tool registry --------------------------------------------------------

@dataclass
class ToolEntry:
    name: str
    fn: Callable[..., Any]
    description: str
    input_schema: Dict[str, Any]
    concepts: List[str] = field(default_factory=list)
    phrases: List[str] = field(default_factory=list)
    similar: List[str] = field(default_factory=list)
    tier: int = 2  # tier-1 tools are always loaded; tier-2 are routed in.


_REGISTRY: Dict[str, ToolEntry] = {}


def tool(
    name: Optional[str] = None,
    tier: int = 2,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as an LLM-callable tool.

    Usage:
        @tool(name="walls.create", tier=1)
        def create(kg: ProjectKG, level_ref: str, ...) -> dict:
            '''Crée un mur sur le niveau donné.

            Concepts: mur, création
            Phrases: "dessine un mur", "trace un mur"
            Similar: walls.modify

            Args:
                level_ref: llm_id du Level cible.
                ...
            '''
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if not params:
            raise TypeError(
                "Tool {} must accept at least kg: ProjectKG as first argument".format(
                    tool_name
                )
            )

        # Hidden context parameters — first must be `kg`, optional second
        # is `doc` for Revit-touching tools. Both are skipped in the
        # LLM-facing schema and injected by the dispatcher at call time.
        if params[0].name != "kg":
            raise TypeError(
                "Tool {} first param must be named 'kg' (got '{}')".format(
                    tool_name, params[0].name
                )
            )
        if len(params) >= 2 and params[1].name == "doc":
            user_params = params[2:]
        else:
            user_params = params[1:]

        # `from __future__ import annotations` (used everywhere in this repo)
        # turns annotations into strings; resolve them once via get_type_hints
        # so the schema generator sees real types.
        try:
            type_hints = typing.get_type_hints(fn)
        except Exception:  # noqa: BLE001 - annotations may reference unresolved names
            type_hints = {}

        meta = parse_docstring(fn.__doc__)
        properties: Dict[str, Dict[str, Any]] = {}
        required: List[str] = []
        for p in user_params:
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                raise TypeError(
                    "Tool {} cannot use *args/**kwargs (param '{}')".format(
                        tool_name, p.name
                    )
                )
            annotation = type_hints.get(p.name, str)
            description = meta["args"].get(p.name, "")
            schema = _annotation_to_schema(annotation, description)
            properties[p.name] = schema
            optional, _ = _is_optional(annotation)
            if not optional and p.default is inspect.Parameter.empty:
                required.append(p.name)

        input_schema: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            input_schema["required"] = required

        entry = ToolEntry(
            name=tool_name,
            fn=fn,
            description=meta["description"],
            input_schema=input_schema,
            concepts=meta["concepts"],
            phrases=meta["phrases"],
            similar=meta["similar"],
            tier=tier,
        )
        if tool_name in _REGISTRY:
            raise ValueError("Tool already registered: {}".format(tool_name))
        _REGISTRY[tool_name] = entry
        # Stash on the function so test code can introspect.
        fn.__tool_entry__ = entry  # type: ignore[attr-defined]
        return fn

    return deco


def get_registry() -> Dict[str, ToolEntry]:
    """Return the tool registry. Triggers tool auto-import on first call."""
    if not _REGISTRY:
        # Importing the package runs its __init__.py which auto-imports modules.
        from . import tools  # noqa: F401
    return _REGISTRY


def reset_registry() -> None:
    """Test helper — wipes the registry AND drops cached tool modules.

    Removing entries from `sys.modules` is not enough: Python also stashes
    submodules as attributes on the parent package, so `from . import tools`
    would otherwise short-circuit on the cached attribute and skip the
    @tool-decorated module bodies.
    """
    _REGISTRY.clear()
    parent = sys.modules.get("lib")
    for mod_name in [
        n for n in list(sys.modules)
        if n == "lib.tools" or n.startswith("lib.tools.")
    ]:
        del sys.modules[mod_name]
    if parent is not None and hasattr(parent, "tools"):
        delattr(parent, "tools")


def tools_as_anthropic_payload(tier_max: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return the registry in the shape Anthropic's `tools=...` expects."""
    registry = get_registry()
    out: List[Dict[str, Any]] = []
    for entry in registry.values():
        if tier_max is not None and entry.tier > tier_max:
            continue
        out.append({
            "name": entry.name,
            "description": entry.description,
            "input_schema": entry.input_schema,
        })
    return out


# ----- Dispatcher ----------------------------------------------------------

def dispatch_tool_use(
    tool_name: str,
    tool_input: Dict[str, Any],
    tool_use_id: str,
    kg: ProjectKG,
    doc: Any = None,
) -> Dict[str, Any]:
    """Execute a tool_use and return a tool_result content block.

    Mutations run inside `kg.transaction()` — any exception rolls the KG
    back and surfaces an `is_error: true` result to the LLM.

    `doc` is passed only to tools that declared it as their second
    parameter (after `kg`). Tools that don't declare `doc` keep their
    pre-Revit signature and aren't affected. Callers running hors-Revit
    (CLI, pytest) leave `doc=None` and Revit-aware tools take their
    KG-only fallback branch.
    """
    registry = get_registry()
    entry = registry.get(tool_name)
    if entry is None:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": "Unknown tool: {}".format(tool_name),
            "is_error": True,
        }

    call_kwargs: Dict[str, Any] = dict(tool_input)
    call_kwargs["kg"] = kg
    if "doc" in inspect.signature(entry.fn).parameters:
        call_kwargs["doc"] = doc

    try:
        with kg.transaction():
            result = entry.fn(**call_kwargs)
        # Result must be JSON-serialisable. Tools should return dicts.
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(result, ensure_ascii=False),
            "is_error": False,
        }
    except Exception as e:  # noqa: BLE001  - surfacing errors to the LLM is the point
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": _compact_tool_error(e),
            "is_error": True,
        }


_TOOL_ERROR_MAX_CHARS = 400


def _compact_tool_error(exc: BaseException) -> str:
    """Compact `tool_result.content` for an exception — type + truncated msg.

    Why : .NET / PythonNet exceptions (the typical Revit-side failures)
    have multi-line messages with namespaces and parameter dumps that
    can easily push 5000+ tokens *per failed call*. Multiplied by N
    retries × accumulated in the conversation history, this dominates
    the per-turn token bill (saw 100 K input tokens after 2 retries
    on 2026-05-11). The LLM doesn't need the full .NET trace to
    correct course — exception type + first ~400 chars of the message
    is enough.

    The full traceback is still emitted to the pyRevit log via the
    defensive shell in `prompt.pushbutton` for human debugging.
    """
    msg = str(exc)
    if len(msg) > _TOOL_ERROR_MAX_CHARS:
        msg = msg[:_TOOL_ERROR_MAX_CHARS] + "…[truncated]"
    return "{}: {}".format(type(exc).__name__, msg)
