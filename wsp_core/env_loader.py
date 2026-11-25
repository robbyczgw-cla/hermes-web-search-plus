"""Shared environment loading helpers for Web Search Plus.

The engine and the Hermes plugin never call :func:`load_env_files`: under
Hermes, credentials come from the active profile through
``wsp_core.credentials.get_secret``. Hosts that vendor ``wsp_core`` (the MCP
server, the OpenClaw skill) may call it from their own entrypoints.

:func:`load_env_files` reads three locations, in precedence order:
1. plugin-local ``.env`` for standalone/plugin development
2. legacy parent ``.env`` used by earlier installs
3. Hermes profile ``.env`` (``$HERMES_HOME/.env`` or hermes_constants)

Values already present in ``os.environ`` are never overwritten.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, MutableMapping, Optional, Union


# Template values copied from .env.example files or setup docs, for example
# "your-brave-api-key-here", "<api-key>", "xxxxxx" or "changeme".
_TEMPLATE_PLACEHOLDER = re.compile(
    r"^(?:<[^<>]*>|\{\{[^{}]*\}\}|\$\{[^{}]*\}|your[-_ ].*|.*[-_ ]here|x{3,}"
    r"|change[-_ ]?me|replace[-_ ]?me|placeholder|insert[-_ ].*|todo)$",
    re.IGNORECASE,
)


def clean_env_value(value: str) -> Optional[str]:
    """Return a real env value, or None for empty/template placeholders."""
    stripped = (value or "").strip().strip('"').strip("'")
    # Keep the historical second quote pass for placeholder recognition while
    # retaining the first pass's value for real credentials.
    placeholder = stripped.strip().strip('"').strip("'")
    if not placeholder or set(placeholder) == {"*"} or _TEMPLATE_PLACEHOLDER.match(placeholder):
        return None
    return stripped


_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def is_truthy(value: object) -> bool:
    """Strict opt-in parse: only 1/true/yes/on are truthy, so a present-but-false value never enables an egress flag."""
    return str(value).strip().strip('"').strip("'").lower() in _TRUTHY_VALUES


def candidate_env_paths(anchor_file: Union[str, Path]) -> List[Path]:
    """Return local, parent and profile .env paths in precedence order."""
    plugin_dir = Path(anchor_file).resolve().parent
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        profile_env = Path(get_hermes_home()) / ".env"
    except Exception:
        # Optional host code can fail with any exception while resolving a
        # profile; standalone MCP must retain its environment/home fallback.
        profile_env = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"
    paths = [plugin_dir / ".env", plugin_dir.parent / ".env", profile_env]
    deduped: dict[str, Path] = {}
    for path in paths:
        deduped.setdefault(str(path.expanduser()), path)
    return list(deduped.values())


def load_env_files(anchor_file: Union[str, Path], environ: Optional[MutableMapping[str, str]] = None) -> List[Path]:
    """Load supported .env files without overriding existing env vars.

    Returns the files that existed and were inspected. Template placeholders such
    as ``***`` are ignored so they do not mask later real credentials.
    """
    target_env = environ if environ is not None else os.environ
    loaded: List[Path] = []
    for env_path in candidate_env_paths(anchor_file):
        if not env_path.exists():
            continue
        loaded.append(env_path)
        for raw_line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            key = key.strip()
            value = clean_env_value(value)
            if key and value and key not in target_env:
                target_env[key] = value
    return loaded
