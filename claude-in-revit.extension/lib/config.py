"""config.py — load API key and per-user settings.

Single source of truth for runtime configuration. The design doc §8 fixes the
on-disk hierarchy at `~/.config/claude-in-revit/`:

    ~/.config/claude-in-revit/
    ├── api_key                  # Anthropic key, single line, chmod 600 on Unix
    ├── config.json              # global defaults (not used yet in V0)
    ├── context.md               # cross-project conversation history (V1+)
    ├── projects/<uuid>.kg.json  # per-project KG cache
    └── extensions/              # PyRevit extension cache

For V0 only `get_api_key()` is needed; the other slots will be wired as their
consumers arrive.

Path resolution is lazy (functions, not module-level constants) so tests can
override `Path.home()` and so PyRevit's runtime — which may stage temp HOME
values per click — sees the current value on each call.

Precedence (deliberate, see JOURNAL 2026-05-11):
1. `~/.config/claude-in-revit/api_key` file (canonical, persistent).
2. `ANTHROPIC_API_KEY` env var (slice-era fallback, useful for CI/.env shells).

Rationale: the file is the steady state and survives shell sessions; the env
var is the override for ad-hoc work. We surface a single `ConfigError` rather
than letting `anthropic.AuthenticationError` fire mid-turn with a less actionable
message.
"""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_SUBPATH = Path(".config") / "claude-in-revit"
API_KEY_FILENAME = "api_key"
ENV_VAR_NAME = "ANTHROPIC_API_KEY"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unreadable."""


def config_dir() -> Path:
    """Return the per-user config directory (`~/.config/claude-in-revit/`)."""
    return Path.home() / CONFIG_SUBPATH


def api_key_file() -> Path:
    """Return the path to the on-disk API key file."""
    return config_dir() / API_KEY_FILENAME


def get_api_key() -> str:
    """Return the Anthropic API key.

    File takes precedence over env var. Raises `ConfigError` with an actionable
    message if neither source yields a non-empty value.
    """
    key_file = api_key_file()
    if key_file.exists():
        try:
            content = key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(
                "Cannot read API key file {}: {}".format(key_file, exc)
            ) from exc
        if not content:
            raise ConfigError(
                "API key file {} exists but is empty.".format(key_file)
            )
        return content

    env_value = os.environ.get(ENV_VAR_NAME, "").strip()
    if env_value:
        return env_value

    raise ConfigError(
        "No Anthropic API key found. Either create {} (single line, "
        "chmod 600 on Unix) or set the {} environment variable.".format(
            key_file, ENV_VAR_NAME
        )
    )
