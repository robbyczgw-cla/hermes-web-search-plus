"""
web-search-plus — Hermes Plugin v2.3.1
Multi-provider web search, URL extraction, quality reports, and opt-in research mode.
Ported from robbyczgw-cla/web-search-plus-plugin (OpenClaw) to Hermes Plugin API.
"""
from __future__ import annotations

__version__ = "2.3.1"

import argparse
import getpass
import json
import logging
import os
import shutil
import subprocess
import tempfile
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:  # Package load path used by Hermes plugin discovery.
    from .provider_registry import (
        DEFAULT_AUTO_ALLOW,
        DEFAULT_PROVIDER_PRIORITY,
        EXTRACT_PROVIDER_ENV_KEYS,
        PROVIDER_ENV_KEYS,
        PROVIDER_SPECS,
        plugin_catalog,
    )
except ImportError:  # Direct script/test imports from the plugin directory.
    from provider_registry import (
        DEFAULT_AUTO_ALLOW,
        DEFAULT_PROVIDER_PRIORITY,
        EXTRACT_PROVIDER_ENV_KEYS,
        PROVIDER_ENV_KEYS,
        PROVIDER_SPECS,
        plugin_catalog,
    )

_SEARCH_SCRIPT = Path(__file__).parent / "search.py"
_TOOLSET_NAME = "web-search-plus"
_PROVIDER_ENV_KEYS = list(PROVIDER_ENV_KEYS)
_EXTRACT_PROVIDER_ENV_KEYS = list(EXTRACT_PROVIDER_ENV_KEYS)

logger = logging.getLogger(__name__)


def _clean_env_value(value: str) -> Optional[str]:
    """Return a real env value, or None for empty/template placeholders."""
    stripped = (value or "").strip().strip('"').strip("'")
    return None if not stripped or set(stripped) == {"*"} else stripped


_PROVIDER_CATALOG = plugin_catalog()


def _load_plugin_env() -> None:
    """Load the plugin's .env file into os.environ if keys aren't already set."""
    plugin_env = Path(__file__).parent / ".env"
    if not plugin_env.exists():
        return
    for line in plugin_env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = _clean_env_value(val)
        if val and key not in os.environ:
            os.environ[key] = val

# Load plugin .env on import
_load_plugin_env()


def _get_provider_catalog() -> List[Dict[str, Any]]:
    """Return provider onboarding metadata without exposing secrets."""
    return [dict(item) for item in _PROVIDER_CATALOG]


def _read_env_file(path: Path) -> Dict[str, str]:
    """Read simple KEY=VALUE entries from an env file without exposing secrets."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = _clean_env_value(value)
        if key.strip() and value:
            values[key.strip()] = value
    return values


def _provider_config_status(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Describe configured providers by capability tier.

    No single provider key is globally required. Search and extraction are
    capability-based: one search provider enables web_search_plus; one
    extraction-capable provider enables web_extract_plus.
    """
    env = env if env is not None else os.environ
    providers: Dict[str, Dict[str, Any]] = {}
    configured_count = 0
    configured_search_count = 0
    configured_extract_count = 0
    for item in _PROVIDER_CATALOG:
        key = item["env"]
        configured = _clean_env_value(env.get(key) or "") is not None
        configured_count += int(configured)
        capabilities = item.get("capabilities", [])
        if configured and "search" in capabilities:
            configured_search_count += 1
        if configured and "extract" in capabilities:
            configured_extract_count += 1
        providers[item["provider"]] = {
            "env": key,
            "display_name": item["display_name"],
            "configured": configured,
            "recommended": item.get("recommended", False),
            "capabilities": capabilities,
        }
    return {
        "configured": configured_count > 0,
        "search_configured": configured_search_count > 0,
        "extract_configured": configured_extract_count > 0,
        "configured_count": configured_count,
        "configured_search_count": configured_search_count,
        "configured_extract_count": configured_extract_count,
        "total": len(_PROVIDER_CATALOG),
        "providers": providers,
    }


def _get_hermes_env_path() -> Path:
    """Return Hermes' profile-aware .env path when available."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home()) / ".env"
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".env"




_SETUP_PROVIDER_NAMES = set(PROVIDER_SPECS)
_DEFAULT_PROVIDER_PRIORITY = list(DEFAULT_PROVIDER_PRIORITY)
_DEFAULT_AUTO_ALLOW = dict(DEFAULT_AUTO_ALLOW)
_ROUTING_PROVIDER_NAMES = set(PROVIDER_SPECS)


def _get_plugin_config_path() -> Path:
    """Return the behavior config path shared with search.py."""
    override = os.environ.get("WEB_SEARCH_PLUS_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).parent.parent / "config.json"


def _default_behavior_config() -> Dict[str, Any]:
    return {
        "version": 1,
        "default_provider": None,
        "auto_routing": {
            "enabled": True,
            "fallback_provider": "serper",
            "provider_priority": list(_DEFAULT_PROVIDER_PRIORITY),
            "disabled_providers": [],
            "auto_allow": dict(_DEFAULT_AUTO_ALLOW),
            "confidence_threshold": 0.3,
        },
    }


def _normalize_provider_name(provider: str) -> str:
    """Normalize a setup-provider name from the onboarding catalog."""
    normalized = (provider or "").strip().lower()
    if normalized not in _SETUP_PROVIDER_NAMES:
        valid = ", ".join(sorted(_SETUP_PROVIDER_NAMES))
        print(f"Unknown provider: {provider}. Valid providers: {valid}", file=sys.stderr)
        raise SystemExit(2)
    return normalized


def _normalize_routing_provider(provider: str) -> str:
    """Normalize a provider that search.py can actually route to."""
    normalized = (provider or "").strip().lower()
    if normalized == "kilo_perplexity":
        normalized = "kilo-perplexity"
    if normalized not in _ROUTING_PROVIDER_NAMES:
        valid = ", ".join(sorted(_ROUTING_PROVIDER_NAMES))
        print(f"Unknown routing provider: {provider}. Valid routing providers: {valid}", file=sys.stderr)
        raise SystemExit(2)
    return normalized


def _normalize_provider_csv(value: str, *, routing: bool = True) -> List[str]:
    providers: List[str] = []
    seen = set()
    for raw in (value or "").split(","):
        if not raw.strip():
            continue
        provider = _normalize_routing_provider(raw) if routing else _normalize_provider_name(raw)
        if provider in seen:
            print(f"warning: duplicate provider ignored: {provider}", file=sys.stderr)
            continue
        seen.add(provider)
        providers.append(provider)
    if not providers:
        raise SystemExit("At least one provider is required.")
    return providers


def _append_missing_default_providers(providers: List[str]) -> List[str]:
    seen = set(providers)
    merged = list(providers)
    for provider in _DEFAULT_PROVIDER_PRIORITY:
        if provider not in seen:
            seen.add(provider)
            merged.append(provider)
    return merged


def _merge_behavior_config(user_config: Mapping[str, Any]) -> Dict[str, Any]:
    config = _default_behavior_config()
    if not isinstance(user_config, Mapping):
        return config
    config["version"] = int(user_config.get("version", 1) or 1)
    default_provider = user_config.get("default_provider")
    if default_provider:
        config["default_provider"] = _normalize_routing_provider(str(default_provider))
    auto_user = user_config.get("auto_routing", {}) if isinstance(user_config.get("auto_routing", {}), Mapping) else {}
    auto = dict(config["auto_routing"])
    if "enabled" in auto_user:
        auto["enabled"] = bool(auto_user.get("enabled"))
    if auto_user.get("fallback_provider"):
        auto["fallback_provider"] = _normalize_routing_provider(str(auto_user["fallback_provider"]))
    if auto_user.get("provider_priority"):
        if isinstance(auto_user["provider_priority"], str):
            priority = _normalize_provider_csv(auto_user["provider_priority"], routing=True)
        else:
            priority = _normalize_provider_csv(",".join(str(p) for p in auto_user["provider_priority"]), routing=True)
        auto["provider_priority"] = _append_missing_default_providers(priority) if auto.get("enabled", True) is not False else priority
    if "disabled_providers" in auto_user:
        disabled = auto_user.get("disabled_providers") or []
        if isinstance(disabled, str):
            auto["disabled_providers"] = _normalize_provider_csv(disabled, routing=True) if disabled.strip() else []
        else:
            auto["disabled_providers"] = _normalize_provider_csv(",".join(str(p) for p in disabled), routing=True) if disabled else []
    if "auto_allow" in auto_user:
        raw_allow = auto_user.get("auto_allow") or {}
        if not isinstance(raw_allow, Mapping):
            raise SystemExit("auto_allow must be an object mapping provider names to booleans")
        normalized_allow = dict(_DEFAULT_AUTO_ALLOW)
        for raw_provider, allowed in raw_allow.items():
            normalized_allow[_normalize_routing_provider(str(raw_provider))] = bool(allowed)
        auto["auto_allow"] = normalized_allow
    if "confidence_threshold" in auto_user:
        threshold = float(auto_user["confidence_threshold"])
        if threshold < 0.0 or threshold > 1.0:
            raise SystemExit("confidence_threshold must be between 0.0 and 1.0")
        auto["confidence_threshold"] = threshold
    config["auto_routing"] = auto
    if config["default_provider"] and config["default_provider"] in set(auto.get("disabled_providers", [])):
        raise SystemExit("default_provider cannot be disabled")
    return config


def _unique_timestamped_path(path: Path, marker: str) -> Path:
    base = path.with_name(path.name + f".{marker}-{int(time.time())}")
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(base.name + f"-{suffix}")
        suffix += 1
    return candidate


def _quarantine_behavior_config(path: Path, reason: str) -> None:
    broken = _unique_timestamped_path(path, "broken")
    try:
        path.rename(broken)
        print(f"warning: invalid config moved to {broken}: {reason}", file=sys.stderr)
    except OSError as exc:
        print(f"warning: invalid config could not be moved: {exc}; reason: {reason}", file=sys.stderr)


def _load_behavior_config(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or _get_plugin_config_path()
    if not path.exists():
        return _default_behavior_config()
    try:
        raw = json.loads(path.read_text() or "{}")
        return _merge_behavior_config(raw)
    except json.JSONDecodeError as exc:
        _quarantine_behavior_config(path, str(exc))
        return _default_behavior_config()
    except (SystemExit, ValueError, TypeError) as exc:
        _quarantine_behavior_config(path, str(exc))
        return _default_behavior_config()


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_behavior_config(path: Path, data: Mapping[str, Any], *, dry_run: bool = False, backup: bool = False) -> None:
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if dry_run:
        print(rendered, end="")
        return
    if backup and path.exists():
        backup_path = _unique_timestamped_path(path, "bak")
        shutil.copy2(path, backup_path)
        print(f"Backup written: {backup_path}")
    _atomic_write_json(path, data)


def _routing_summary(config: Mapping[str, Any]) -> str:
    auto = config.get("auto_routing", {}) if isinstance(config.get("auto_routing"), Mapping) else {}
    lines = [
        "Routing:",
        f"  auto-routing: {'on' if auto.get('enabled', True) else 'off'}",
        f"  default provider: {config.get('default_provider') or 'none'}",
        f"  fallback provider: {auto.get('fallback_provider', 'serper')}",
        "  priority: " + ", ".join(auto.get("provider_priority", _DEFAULT_PROVIDER_PRIORITY)),
        "  disabled: " + (", ".join(auto.get("disabled_providers", [])) or "none"),
        "  auto-allow false: " + (
            ", ".join(p for p, allowed in sorted((auto.get("auto_allow") or {}).items()) if allowed is False) or "none"
        ),
        f"  confidence threshold: {auto.get('confidence_threshold', 0.3)}",
    ]
    return "\n".join(lines)


def _status_payload(env: Optional[Mapping[str, str]] = None, config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    return {"providers": _provider_config_status(env), "routing": dict(config or _default_behavior_config())}

def _setup_state_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state" / "web-search-plus-onboarding.json"


def _supports_color() -> bool:
    """Return whether ANSI color should be used for the standalone CLI."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _style(text: str, code: str, *, color: Optional[bool] = None) -> str:
    if color is None:
        color = _supports_color()
    return f"\033[{code}m{text}\033[0m" if color else text


def _capability_badge(enabled: bool, label: str, *, color: Optional[bool] = None) -> str:
    mark = "✓" if enabled else "•"
    rendered = f"{mark} {label}"
    return _style(rendered, "32;1" if enabled else "2", color=color)


def _render_setup_guidance(env: Optional[Mapping[str, str]] = None, *, fancy: bool = False) -> str:
    """Return concise user-facing onboarding guidance."""
    status = _provider_config_status(env)
    if fancy:
        return _render_status_dashboard(status)

    if status["configured"]:
        configured = [
            meta["display_name"]
            for meta in status["providers"].values()
            if meta["configured"]
        ]
        lines = ["web-search-plus is configured. Providers: " + ", ".join(configured)]
        lines.append(
            "Capabilities: "
            f"search={'yes' if status['search_configured'] else 'no'}, "
            f"extraction={'yes' if status['extract_configured'] else 'no'}"
        )
        if status["search_configured"] and not status["extract_configured"]:
            lines.append(
                "Tip: add LINKUP_API_KEY or another extraction key for web_extract_plus."
            )
        return "\n".join(lines)

    lines = [
        "web-search-plus is installed but no provider keys are configured.",
        "No single key is mandatory, but at least one search-capable provider is needed for web_search_plus.",
        "Add LINKUP_API_KEY or another extraction-capable provider for web_extract_plus.",
        "Run `python ~/.hermes/plugins/web-search-plus/setup.py setup` to walk through every supported provider, or add `--preset starter` for the short path.",
        "",
        "Recommended starter providers:",
    ]
    for item in _PROVIDER_CATALOG:
        if item.get("recommended"):
            lines.append(
                f"- {item['display_name']} ({item['env']}): {item['description']} "
                f"Free tier: {item['free_tier']}. Signup: {item['signup_url']}"
            )
    return "\n".join(lines)


def _render_status_dashboard(status: Optional[Dict[str, Any]] = None, *, color: Optional[bool] = None) -> str:
    """Render a compact, premium-feeling status dashboard for humans."""
    status = status or _provider_config_status()
    if color is None:
        color = _supports_color()
    configured = [
        meta["display_name"]
        for meta in status["providers"].values()
        if meta["configured"]
    ]
    title = _style("web-search-plus", "36;1", color=color)
    subtitle = "provider setup"
    lines = [
        f"╭─ {title} {subtitle} " + "─" * 28,
        "│ " + "  ".join([
            _capability_badge(status["search_configured"], "search", color=color),
            _capability_badge(status["extract_configured"], "extraction", color=color),
        ]),
        f"│ Providers: {status['configured_count']}/{status['total']} configured",
    ]
    if configured:
        lines.append("│ Active: " + ", ".join(configured))
    else:
        lines.append("│ Active: none yet — add one search provider to unlock the tools")
    if status["search_configured"] and not status["extract_configured"]:
        lines.append("│ Tip: add Linkup for clean web_extract_plus markdown.")
    elif not status["search_configured"]:
        lines.append("│ Starter: You + Serper + Linkup is the best first setup.")
    lines.extend([
        "╰─ Next commands",
        "   python ~/.hermes/plugins/web-search-plus/setup.py setup",
        "   python ~/.hermes/plugins/web-search-plus/setup.py list",
        "   python ~/.hermes/plugins/web-search-plus/search.py --query \"Hermes Agent latest release\" --quality-report",
    ])
    return "\n".join(lines)


def _render_provider_catalog(*, json_output: bool = False, color: Optional[bool] = None) -> str:
    """Render provider metadata for either scripts or humans."""
    catalog = _get_provider_catalog()
    if json_output:
        return json.dumps(catalog, indent=2)
    if color is None:
        color = _supports_color()
    lines = [_style("Providers", "36;1", color=color)]
    for item in catalog:
        star = _style("★", "33;1", color=color) if item.get("recommended") else " "
        caps = ", ".join(item.get("capabilities", []))
        lines.append(f"{star} {item['provider']:<10} {item['display_name']}")
        lines.append(f"    env: {item['env']}  caps: {caps}")
        lines.append(f"    {item['description']}")
        lines.append(f"    free: {item['free_tier']}  signup: {item['signup_url']}")
    lines.append("\n★ recommended starter providers")
    return "\n".join(lines)


def _providers_for_preset(preset: str) -> List[Dict[str, Any]]:
    """Return provider catalog entries for a named setup preset."""
    preset = preset.lower().strip()
    if preset == "starter":
        names = {"you", "serper", "linkup"}
    elif preset == "lean":
        names = {"you", "linkup"}
    elif preset == "search":
        names = {"you", "serper", "exa", "firecrawl", "tavily", "linkup"}
    elif preset == "extract":
        names = {"linkup", "firecrawl", "tavily"}
    elif preset == "all":
        names = {item["provider"] for item in _PROVIDER_CATALOG}
    else:
        raise SystemExit(f"Unknown preset: {preset}. Choose starter, lean, search, extract, or all.")
    return [item for item in _PROVIDER_CATALOG if item["provider"] in names]


def _upsert_env_values(env_path: Path, values: Mapping[str, str]) -> Dict[str, List[str]]:
    """Insert/update env values in a .env file. Caller owns secret prompting."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_path.read_text().splitlines() if env_path.exists() else []
    keys = set(values)
    seen = set()
    added: List[str] = []
    updated: List[str] = []
    output: List[str] = []

    for line in existing_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key, _, _old = line.partition("=")
        clean_key = key.strip()
        if clean_key in keys:
            output.append(f"{clean_key}={values[clean_key]}")
            updated.append(clean_key)
            seen.add(clean_key)
        else:
            output.append(line)

    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
            added.append(key)

    env_path.write_text("\n".join(output).rstrip() + "\n")
    return {"updated": updated, "added": added}


def _unconfigured_session_hint(
    env: Optional[Mapping[str, str]] = None,
    state_path: Optional[Path] = None,
) -> Optional[Dict[str, str]]:
    """Return a one-shot unconfigured hint payload, recording acknowledgement in state."""
    if _provider_config_status(env)["configured"]:
        return None
    state_path = state_path or _setup_state_path()
    try:
        if state_path.exists():
            data = json.loads(state_path.read_text() or "{}")
            if data.get("unconfigured_hint_shown"):
                return None
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"unconfigured_hint_shown": True}, indent=2) + "\n")
    except Exception as exc:
        logger.debug("web-search-plus onboarding state write failed: %s", exc)
    return {
        "action": "hint",
        "message": "web-search-plus loaded but no provider keys are configured. Run `python ~/.hermes/plugins/web-search-plus/setup.py setup`.",
    }


def _web_search_plus_cli_setup(parser: argparse.ArgumentParser) -> None:
    parser.description = "Configure web-search-plus provider keys with a tiny, secret-safe wizard."
    parser.epilog = (
        "Default setup prompts every provider. Presets: starter=You+Serper+Linkup, lean=You+Linkup, "
        "search=You+Serper+Exa+Firecrawl+Tavily+Linkup, extract=Linkup+Firecrawl+Tavily."
    )
    subs = parser.add_subparsers(dest="web_search_plus_command")
    status = subs.add_parser("status", help="Show a setup dashboard without printing secrets")
    status.add_argument("--plain", action="store_true", help="Print compact legacy text instead of the dashboard")
    status.add_argument("--json", action="store_true", help="Print status as JSON")
    status.add_argument("--env-path", help="Override Hermes .env path for status checks")
    status.add_argument("--config-path", help="Override web-search-plus config.json path")

    setup = subs.add_parser("setup", help="Run the provider-key setup wizard")
    setup.add_argument("providers", nargs="*", help="Provider names to configure (overrides --preset)")
    setup.add_argument("--preset", default="all", help="starter, lean, search, extract, or all (default: all)")
    setup.add_argument("--open", action="store_true", help="Open signup URLs in a browser before prompting")
    setup.add_argument("--env-path", help="Override Hermes .env path")
    setup.add_argument("--config-path", help="Override web-search-plus config.json path")
    setup.add_argument("--show-values", action="store_true", help="Use visible input instead of hidden secret prompts")
    setup.add_argument("--dry-run", action="store_true", help="Show the setup/routing plan without writing files")
    setup.add_argument("--routing", choices=["auto", "fixed"], help="Persist routing mode after key setup")
    setup.add_argument("--default-provider", help="Provider to use when routing is fixed/off")
    setup.add_argument("--provider-priority", help="Comma-separated auto-routing priority")
    setup.add_argument("--disable-providers", help="Comma-separated providers to exclude from auto-routing")
    setup.add_argument("--auto-allow", help="Comma-separated providers allowed in auto-routing")
    setup.add_argument("--auto-deny", help="Comma-separated providers blocked from auto-routing but still usable explicitly")
    setup.add_argument("--fallback-provider", help="Fallback provider when no route is available")
    setup.add_argument("--confidence-threshold", type=float, help="Auto-routing confidence threshold 0.0-1.0")

    list_cmd = subs.add_parser("list", help="List supported providers, capabilities, and signup URLs")
    list_cmd.add_argument("--json", action="store_true", help="Print provider catalog as JSON")

    config_cmd = subs.add_parser("config", help="Inspect or change routing preferences")
    config_subs = config_cmd.add_subparsers(dest="config_command")
    show = config_subs.add_parser("show", help="Show routing config")
    show.add_argument("--json", action="store_true")
    show.add_argument("--config-path")
    set_routing = config_subs.add_parser("set-routing", help="Turn auto-routing on or off")
    set_routing.add_argument("mode", choices=["on", "off"])
    set_routing.add_argument("--config-path")
    set_routing.add_argument("--dry-run", action="store_true")
    set_default = config_subs.add_parser("set-default", help="Use one fixed provider when auto-routing is off")
    set_default.add_argument("provider")
    set_default.add_argument("--config-path")
    set_default.add_argument("--dry-run", action="store_true")
    set_fallback = config_subs.add_parser("set-fallback", help="Set fallback provider")
    set_fallback.add_argument("provider")
    set_fallback.add_argument("--config-path")
    set_fallback.add_argument("--dry-run", action="store_true")
    set_priority = config_subs.add_parser("set-priority", help="Set comma-separated auto-routing priority")
    set_priority.add_argument("providers")
    set_priority.add_argument("--config-path")
    set_priority.add_argument("--dry-run", action="store_true")
    disable = config_subs.add_parser("disable", help="Disable a provider for auto-routing")
    disable.add_argument("provider")
    disable.add_argument("--config-path")
    disable.add_argument("--dry-run", action="store_true")
    enable = config_subs.add_parser("enable", help="Re-enable a provider for auto-routing")
    enable.add_argument("provider")
    enable.add_argument("--config-path")
    enable.add_argument("--dry-run", action="store_true")
    allow_auto = config_subs.add_parser("set-auto-allow", help="Allow or block a provider from automatic routing/fallback")
    allow_auto.add_argument("provider")
    allow_auto.add_argument("mode", choices=["on", "off", "true", "false", "yes", "no"])
    allow_auto.add_argument("--config-path")
    allow_auto.add_argument("--dry-run", action="store_true")
    threshold = config_subs.add_parser("set-threshold", help="Set routing confidence threshold")
    threshold.add_argument("value", type=float)
    threshold.add_argument("--config-path")
    threshold.add_argument("--dry-run", action="store_true")
    reset = config_subs.add_parser("reset", help="Reset routing config to defaults and back up existing config")
    reset.add_argument("--config-path")
    reset.add_argument("--dry-run", action="store_true")
    reset.add_argument("--yes", action="store_true")
    parser.set_defaults(func=_web_search_plus_cli_command)


def _apply_setup_routing_args(config: Dict[str, Any], args: Any) -> Dict[str, Any]:
    updated = _merge_behavior_config(config)
    auto = dict(updated["auto_routing"])
    if getattr(args, "routing", None):
        auto["enabled"] = getattr(args, "routing") == "auto"
    if getattr(args, "default_provider", None):
        updated["default_provider"] = _normalize_routing_provider(getattr(args, "default_provider"))
        auto["enabled"] = False
    if getattr(args, "provider_priority", None):
        auto["provider_priority"] = _normalize_provider_csv(getattr(args, "provider_priority"), routing=True)
    if getattr(args, "disable_providers", None):
        auto["disabled_providers"] = _normalize_provider_csv(getattr(args, "disable_providers"), routing=True)
    auto_allow = dict(auto.get("auto_allow") or _DEFAULT_AUTO_ALLOW)
    if getattr(args, "auto_allow", None):
        for provider in _normalize_provider_csv(getattr(args, "auto_allow"), routing=True):
            auto_allow[provider] = True
    if getattr(args, "auto_deny", None):
        for provider in _normalize_provider_csv(getattr(args, "auto_deny"), routing=True):
            auto_allow[provider] = False
    auto["auto_allow"] = auto_allow
    if getattr(args, "fallback_provider", None):
        auto["fallback_provider"] = _normalize_routing_provider(getattr(args, "fallback_provider"))
    if getattr(args, "confidence_threshold", None) is not None:
        value = float(getattr(args, "confidence_threshold"))
        if value < 0.0 or value > 1.0:
            raise SystemExit("confidence threshold must be between 0.0 and 1.0")
        auto["confidence_threshold"] = value
    updated["auto_routing"] = auto
    return _merge_behavior_config(updated)


def _handle_config_command(args: Any) -> None:
    subcommand = getattr(args, "config_command", None) or "show"
    path = Path(getattr(args, "config_path", None) or _get_plugin_config_path())
    config = _load_behavior_config(path)
    dry_run = bool(getattr(args, "dry_run", False))

    if subcommand == "show":
        if getattr(args, "json", False):
            print(json.dumps(config, indent=2, sort_keys=True))
        else:
            print(_routing_summary(config))
        return

    if subcommand == "set-routing":
        config["auto_routing"]["enabled"] = getattr(args, "mode") == "on"
    elif subcommand == "set-default":
        provider = _normalize_routing_provider(getattr(args, "provider"))
        config["default_provider"] = provider
        config["auto_routing"]["enabled"] = False
    elif subcommand == "set-fallback":
        config["auto_routing"]["fallback_provider"] = _normalize_routing_provider(getattr(args, "provider"))
    elif subcommand == "set-priority":
        config["auto_routing"]["provider_priority"] = _normalize_provider_csv(getattr(args, "providers"), routing=True)
    elif subcommand == "disable":
        provider = _normalize_routing_provider(getattr(args, "provider"))
        disabled = list(config["auto_routing"].get("disabled_providers", []))
        if provider == config.get("default_provider"):
            raise SystemExit("default_provider cannot be disabled")
        if provider not in disabled:
            disabled.append(provider)
        config["auto_routing"]["disabled_providers"] = disabled
    elif subcommand == "enable":
        provider = _normalize_routing_provider(getattr(args, "provider"))
        config["auto_routing"]["disabled_providers"] = [p for p in config["auto_routing"].get("disabled_providers", []) if p != provider]
    elif subcommand == "set-auto-allow":
        provider = _normalize_routing_provider(getattr(args, "provider"))
        mode = str(getattr(args, "mode")).lower()
        auto_allow = dict(config["auto_routing"].get("auto_allow") or _DEFAULT_AUTO_ALLOW)
        auto_allow[provider] = mode in {"on", "true", "yes"}
        config["auto_routing"]["auto_allow"] = auto_allow
    elif subcommand == "set-threshold":
        value = float(getattr(args, "value"))
        if value < 0.0 or value > 1.0:
            raise SystemExit("confidence threshold must be between 0.0 and 1.0")
        config["auto_routing"]["confidence_threshold"] = value
    elif subcommand == "reset":
        if not getattr(args, "yes", False) and not dry_run:
            raise SystemExit("Refusing to reset without --yes. Use --dry-run to preview.")
        config = _default_behavior_config()
        _write_behavior_config(path, config, dry_run=dry_run, backup=True)
        if not dry_run:
            print(f"✓ Reset routing config: {path}")
        return
    else:
        raise SystemExit(f"Unknown config command: {subcommand}")

    config = _merge_behavior_config(config)
    _write_behavior_config(path, config, dry_run=dry_run)
    if not dry_run:
        print(f"✓ Updated routing config: {path}")
        print(_routing_summary(config))


def _web_search_plus_cli_command(args: Any) -> None:
    command = getattr(args, "web_search_plus_command", None) or "status"
    if command == "list":
        print(_render_provider_catalog(json_output=getattr(args, "json", False)))
        return

    if command == "config":
        _handle_config_command(args)
        return

    if command == "status":
        env_path = getattr(args, "env_path", None)
        config_path = getattr(args, "config_path", None)
        env = _read_env_file(Path(env_path)) if env_path else None
        config = _load_behavior_config(Path(config_path)) if config_path else _load_behavior_config()
        if getattr(args, "json", False):
            print(json.dumps(_status_payload(env, config), indent=2, sort_keys=True))
        else:
            print(_render_setup_guidance(env=env, fancy=not getattr(args, "plain", False)))
            print("\n" + _routing_summary(config))
        return

    if command == "setup":
        selected = set(getattr(args, "providers", None) or [])
        selected = {_normalize_provider_name(p) for p in selected} if selected else set()
        catalog = [item for item in _PROVIDER_CATALOG if item["provider"] in selected] if selected else _providers_for_preset(getattr(args, "preset", "all"))
        if not catalog:
            raise SystemExit("No matching providers. Run `python ~/.hermes/plugins/web-search-plus/setup.py list`.")

        env_path = Path(getattr(args, "env_path", None) or _get_hermes_env_path())
        config_path = Path(getattr(args, "config_path", None) or _get_plugin_config_path())
        config = _apply_setup_routing_args(_load_behavior_config(config_path), args)
        print(_render_status_dashboard(_provider_config_status(_read_env_file(env_path))))
        print("\nSetup plan:")
        for item in catalog:
            rec = " recommended" if item.get("recommended") else ""
            caps = ", ".join(item.get("capabilities", []))
            print(f"  • {item['display_name']} ({item['provider']}) — {item['env']} — {caps}{rec}")
            print(f"    {item['signup_url']}")
        print(f"\nTarget env file: {env_path}")
        print(f"Target config file: {config_path}")
        print(_routing_summary(config))
        if getattr(args, "dry_run", False):
            print("Dry run only; no keys or routing config written.")
            return

        values: Dict[str, str] = {}
        for item in catalog:
            if getattr(args, "open", False):
                webbrowser.open(item["signup_url"])
            prompt = f"{item['display_name']} key ({item['env']}, Enter to skip): "
            try:
                if getattr(args, "show_values", False):
                    value = input(prompt).strip()
                else:
                    value = getpass.getpass(prompt).strip()
            except EOFError:
                value = ""
            if value:
                values[item["env"]] = value
        routing_args_present = any(
            getattr(args, name, None) is not None
            for name in ["routing", "default_provider", "provider_priority", "disable_providers", "fallback_provider", "confidence_threshold"]
        )
        wrote_any = False
        if values:
            result = _upsert_env_values(env_path, values)
            changed = sorted(result["updated"] + result["added"])
            print(f"\n✓ Configured {len(changed)} provider key(s) in {env_path}: " + ", ".join(changed))
            print("✓ Secrets were not printed.")
            wrote_any = True
        if routing_args_present:
            _write_behavior_config(config_path, config)
            print(f"✓ Saved routing preferences in {config_path}")
            wrote_any = True
        if not wrote_any:
            print("No keys entered; nothing changed.")
            return
        print("Next: restart Hermes or run /reset so tools re-register with the new credentials/preferences.")
        return

    raise SystemExit(f"Unknown web-search-plus command: {command}")

def _web_search_plus_slash_setup(raw_args: str = "") -> str:
    """In-session lightweight status/help command."""
    return _render_setup_guidance()


def _on_session_start(**kwargs: Any) -> Optional[Dict[str, str]]:
    hint = _unconfigured_session_hint()
    if hint:
        logger.info(hint["message"])
    return hint


def _run_search(
    query: str,
    provider: str = "auto",
    count: int = 5,
    exa_depth: str = "normal",
    time_range: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    mode: str = "normal",
    quality_report: bool = False,
    research_time_budget: float = 55.0,
    language: Optional[str] = None,
    country: Optional[str] = None,
    subprocess_timeout: int = 75,
) -> dict:
    """Call search.py subprocess and return parsed JSON result."""
    cmd = [
        sys.executable,
        str(_SEARCH_SCRIPT),
        "--query", query,
        "--provider", provider,
        "--max-results", str(count),
        "--compact",
    ]
    if exa_depth != "normal":
        cmd += ["--exa-depth", exa_depth]
    if time_range and time_range != "none":
        cmd += ["--time-range", time_range]
    if include_domains:
        cmd += ["--include-domains"] + include_domains
    if exclude_domains:
        cmd += ["--exclude-domains"] + exclude_domains
    if mode != "normal":
        cmd += ["--mode", mode, "--research-time-budget", str(research_time_budget)]
        if mode == "research":
            subprocess_timeout = max(subprocess_timeout, int(research_time_budget) + 15)
    if quality_report:
        cmd.append("--quality-report")
    if language and language != "auto":
        cmd += ["--language", language]
    if country and country != "auto":
        cmd += ["--country", country]

    env = os.environ.copy()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=subprocess_timeout,
            env=env,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            try:
                return json.loads(stderr)
            except json.JSONDecodeError:
                return {"error": stderr or "Search failed", "provider": provider, "query": query, "results": []}

        return json.loads(result.stdout)

    except subprocess.TimeoutExpired:
        return {"error": f"Search timed out after {subprocess_timeout}s", "provider": provider, "query": query, "results": []}
    except Exception as e:
        return {"error": str(e), "provider": provider, "query": query, "results": []}


def _run_extract(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    subprocess_timeout: int = 90,
) -> dict:
    """Call search.py extract mode and return parsed JSON result."""
    cmd = [
        sys.executable,
        str(_SEARCH_SCRIPT),
        "--extract-urls",
        *urls,
        "--provider",
        provider,
        "--format",
        output_format,
        "--compact",
    ]
    if include_images:
        cmd.append("--extract-images")
    if include_raw_html:
        cmd.append("--include-raw-html")
    if render_js:
        cmd.append("--render-js")

    env = os.environ.copy()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=subprocess_timeout, env=env)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            try:
                return json.loads(stderr)
            except json.JSONDecodeError:
                return {"error": stderr or "Extract failed", "provider": provider, "results": []}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"error": f"Extract timed out after {subprocess_timeout}s", "provider": provider, "results": []}
    except Exception as e:
        return {"error": str(e), "provider": provider, "results": []}


def _format_results(data: dict) -> str:
    """Format search results for LLM consumption."""
    if "error" in data and not data.get("results"):
        return f"Search error: {data['error']}"

    results = data.get("results", [])
    provider = data.get("provider", "unknown")
    routing = data.get("routing", {})
    answer = data.get("answer", "")
    cached = data.get("cached", False)

    lines = []

    if routing.get("auto_routed"):
        confidence = routing.get("confidence_level", "")
        reason = routing.get("reason", "")
        lines.append(f"[Provider: {provider} | auto-routed | {confidence} confidence | {reason}]")
    else:
        lines.append(f"[Provider: {provider}{' | cached' if cached else ''}]")

    if answer:
        lines.append(f"\nAnswer: {answer}\n")

    quality_report = data.get("quality_report") or {}
    if quality_report:
        lines.append(
            "Quality: "
            f"{quality_report.get('confidence', 'unknown')} confidence | "
            f"{quality_report.get('domain_count', 0)} domains | "
            f"{quality_report.get('duplicate_count', 0)} duplicates | "
            f"extract recommended: {quality_report.get('extract_recommended', False)}"
        )
        if quality_report.get("extract_reasons"):
            lines.append("Quality reasons: " + ", ".join(quality_report["extract_reasons"]))
        lines.append("")

    source_summaries = data.get("source_summaries") or []
    if source_summaries:
        lines.append("Extracted source summaries:")
        for i, src in enumerate(source_summaries, 1):
            url = src.get("url", "")
            content = (src.get("content") or src.get("raw_content") or "").strip()
            lines.append(f"{i}. {url}")
            if content:
                lines.append(f"   {content[:500]}")
        lines.append("")

    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        snippet = r.get("snippet", "")
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_extract_results(data: dict) -> str:
    """Format extracted URL content for LLM consumption."""
    if "error" in data and not data.get("results"):
        return f"Extract error: {data['error']}"
    provider = data.get("provider", "unknown")
    lines = [f"[Provider: {provider}]"]
    for i, r in enumerate(data.get("results", []), 1):
        title = r.get("title") or "No title"
        url = r.get("url", "")
        content = r.get("content") or r.get("raw_content") or ""
        lines.append(f"\n{i}. {title}")
        if url:
            lines.append(url)
        if r.get("error"):
            lines.append(f"Error: {r['error']}")
        elif content:
            lines.append(content)
    return "\n".join(lines).strip()


def register(ctx: Any) -> None:
    """Register web-search-plus tools with Hermes plugin system."""

    schema = {
        "name": "web_search_plus",
        "description": (
            "Multi-provider web search with intelligent auto-routing. "
            "Automatically selects the best provider based on query intent: "
            "Serper for shopping/news/facts, Tavily for research/analysis, "
            "Exa for semantic discovery, "
            "Brave for general web search, "
            "Linkup for source-backed grounding/citations, "
            "Firecrawl for web search plus optional scrape-ready results, "
            "Perplexity for direct answers, You.com for real-time snippets, "
            "SearXNG for privacy-focused/self-hosted search, and SerpBase/Querit only when explicitly enabled or forced. "
            "Set depth='deep' for Exa multi-source synthesis, 'deep-reasoning' for complex cross-document analysis. "
            "Override with provider param if needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "provider": {
                    "type": "string",
                    "enum": ["auto", "serper", "serpbase", "brave", "tavily", "exa", "querit", "linkup", "firecrawl", "parallel", "perplexity", "kilo-perplexity", "you", "searxng"],
                    "description": "Search provider. Use 'auto' for intelligent routing (default). Brave and Serper share generic web-search intents and ties are distributed deterministically per query.",
                    "default": "auto",
                },
                "depth": {
                    "type": "string",
                    "enum": ["normal", "deep", "deep-reasoning"],
                    "description": "Exa search depth: 'deep' synthesizes across sources (4-12s), 'deep-reasoning' for complex cross-document analysis (12-50s). Only applies when routed to Exa.",
                    "default": "normal",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "Filter results by recency. Optional.",
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Whitelist specific domains (e.g. ['arxiv.org', 'github.com']). Optional.",
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Blacklist specific domains (e.g. ['reddit.com']). Optional.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["normal", "research"],
                    "description": "normal = fast routed search; research = multi-provider search plus top-source extraction.",
                    "default": "normal",
                },
                "quality_report": {
                    "type": "boolean",
                    "description": "Attach routing/result quality diagnostics such as selected provider, skips, dedup count, domain diversity, and extraction recommendation.",
                    "default": False,
                },
                "research_time_budget": {
                    "type": "number",
                    "description": "Best-effort wall-clock budget in seconds for research mode. Checked between provider calls and before extraction.",
                    "default": 55.0,
                    "minimum": 1,
                    "maximum": 75,
                },
            },
            "required": ["query"],
        },
    }

    def handler(args_or_query, provider: str = "auto", count: int = 5, depth: str = "normal",
                time_range: Optional[str] = None, include_domains: Optional[List[str]] = None,
                exclude_domains: Optional[List[str]] = None, mode: str = "normal",
                quality_report: bool = False, research_time_budget: float = 55.0, **kwargs) -> str:
        # Hermes registry passes the entire input dict as first positional arg
        if isinstance(args_or_query, dict):
            query = args_or_query.get("query", "")
            provider = args_or_query.get("provider", provider)
            count = args_or_query.get("count", count)
            depth = args_or_query.get("depth", depth)
            time_range = args_or_query.get("time_range", time_range)
            include_domains = args_or_query.get("include_domains", include_domains)
            exclude_domains = args_or_query.get("exclude_domains", exclude_domains)
            mode = args_or_query.get("mode", mode)
            quality_report = args_or_query.get("quality_report", quality_report)
            research_time_budget = args_or_query.get("research_time_budget", research_time_budget)
        else:
            query = args_or_query
        data = _run_search(
            query=query,
            provider=provider,
            count=count,
            exa_depth=depth,
            time_range=time_range,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            mode=mode,
            quality_report=quality_report,
            research_time_budget=research_time_budget,
        )
        return _format_results(data)

    def check_fn() -> bool:
        """Search is available if at least one search provider credential is configured."""
        return any(os.environ.get(k) for k in _PROVIDER_ENV_KEYS)

    def extract_check_fn() -> bool:
        """Extraction is available if at least one extraction-capable provider credential is configured."""
        return any(os.environ.get(k) for k in _EXTRACT_PROVIDER_ENV_KEYS)

    ctx.register_tool(
        name="web_search_plus",
        toolset=_TOOLSET_NAME,
        schema=schema,
        handler=handler,
        check_fn=check_fn,
        requires_env=[],
        description="Multi-provider web search with intelligent auto-routing",
        emoji="🔍",
    )

    extract_schema = {
        "name": "web_extract_plus",
        "description": (
            "Multi-provider URL content extraction. Auto tries Tavily, Exa, Linkup, "
            "Firecrawl, then You.com; force a provider for robust scraping, clean markdown, or explicit fallback tests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to extract"},
                "provider": {"type": "string", "enum": ["auto", "firecrawl", "linkup", "parallel", "tavily", "exa", "you"], "default": "auto"},
                "format": {"type": "string", "enum": ["markdown", "html"], "default": "markdown"},
                "include_images": {"type": "boolean", "default": False},
                "include_raw_html": {"type": "boolean", "default": False},
                "render_js": {"type": "boolean", "default": False},
            },
            "required": ["urls"],
        },
    }

    def extract_handler(args_or_urls, provider: str = "auto", format: str = "markdown",
                        include_images: bool = False, include_raw_html: bool = False,
                        render_js: bool = False, **kwargs) -> str:
        if isinstance(args_or_urls, dict):
            urls = args_or_urls.get("urls", [])
            provider = args_or_urls.get("provider", provider)
            format = args_or_urls.get("format", format)
            include_images = args_or_urls.get("include_images", include_images)
            include_raw_html = args_or_urls.get("include_raw_html", include_raw_html)
            render_js = args_or_urls.get("render_js", render_js)
        else:
            urls = args_or_urls
        if isinstance(urls, str):
            urls = [urls]
        data = _run_extract(
            urls=urls,
            provider=provider,
            output_format=format,
            include_images=include_images,
            include_raw_html=include_raw_html,
            render_js=render_js,
        )
        return _format_extract_results(data)

    ctx.register_tool(
        name="web_extract_plus",
        toolset=_TOOLSET_NAME,
        schema=extract_schema,
        handler=extract_handler,
        check_fn=extract_check_fn,
        requires_env=[],
        description="Multi-provider URL extraction",
        emoji="📄",
    )

    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="web-search-plus-setup",
            handler=_web_search_plus_slash_setup,
            description="Show Web Search Plus provider setup status and starter-key guidance.",
            args_hint="",
        )

    if hasattr(ctx, "register_hook"):
        ctx.register_hook("on_session_start", _on_session_start)
