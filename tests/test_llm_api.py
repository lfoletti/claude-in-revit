"""Tests for lib.llm_api — history serialisation and persistence.

The Anthropic API call itself is not unit-tested here (it requires a live
key and money); validated end-to-end via the slice CLI and the
`prompt.pushbutton` runtime check. These tests cover the persistence
layer that survives across Revit-session-equivalent process restarts.
"""
from __future__ import annotations

import json

import pytest

from lib import llm_api


class _FakeBlock:
    """Stand-in for an Anthropic SDK ContentBlock (pydantic v2 model)."""
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return dict(self._payload)


def test_serialize_history_passes_string_content_through():
    history = [{"role": "user", "content": "hello"}]
    assert llm_api.serialize_history(history) == [
        {"role": "user", "content": "hello"},
    ]


def test_serialize_history_dumps_sdk_blocks_via_model_dump():
    block = _FakeBlock({"type": "text", "text": "salut"})
    history = [{"role": "assistant", "content": [block]}]
    assert llm_api.serialize_history(history) == [
        {"role": "assistant", "content": [{"type": "text", "text": "salut"}]},
    ]


def test_serialize_history_keeps_plain_dict_blocks_intact():
    """Tool-result turns are dicts we build ourselves; they should round-trip."""
    tool_result = {
        "type": "tool_result",
        "tool_use_id": "t_1",
        "content": "{\"ok\": true}",
        "is_error": False,
    }
    history = [{"role": "user", "content": [tool_result]}]
    assert llm_api.serialize_history(history) == [
        {"role": "user", "content": [tool_result]},
    ]


def test_serialize_history_defends_against_unknown_objects():
    """An object without `.model_dump` and not a dict gets a defensive wrap."""
    class Weird:
        def __str__(self):
            return "unexpected"

    history = [{"role": "assistant", "content": [Weird()]}]
    out = llm_api.serialize_history(history)
    assert out == [
        {"role": "assistant", "content": [{"type": "text", "text": "unexpected"}]},
    ]


def test_save_and_load_history_roundtrip(tmp_path):
    path = tmp_path / "p" / "abc.history.json"  # parent dir doesn't exist yet
    history = [
        {"role": "user", "content": "trace un mur de 3 m"},
        {"role": "assistant", "content": [
            _FakeBlock({"type": "text", "text": "OK"}),
            _FakeBlock({"type": "tool_use", "id": "t_1", "name": "walls_create", "input": {}}),
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t_1", "content": "ok", "is_error": False},
        ]},
    ]

    llm_api.save_history(history, path)
    assert path.exists()
    # No leftover tmp file alongside.
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    loaded = llm_api.load_history(path)
    # All SDK blocks materialised as dicts.
    assert loaded[0] == {"role": "user", "content": "trace un mur de 3 m"}
    assert loaded[1]["content"][0] == {"type": "text", "text": "OK"}
    assert loaded[1]["content"][1]["name"] == "walls_create"
    assert loaded[2]["content"][0]["tool_use_id"] == "t_1"


def test_load_history_returns_empty_list_when_missing(tmp_path):
    assert llm_api.load_history(tmp_path / "nope.history.json") == []


def test_save_history_is_atomic_via_tmp_rename(tmp_path):
    """A failed write must not leave a half-written history.json behind.

    We don't simulate a crash here; we just confirm that after a normal
    `save_history` the file content parses cleanly (rename-from-tmp gives
    us all-or-nothing semantics on the same filesystem).
    """
    path = tmp_path / "h.history.json"
    llm_api.save_history([{"role": "user", "content": "x"}], path)
    json.loads(path.read_text(encoding="utf-8"))  # raises if half-written


# ----- build_user_content (file attachments) -----------------------------

import base64  # noqa: E402  (kept at use site to highlight the encoding path)


def test_system_blocks_wraps_string_with_cache_breakpoint(monkeypatch):
    """Backwards-compat path: a plain string system prompt → one block
    with `cache_control` set, as before the static/dynamic split."""
    # Avoid touching the real Anthropic client; we never call .messages.
    monkeypatch.setattr(llm_api.config, "get_api_key", lambda: "sk-fake")
    client = llm_api.LLMClient(api_key="sk-fake")
    blocks = client._system_blocks("hello world")  # noqa: SLF001
    assert blocks == [
        {"type": "text", "text": "hello world", "cache_control": {"type": "ephemeral"}},
    ]


def test_system_blocks_passes_list_of_blocks_through_unchanged(monkeypatch):
    """New path: a list of pre-shaped blocks is forwarded as-is so the
    caller (prompt.pushbutton) can place the cache breakpoint between
    stable instructions and per-turn project state."""
    monkeypatch.setattr(llm_api.config, "get_api_key", lambda: "sk-fake")
    client = llm_api.LLMClient(api_key="sk-fake")
    structured = [
        {"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "DYNAMIC"},
    ]
    blocks = client._system_blocks(structured)  # noqa: SLF001
    assert blocks == structured


def test_build_user_content_no_attachment_returns_string():
    """No attachment → plain string content, cheap and cache-friendly."""
    assert llm_api.build_user_content("salut") == "salut"


def test_build_user_content_image_becomes_base64_image_block(tmp_path):
    raw = b"\x89PNG\r\n\x1a\nfake-png-payload"
    p = tmp_path / "shot.png"
    p.write_bytes(raw)

    content = llm_api.build_user_content("regarde", p)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "regarde"}
    assert content[1]["type"] == "image"
    src = content[1]["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/png"
    assert base64.b64decode(src["data"]) == raw


def test_build_user_content_pdf_becomes_document_block(tmp_path):
    raw = b"%PDF-1.4\n%fake"
    p = tmp_path / "doc.pdf"
    p.write_bytes(raw)

    content = llm_api.build_user_content("résume", p)
    assert content[1]["type"] == "document"
    assert content[1]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(content[1]["source"]["data"]) == raw


def test_build_user_content_text_file_is_inlined(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Titre\nContenu accentué é à ï.", encoding="utf-8")

    content = llm_api.build_user_content("regarde mes notes", p)
    assert content[0]["text"] == "regarde mes notes"
    assert content[1]["type"] == "text"
    body = content[1]["text"]
    assert "--- Fichier joint : notes.md" in body
    assert "# Titre" in body
    assert "--- Fin notes.md ---" in body


def test_build_user_content_handles_non_utf8_text_files(tmp_path):
    """Non-UTF-8 bytes fall back to UTF-8-with-replace rather than raising."""
    p = tmp_path / "weird.txt"
    p.write_bytes(b"hello \xff\xfe world")  # invalid UTF-8.
    content = llm_api.build_user_content("regarde", p)
    assert content[1]["type"] == "text"
    assert "hello" in content[1]["text"]
    assert "world" in content[1]["text"]


def test_build_user_content_rejects_oversize_file(tmp_path):
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * (llm_api.MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(ValueError, match="trop volumineux"):
        llm_api.build_user_content("salut", p)


def test_build_user_content_rejects_unsupported_extension(tmp_path):
    p = tmp_path / "bin.exe"
    p.write_bytes(b"MZ\x90\x00")
    with pytest.raises(ValueError, match="non supporté"):
        llm_api.build_user_content("salut", p)


def test_build_user_content_missing_file_raises(tmp_path):
    with pytest.raises(ValueError, match="introuvable"):
        llm_api.build_user_content("salut", tmp_path / "ghost.png")


# ----- sanitize_history --------------------------------------------------


def _tool_use_block(tid, name="probe"):
    return {"type": "tool_use", "id": tid, "name": name, "input": {}}


def _tool_result_block(tid, content="ok"):
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def test_sanitize_empty_history_is_unchanged():
    assert llm_api.sanitize_history([]) == ([], 0)


def test_sanitize_clean_text_only_history_is_unchanged():
    h = [
        {"role": "user", "content": "salut"},
        {"role": "assistant", "content": [{"type": "text", "text": "bonjour"}]},
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    assert dropped == 0
    assert cleaned == h


def test_sanitize_well_formed_tool_use_chain_is_unchanged():
    h = [
        {"role": "user", "content": "fais X"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "j'utilise un tool"},
            _tool_use_block("toolu_1"),
        ]},
        {"role": "user", "content": [_tool_result_block("toolu_1", "{\"ok\": true}")]},
        {"role": "assistant", "content": [{"type": "text", "text": "fait"}]},
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    assert dropped == 0
    assert cleaned == h


def test_sanitize_drops_dangling_tool_use_at_end():
    """A trailing assistant turn with tool_use but no follow-up gets
    dropped, along with the user prompt that triggered it."""
    h = [
        {"role": "user", "content": "fais X"},
        {"role": "assistant", "content": [_tool_use_block("toolu_1")]},
        # Missing user-tool_result turn — this is the dangling case.
        {"role": "user", "content": "et puis ça"},  # subsequent prompt that broke things
    ]
    # Even the subsequent user prompt can't survive — sanitize keeps the
    # longest VALID prefix, which here is the empty prefix (since the
    # dangling assistant comes right after the first user turn).
    cleaned, dropped = llm_api.sanitize_history(h)
    assert dropped == 3
    assert cleaned == []


def test_sanitize_drops_after_mismatched_tool_result_ids():
    """An assistant turn with `tool_use[id=A]` followed by user with
    `tool_result[id=B]` is mismatched — truncate at the assistant."""
    h = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": [_tool_use_block("toolu_A")]},
        {"role": "user", "content": [_tool_result_block("toolu_B")]},  # wrong id!
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    assert dropped == 3
    assert cleaned == []


def test_sanitize_keeps_valid_prefix_before_dangling_mid_history():
    """A well-formed prefix should be preserved when a later turn breaks."""
    h = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            _tool_use_block("toolu_1"),
        ]},
        {"role": "user", "content": [_tool_result_block("toolu_1")]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        # Now corruption starts:
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": [_tool_use_block("toolu_BAD")]},
        # Missing tool_result — dangling.
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    # 4 entries before the corruption survive ; the orphan user (u2) and
    # the dangling assistant are dropped (the user is also pruned since
    # we end on a "user with no assistant" otherwise).
    assert dropped == 2
    assert len(cleaned) == 4
    assert cleaned[-1]["role"] == "assistant"
    assert cleaned[-1]["content"][0]["text"] == "done"


def test_trim_history_under_cap_is_unchanged():
    h = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]
    trimmed, dropped = llm_api.trim_history_to_max_chars(h, max_chars=1000)
    assert dropped == 0
    assert trimmed == h


def test_trim_history_drops_oldest_until_under_cap():
    h = [
        {"role": "user", "content": "x" * 200},
        {"role": "assistant", "content": "y" * 200},
        {"role": "user", "content": "z" * 200},
        {"role": "assistant", "content": "w" * 200},
    ]
    trimmed, dropped = llm_api.trim_history_to_max_chars(h, max_chars=500)
    assert dropped >= 2
    # We always retain at least 2 trailing entries to keep some context.
    assert len(trimmed) >= 2
    # Output is structurally valid (no dangling tool_use, etc.).
    assert all("role" in t for t in trimmed)


def test_trim_history_runs_sanitize_after_dropping():
    """If we drop an assistant turn with tool_use, the orphan user
    tool_result that follows must also go (sanitize_history)."""
    h = [
        {"role": "user", "content": "x" * 500},  # will be trimmed
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_X", "name": "f", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_X", "content": "ok"},
        ]},
        {"role": "assistant", "content": "final"},
    ]
    # cap to ~600 — should drop the first big user turn at minimum.
    trimmed, dropped = llm_api.trim_history_to_max_chars(h, max_chars=600)
    assert dropped >= 1
    # The resulting history must be Anthropic-valid.
    sanitized, extra = llm_api.sanitize_history(trimmed)
    assert extra == 0  # already sanitized inside trim


def test_trim_history_keeps_minimum_two_entries_even_if_oversize():
    """Hard floor: never return < 2 entries (would lose all context
    mid-conversation)."""
    h = [
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "y" * 5000},
    ]
    trimmed, _ = llm_api.trim_history_to_max_chars(h, max_chars=100)
    assert len(trimmed) >= 2


def test_sanitize_drops_partial_tool_result_coverage():
    """If assistant emits 3 tool_use but user only ack'd 2, truncate."""
    h = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": [
            _tool_use_block("toolu_1"),
            _tool_use_block("toolu_2"),
            _tool_use_block("toolu_3"),
        ]},
        {"role": "user", "content": [
            _tool_result_block("toolu_1"),
            _tool_result_block("toolu_2"),
            # toolu_3 missing → mismatch.
        ]},
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    assert dropped == 3
    assert cleaned == []


def test_sanitize_drops_orphan_user_tool_result_at_head():
    """Cas observé en runtime 2026-05-12 : trim a viré l'assistant
    tool_use, le user tool_result reste orphelin en tête → Anthropic 400
    `unexpected tool_use_id`. Le sanitize doit le détecter et tout drop."""
    h = [
        {"role": "user", "content": [_tool_result_block("toolu_orphan")]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    assert cleaned == []
    assert dropped == 2


def test_sanitize_drops_orphan_user_tool_result_mid_history():
    """User tool_result orphelin au milieu — coupe à partir de l'orphelin."""
    h = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        # Orphan : pas d'assistant tool_use juste avant ce user.
        {"role": "user", "content": [_tool_result_block("toolu_orphan")]},
    ]
    cleaned, dropped = llm_api.sanitize_history(h)
    # Les 2 premiers messages sont sains, l'orphelin est dropped.
    assert len(cleaned) == 2
    assert dropped == 1


def test_trim_then_sanitize_drops_orphan_when_assistant_dropped():
    """Si le trim coupe entre assistant tool_use et user tool_result,
    le sanitize en fin de trim doit cleaner. Validation end-to-end."""
    h = [
        {"role": "user", "content": "x" * 5000},  # gros, sera trim
        {"role": "assistant", "content": [_tool_use_block("toolu_1")]},
        {"role": "user", "content": [_tool_result_block("toolu_1")]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    trimmed, _ = llm_api.trim_history_to_max_chars(h, max_chars=1000)
    # Pas d'orphan user tool_result en tête après trim+sanitize.
    if trimmed and trimmed[0].get("role") == "user":
        tr_ids = llm_api._tool_result_ids(trimmed[0].get("content"))
        assert not tr_ids, "Orphan user tool_result at head of trimmed history"
