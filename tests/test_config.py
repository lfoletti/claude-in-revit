"""Tests for lib.config — API key loading + precedence.

Each test overrides `Path.home()` to a pytest tmp_path so the real
`~/.config/claude-in-revit/` is never touched. The `ANTHROPIC_API_KEY` env
var is cleared by default and re-set explicitly when we exercise the fallback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib import config


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirect `Path.home()` and clear the env var for hermetic tests."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(config.ENV_VAR_NAME, raising=False)
    return tmp_path


def _write_key(home: Path, content: str) -> Path:
    cfg_dir = home / config.CONFIG_SUBPATH
    cfg_dir.mkdir(parents=True, exist_ok=True)
    key_file = cfg_dir / config.API_KEY_FILENAME
    key_file.write_text(content, encoding="utf-8")
    return key_file


def test_file_returns_key(fake_home):
    _write_key(fake_home, "sk-ant-test-from-file")
    assert config.get_api_key() == "sk-ant-test-from-file"


def test_file_strips_trailing_whitespace(fake_home):
    _write_key(fake_home, "  sk-ant-test  \n\n")
    assert config.get_api_key() == "sk-ant-test"


def test_env_fallback_when_file_missing(fake_home, monkeypatch):
    monkeypatch.setenv(config.ENV_VAR_NAME, "sk-ant-test-from-env")
    assert config.get_api_key() == "sk-ant-test-from-env"


def test_file_wins_over_env(fake_home, monkeypatch):
    _write_key(fake_home, "sk-ant-from-file")
    monkeypatch.setenv(config.ENV_VAR_NAME, "sk-ant-from-env")
    assert config.get_api_key() == "sk-ant-from-file"


def test_empty_file_raises(fake_home):
    _write_key(fake_home, "   \n  ")
    with pytest.raises(config.ConfigError, match="empty"):
        config.get_api_key()


def test_missing_everywhere_raises(fake_home):
    with pytest.raises(config.ConfigError, match="No Anthropic API key found"):
        config.get_api_key()


def test_empty_env_is_treated_as_missing(fake_home, monkeypatch):
    monkeypatch.setenv(config.ENV_VAR_NAME, "   ")
    with pytest.raises(config.ConfigError):
        config.get_api_key()


def test_paths_resolve_under_fake_home(fake_home):
    assert config.config_dir() == fake_home / ".config" / "claude-in-revit"
    assert config.api_key_file() == fake_home / ".config" / "claude-in-revit" / "api_key"


def test_projects_dir_resolves_under_fake_home(fake_home):
    assert config.projects_dir() == fake_home / ".config" / "claude-in-revit" / "projects"


def test_kg_path_for_appends_suffix(fake_home):
    expected = fake_home / ".config" / "claude-in-revit" / "projects" / "abc123.kg.json"
    assert config.kg_path_for("abc123") == expected


def test_history_path_for_appends_suffix(fake_home):
    expected = fake_home / ".config" / "claude-in-revit" / "projects" / "abc123.history.json"
    assert config.history_path_for("abc123") == expected


def test_pending_diffs_path_for_appends_suffix(fake_home):
    expected = (
        fake_home / ".config" / "claude-in-revit" / "projects"
        / "abc123.pending_diffs.jsonl"
    )
    assert config.pending_diffs_path_for("abc123") == expected


def test_hooks_sentinel_starts_absent(fake_home):
    assert not config.are_hooks_disabled()
    assert config.hooks_disabled_file() == (
        fake_home / ".config" / "claude-in-revit" / "hooks.disabled"
    )


def test_hooks_sentinel_toggle_on(fake_home):
    state = config.set_hooks_disabled(True)
    assert state is True
    assert config.are_hooks_disabled()
    assert config.hooks_disabled_file().exists()


def test_hooks_sentinel_toggle_off(fake_home):
    config.set_hooks_disabled(True)
    state = config.set_hooks_disabled(False)
    assert state is False
    assert not config.are_hooks_disabled()
    assert not config.hooks_disabled_file().exists()


def test_hooks_sentinel_toggle_idempotent(fake_home):
    # set_hooks_disabled(False) when sentinel is already absent — no crash.
    state = config.set_hooks_disabled(False)
    assert state is False
    # set_hooks_disabled(True) twice — no crash, still True.
    config.set_hooks_disabled(True)
    state = config.set_hooks_disabled(True)
    assert state is True
