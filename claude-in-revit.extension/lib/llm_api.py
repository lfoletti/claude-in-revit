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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic

from . import config
from .llm_protocol import dispatch_tool_use, tools_as_anthropic_payload
from .project_kg import ProjectKG


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

    def _system_blocks(self, system_prompt: str) -> List[Dict[str, Any]]:
        """Wrap the system prompt as a single text block with a cache breakpoint.

        Below the model's cache threshold the breakpoint silently no-ops; above
        it (Sonnet 4.6: 2048 tokens; Opus 4.6: 4096 tokens) we get the discount
        on every turn after the first.
        """
        return [{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]

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
        user_prompt: str,
        system_prompt: str,
        history: List[Dict[str, Any]],
        tier_max: Optional[int] = 1,
    ) -> TurnResult:
        """Run one user turn end-to-end (model call + dispatched tool calls).

        `history` is mutated: the user message, all assistant turns, and all
        tool-result user turns are appended. Pass an empty list for the first
        turn of a new conversation, or the kept history for follow-ups.
        """
        history.append({"role": "user", "content": user_prompt})
        usage = TurnUsage()
        tool_calls: List[Dict[str, Any]] = []
        tools_payload = tools_as_anthropic_payload(tier_max=tier_max)
        system_blocks = self._system_blocks(system_prompt)

        response = None
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

        assert response is not None
        text_parts = [b.text for b in response.content if b.type == "text"]
        return TurnResult(
            text="\n".join(text_parts),
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
            usage=usage,
        )
