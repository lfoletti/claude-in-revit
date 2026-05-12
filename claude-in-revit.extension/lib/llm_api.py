"""llm_api.py — Anthropic client + manual multi-turn tool-use loop.

The slice uses the manual loop (not `client.beta.messages.tool_runner`) on
purpose: between LLM turns the dispatcher mutates the KG inside an atomic
`kg.transaction()` and we want full visibility into each tool call. Once
the design stabilises we can decide whether the SDK tool runner buys us
anything; for now we control the loop.

Defaults track the design doc §3 (Sonnet 4.6 by default) and §7 (prompt
caching, eventual Haiku 4.5 triage). Adaptive thinking is left OFF by
default in the slice — it inflates token use on simple tool calls and the
harness validation doesn't need it. Callers can flip `thinking="adaptive"`
when they want it.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import anthropic

from . import config
from .llm_protocol import dispatch_tool_use, tools_as_anthropic_payload
from .project_kg import ProjectKG


# ----- File attachments ----------------------------------------------------
#
# Anthropic accepts attachments as content blocks directly inside the
# `messages=` payload — no separate upload endpoint. We build the blocks
# locally and they ride along with the prompt in a single HTTPS request.
# Three types covered in V0:
#   - image/* (PNG, JPEG, GIF, WEBP) → "image" block, base64-encoded.
#   - application/pdf               → "document" block, base64-encoded.
#   - text/* and friends (.txt, .md, .csv, .json, .py, …) → inlined into
#     a "text" block with a delimiter so the model sees the file as
#     structured prose rather than opaque bytes.

MAX_ATTACHMENT_BYTES = 1 * 1024 * 1024  # 1 MB hard cap (per user request).

_IMAGE_MEDIA_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_TEXT_EXTENSIONS: set = {
    ".txt", ".md", ".csv", ".json", ".py", ".log", ".html", ".xml",
    ".yaml", ".yml", ".ini", ".cfg", ".toml", ".tsv", ".rst",
}


def _read_attachment(path: Path) -> bytes:
    if not path.is_file():
        raise ValueError("Fichier introuvable : {}".format(path))
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            "Fichier trop volumineux ({} octets > {} MB max).".format(
                size, MAX_ATTACHMENT_BYTES // (1024 * 1024),
            )
        )
    return path.read_bytes()


def _file_attachment_blocks(path: Path) -> List[Dict[str, Any]]:
    """Return Anthropic content block(s) for a single attached file."""
    raw = _read_attachment(path)
    suffix = path.suffix.lower()
    name = path.name

    if suffix in _IMAGE_MEDIA_TYPES:
        return [{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _IMAGE_MEDIA_TYPES[suffix],
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }]

    if suffix == ".pdf":
        return [{
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }]

    if suffix in _TEXT_EXTENSIONS:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        return [{
            "type": "text",
            "text": (
                "--- Fichier joint : {} ({} octets) ---\n"
                "{}\n"
                "--- Fin {} ---"
            ).format(name, len(raw), text, name),
        }]

    raise ValueError(
        "Type de fichier non supporté ({}). Pris en charge : "
        "images (png/jpg/gif/webp), PDF, texte (txt/md/csv/json/py/…).".format(
            suffix or "(sans extension)"
        )
    )


def build_user_content(
    prompt_text: str,
    attachment_path: Optional[Union[str, Path]] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """Build the `content` of a user turn — string if no file, list else.

    The Anthropic SDK accepts both forms in `messages=[{"role": "user",
    "content": ...}]`. Returning a string when no attachment keeps the
    payload identical to the pre-Phase-14 flow (and trivially cacheable
    once we cross the prompt-cache threshold).
    """
    if attachment_path is None:
        return prompt_text
    path = Path(attachment_path)
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    blocks.extend(_file_attachment_blocks(path))
    return blocks


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_EFFORT = "medium"  # low | medium | high | max  (max is Opus-only)

# Models where the effort parameter is rejected.
_NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5")
# Models where `thinking` is not supported at all (Haiku family).
_NO_THINKING_MODELS = ("claude-haiku-",)


@dataclass
class TurnUsage:
    """Aggregated token usage across all API calls within one user turn."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    api_calls: int = 0

    def add(self, u: Any) -> None:
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.api_calls += 1


@dataclass
class TurnResult:
    """Outcome of one user turn (one or more API round-trips, one or more tool calls)."""
    text: str
    stop_reason: Optional[str]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: TurnUsage = field(default_factory=TurnUsage)


# ----- Conversation history persistence ------------------------------------


def _block_to_dict(block: Any) -> Any:
    """Convert one Anthropic SDK content block to a JSON-serialisable dict.

    The SDK returns pydantic v2 models (TextBlock, ToolUseBlock, etc.); their
    `.model_dump()` produces the same shape the API accepts as input. Dicts
    are already serialisable, returned as-is. Anything else gets a defensive
    `{"type": "text", "text": str(...)}` wrap so a corrupt history can't crash
    the loop.
    """
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if isinstance(block, dict):
        return block
    return {"type": "text", "text": str(block)}


def serialize_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk a `messages=` history and replace SDK blocks with plain dicts.

    Use before persisting to disk. The API on rehydration accepts the
    serialised form directly — no inverse `deserialize_history` needed,
    `json.load` is enough.
    """
    out: List[Dict[str, Any]] = []
    for turn in history:
        content = turn["content"]
        if isinstance(content, str):
            serialised = content
        elif isinstance(content, list):
            serialised = [_block_to_dict(b) for b in content]
        else:
            serialised = _block_to_dict(content)
        out.append({"role": turn["role"], "content": serialised})
    return out


def save_history(history: List[Dict[str, Any]], path: Path) -> None:
    """Atomically write the serialised history to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_history(history)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_history(path: Path) -> List[Dict[str, Any]]:
    """Load a previously-persisted history, or return an empty list."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _block_type(block: Any) -> Optional[str]:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_attr(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _tool_use_ids(content: Any) -> List[str]:
    if not isinstance(content, list):
        return []
    out: List[str] = []
    for block in content:
        if _block_type(block) == "tool_use":
            tid = _block_attr(block, "id")
            if tid:
                out.append(tid)
    return out


def _tool_result_ids(content: Any) -> set:
    if not isinstance(content, list):
        return set()
    return {
        _block_attr(b, "tool_use_id")
        for b in content
        if _block_type(b) == "tool_result"
    }


def _approx_chars(value: Any) -> int:
    """Heuristic char count for a turn's content. Walks the structure
    rather than `json.dumps` to avoid quoting/escaping cost (cheaper
    and good-enough for size budgeting)."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        n = 0
        for k, v in value.items():
            n += len(k) + _approx_chars(v) + 4
        return n
    if isinstance(value, list):
        return sum(_approx_chars(v) for v in value) + 2 * len(value)
    if hasattr(value, "model_dump"):
        return _approx_chars(value.model_dump())
    return len(str(value))


def trim_history_to_max_chars(
    history: List[Dict[str, Any]],
    max_chars: int = 120_000,
) -> "tuple[List[Dict[str, Any]], int]":
    """Drop oldest history entries until total content size ≤ max_chars.

    Approximate token budget : ~4 chars per token → 120 K chars ≈ 30 K
    tokens of history. Adjust `max_chars` if your tour mix differs.

    Why a hard cap : the conversation accumulates indefinitely
    otherwise. Each new turn pays the input-token cost of *every*
    previous turn — linear growth that quickly dominates the bill.
    Trimming the head keeps the working window bounded.

    Structural integrity : we drop entries from the front in steps,
    and run `sanitize_history` at the end so the result is guaranteed
    Anthropic-valid (no orphan tool_use without matching tool_result).

    Returns `(trimmed_history, dropped_count)`.
    """
    total = sum(_approx_chars(turn.get("content", "")) for turn in history)
    if total <= max_chars:
        return list(history), 0
    # Drop from the front until under budget. Keep at least the last
    # 2 entries so we don't return an empty list mid-conversation
    # (would force the LLM to lose all context).
    keep = list(history)
    dropped = 0
    while keep and total > max_chars and len(keep) > 2:
        removed = keep.pop(0)
        total -= _approx_chars(removed.get("content", ""))
        dropped += 1
    # Sanitize to ensure structural validity (a removed assistant turn
    # could leave a dangling user `tool_result` in front).
    keep, sanitized_dropped = sanitize_history(keep)
    return keep, dropped + sanitized_dropped


def sanitize_history(
    history: List[Dict[str, Any]],
) -> "tuple[List[Dict[str, Any]], int]":
    """Return `(longest_valid_prefix, dropped_count)`.

    The Anthropic API rejects any conversation where an assistant turn
    contains `tool_use` blocks that aren't *immediately* followed by a
    user turn whose `tool_result` blocks cover every tool_use id. A
    persisted history can violate that invariant if a previous session
    crashed between appending the assistant turn and appending the
    corresponding tool_results (or if it was saved by an older buggy
    version of the loop).

    This function walks the history once and returns the longest prefix
    that satisfies the invariant. Everything after the first dangling
    `tool_use` is dropped — Anthropic doesn't let us reach into the
    middle and stitch back together, the rest of the history is
    unrecoverable from the API's perspective.

    The user prompt that started the broken sub-conversation is also
    dropped: we want to preserve a *valid* prefix, not a prefix that
    ends mid-question with no answer.
    """
    safe_len = 0
    i = 0
    n = len(history)
    while i < n:
        turn = history[i]
        if not isinstance(turn, dict) or "role" not in turn:
            break
        if turn["role"] == "assistant":
            tu_ids = _tool_use_ids(turn.get("content"))
            if tu_ids:
                # Must be followed by a user turn with matching tool_results.
                if i + 1 >= n:
                    break
                nxt = history[i + 1]
                if not isinstance(nxt, dict) or nxt.get("role") != "user":
                    break
                tr_ids = _tool_result_ids(nxt.get("content"))
                if not all(t in tr_ids for t in tu_ids):
                    break
                # Both turns valid — consume them as a unit.
                i += 2
                safe_len = i
                continue
        if turn["role"] == "user":
            # Si un user message contient des tool_result blocks, il doit
            # **immédiatement** suivre un assistant turn avec les tool_use
            # correspondants — sinon Anthropic 400. Le cas paire (i-1,i)
            # est attrapé par la branche assistant ci-dessus ; ici on
            # gère le user tool_result **orphelin** (en tête de l'historique
            # OU laissé derrière par un trim qui a coupé l'assistant
            # tool_use précédent). Cas observé en runtime 2026-05-12 :
            # trim mid-paire → user tool_result orphelin → API 400.
            if _tool_result_ids(turn.get("content")):
                break
        # Either non-assistant role, or assistant without tool_use → solo.
        i += 1
        safe_len = i
    # If we stopped on a dangling assistant, also drop the user prompt that
    # preceded it — keeping a question without its (now-discarded) answer is
    # confusing for the conversation flow.
    if safe_len < n and safe_len > 0 and history[safe_len - 1].get("role") == "user":
        safe_len -= 1
    return history[:safe_len], n - safe_len


class LLMClient:
    """Thin wrapper around `anthropic.Anthropic` with a manual tool-use loop."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking: str = "disabled",  # "disabled" | "adaptive"
        effort: Optional[str] = DEFAULT_EFFORT,
        max_iterations: int = 16,
        api_key: Optional[str] = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else config.get_api_key()
        self.client = anthropic.Anthropic(api_key=resolved_key)
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.effort = effort
        self.max_iterations = max_iterations

    # ----- Per-call config building ------------------------------------

    def _system_blocks(
        self, system_prompt: Any,
    ) -> List[Dict[str, Any]]:
        """Normalize a system prompt into Anthropic content blocks.

        Two shapes accepted :

        - **str** : single block with a cache breakpoint at its end.
          Below the model's cache threshold (Sonnet 4.6 = 2048 tokens)
          the breakpoint silently no-ops; above it, you get the
          discount on every turn after the first. Used by the CLI for
          simplicity.

        - **list[dict]** : passed through as-is. The caller controls
          which blocks carry `cache_control` and where the
          breakpoints fall. Use this when the system prompt has
          stable + per-turn-varying parts (e.g. static instructions
          cached, dynamic project state not cached) — saves cache
          re-encoding every turn.
        """
        if isinstance(system_prompt, str):
            return [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        return list(system_prompt)

    def _extra_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if self.thinking == "adaptive" and not self._is_no_thinking():
            kwargs["thinking"] = {"type": "adaptive"}
        if self.effort and not self._is_no_effort():
            kwargs["output_config"] = {"effort": self.effort}
        return kwargs

    def _is_no_thinking(self) -> bool:
        return any(self.model.startswith(p) for p in _NO_THINKING_MODELS)

    def _is_no_effort(self) -> bool:
        return self.model in _NO_EFFORT_MODELS

    # ----- Public entry point ------------------------------------------

    def run_turn(
        self,
        kg: ProjectKG,
        user_prompt: Any,
        system_prompt: Any,
        history: List[Dict[str, Any]],
        tier_max: Optional[int] = 1,
        doc: Any = None,
    ) -> TurnResult:
        """Run one user turn end-to-end (model call + dispatched tool calls).

        `system_prompt` may be a plain string (whole block cached at
        its end) or a pre-shaped list of content blocks (caller
        controls `cache_control` placement — recommended once the
        prompt has stable + per-turn-varying parts).

        `history` is mutated: the user message, all assistant turns, and all
        tool-result user turns are appended. Pass an empty list for the first
        turn of a new conversation, or the kept history for follow-ups.

        `doc` is forwarded to `dispatch_tool_use` so Revit-aware tools
        (those declaring `doc` as their second context param) receive the
        live Revit `Document`. Hors-Revit callers (CLI, pytest) leave it
        at `None` and tools fall back to their KG-only branch.
        """
        history.append({"role": "user", "content": user_prompt})
        usage = TurnUsage()
        tool_calls: List[Dict[str, Any]] = []
        tools_payload = tools_as_anthropic_payload(tier_max=tier_max)
        system_blocks = self._system_blocks(system_prompt)

        response = None
        try:
            for _ in range(self.max_iterations):
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    tools=tools_payload,
                    messages=history,
                    **self._extra_kwargs(),
                )
                usage.add(response.usage)

                # Always append the assistant turn before deciding what to do next.
                history.append({"role": "assistant", "content": response.content})

                stop = response.stop_reason
                if stop == "tool_use":
                    tool_results: List[Dict[str, Any]] = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_calls.append({
                                "name": block.name,
                                "input": dict(block.input),
                                "id": block.id,
                            })
                            result = dispatch_tool_use(
                                tool_name=block.name,
                                tool_input=dict(block.input),
                                tool_use_id=block.id,
                                kg=kg,
                                doc=doc,
                            )
                            tool_results.append(result)
                    history.append({"role": "user", "content": tool_results})
                    continue

                # All other stop_reasons (end_turn, refusal, max_tokens, pause_turn)
                # exit the loop. pause_turn would matter for server-side tools; we
                # have none, so treat it as a stop too.
                break
            else:
                # max_iterations exhausted without natural stop
                text = "[max_iterations exhausted: {}]".format(self.max_iterations)
                return TurnResult(text=text, stop_reason="max_iterations", tool_calls=tool_calls, usage=usage)
        finally:
            # Defend against an exception that escapes mid-loop after the
            # assistant turn was appended but before its tool_results user
            # turn could be: drop the dangling assistant so the in-memory
            # history stays well-formed (Anthropic invariant). The user
            # prompt that initiated the broken sub-turn is also dropped —
            # see sanitize_history's rationale.
            while history:
                last = history[-1]
                if not isinstance(last, dict) or last.get("role") != "assistant":
                    break
                tu_ids = _tool_use_ids(last.get("content"))
                if not tu_ids:
                    break
                # Dangling tool_use → drop this assistant turn.
                history.pop()
                # Also drop the preceding user prompt if it's there.
                if history and isinstance(history[-1], dict) and history[-1].get("role") == "user":
                    history.pop()

        assert response is not None
        text_parts = [b.text for b in response.content if b.type == "text"]
        return TurnResult(
            text="\n".join(text_parts),
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
            usage=usage,
        )
