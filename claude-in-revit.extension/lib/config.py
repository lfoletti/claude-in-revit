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
PROJECTS_SUBDIR = "projects"
KG_FILE_SUFFIX = ".kg.json"
HISTORY_FILE_SUFFIX = ".history.json"
SHARED_PARAMS_FILENAME = "shared_params.txt"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unreadable."""


def config_dir() -> Path:
    """Return the per-user config directory (`~/.config/claude-in-revit/`)."""
    return Path.home() / CONFIG_SUBPATH


def api_key_file() -> Path:
    """Return the path to the on-disk API key file."""
    return config_dir() / API_KEY_FILENAME


def projects_dir() -> Path:
    """Return `~/.config/claude-in-revit/projects/` (per-project KG cache).

    Not auto-created here — `ProjectKG.persist()` materialises the parent
    on first write via `mkdir(parents=True, exist_ok=True)`.
    """
    return config_dir() / PROJECTS_SUBDIR


def kg_path_for(project_id: str) -> Path:
    """Return the on-disk KG path for a given project_id."""
    return projects_dir() / "{}{}".format(project_id, KG_FILE_SUFFIX)


def history_path_for(project_id: str) -> Path:
    """Return the on-disk Anthropic conversation history path.

    Sits alongside the KG file: each click of `prompt.pushbutton` runs in
    a fresh CPython process, so the conversation must be persisted to
    disk to survive between turns (§8 of DESIGN.md mentions a
    `.context.md` companion; for V0 we use a JSON sidecar that mirrors
    the Anthropic `messages=` payload directly — Markdown rendering can
    come later when we want human readability).
    """
    return projects_dir() / "{}{}".format(project_id, HISTORY_FILE_SUFFIX)


def shared_params_file() -> Path:
    """Return the path to the Revit shared parameter file managed by us.

    Single file per machine, used by `revit_primitives.ensure_shared_param_binding`
    to define the `claude-in-revit:llm_id` shared parameter. The file is
    auto-created on first call (Revit format, UTF-16 LE header). Located
    in the same config dir as the API key so a per-user backup captures
    both at once.
    """
    return config_dir() / SHARED_PARAMS_FILENAME


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
