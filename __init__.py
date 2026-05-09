"""
web-search-plus — Hermes Plugin v1.8.0
Multi-provider web search, URL extraction, quality reports, and opt-in research mode.
Ported from robbyczgw-cla/web-search-plus-plugin (OpenClaw) to Hermes Plugin API.
"""
from __future__ import annotations

__version__ = "1.8.0"

import argparse
import getpass
import html
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

_SEARCH_SCRIPT = Path(__file__).parent / "search.py"
_TOOLSET_NAME = "web-search-plus"
_PROVIDER_ENV_KEYS = [
    "SERPER_API_KEY",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "QUERIT_API_KEY",
    "LINKUP_API_KEY",
    "FIRECRAWL_API_KEY",
    "PERPLEXITY_API_KEY",
    "KILOCODE_API_KEY",
    "YOU_API_KEY",
    "SEARXNG_INSTANCE_URL",
]
_EXTRACT_PROVIDER_ENV_KEYS = [
    "FIRECRAWL_API_KEY",
    "LINKUP_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "YOU_API_KEY",
]

logger = logging.getLogger(__name__)
_PROVIDER_CATALOG = [
    {
        "provider": "tavily",
        "env": "TAVILY_API_KEY",
        "display_name": "Tavily",
        "description": "Recommended starter for research/news-style search.",
        "free_tier": "1,000 free searches/month",
        "signup_url": "https://tavily.com",
        "capabilities": ["search", "extract", "research"],
        "recommended": True,
    },
    {
        "provider": "linkup",
        "env": "LINKUP_API_KEY",
        "display_name": "Linkup",
        "description": "Best starter for cheap clean extraction and citation-grounded retrieval.",
        "free_tier": "€5 free monthly credits (~5,000 standard extracts)",
        "signup_url": "https://www.linkup.so",
        "capabilities": ["search", "extract", "citations"],
        "recommended": True,
    },
    {
        "provider": "brave",
        "env": "BRAVE_API_KEY",
        "display_name": "Brave Search",
        "description": "Independent general web index; useful for fresh and local web results.",
        "free_tier": "$5 free monthly credits",
        "signup_url": "https://api.search.brave.com/app/keys",
        "capabilities": ["search", "news", "local"],
        "recommended": True,
    },
    {
        "provider": "exa",
        "env": "EXA_API_KEY",
        "display_name": "Exa",
        "description": "Semantic discovery, alternatives, docs, academic and long-form discovery.",
        "free_tier": "1,000 free searches/month",
        "signup_url": "https://dashboard.exa.ai/api-keys",
        "capabilities": ["search", "extract", "semantic"],
        "recommended": False,
    },
    {
        "provider": "firecrawl",
        "env": "FIRECRAWL_API_KEY",
        "display_name": "Firecrawl",
        "description": "Robust scraping/extraction fallback, especially for JS-heavy pages.",
        "free_tier": "500 one-time credits",
        "signup_url": "https://www.firecrawl.dev/app/api-keys",
        "capabilities": ["search", "extract", "js"],
        "recommended": False,
    },
    {
        "provider": "serper",
        "env": "SERPER_API_KEY",
        "display_name": "Serper",
        "description": "Google-like SERP results for facts, shopping, local and news queries.",
        "free_tier": "2,500 one-time credits",
        "signup_url": "https://serper.dev/api-key",
        "capabilities": ["search", "news", "shopping", "local"],
        "recommended": False,
    },
    {
        "provider": "querit",
        "env": "QUERIT_API_KEY",
        "display_name": "Querit",
        "description": "Multilingual and real-time search candidate.",
        "free_tier": "1,000 free searches/month",
        "signup_url": "https://querit.com",
        "capabilities": ["search", "multilingual"],
        "recommended": False,
    },
    {
        "provider": "perplexity",
        "env": "PERPLEXITY_API_KEY",
        "display_name": "Perplexity",
        "description": "Direct answer-style search when configured directly.",
        "free_tier": "API key required",
        "signup_url": "https://www.perplexity.ai/settings/api",
        "capabilities": ["search", "answer"],
        "recommended": False,
    },
    {
        "provider": "kilo-perplexity",
        "env": "KILOCODE_API_KEY",
        "display_name": "Kilo Code Perplexity bridge",
        "description": "Perplexity-compatible access through Kilo Code when configured.",
        "free_tier": "Depends on Kilo account",
        "signup_url": "https://kilo.ai",
        "capabilities": ["search", "answer"],
        "recommended": False,
    },
    {
        "provider": "you",
        "env": "YOU_API_KEY",
        "display_name": "You.com",
        "description": "LLM-ready real-time snippets and extraction when available.",
        "free_tier": "Limited/API key required",
        "signup_url": "https://api.you.com",
        "capabilities": ["search", "extract"],
        "recommended": False,
    },
    {
        "provider": "searxng",
        "env": "SEARXNG_INSTANCE_URL",
        "display_name": "SearXNG",
        "description": "Self-hosted/privacy-preserving metasearch instance URL.",
        "free_tier": "Free if self-hosted",
        "signup_url": "https://docs.searxng.org/admin/installation.html",
        "capabilities": ["search", "self-hosted"],
        "recommended": False,
    },
]


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
        val = val.strip()
        if val and not val.startswith("***") and key not in os.environ:
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
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _provider_config_status(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Describe configured providers by capability tier.

    No single provider key is globally required. Search, extraction, and answer
    quality are capability-based: one search provider enables search/snippet
    answers; one extraction-capable provider enables URL extraction and fuller
    cited answers. Linkup is preferred for answer extraction when available, but
    it is not a hard requirement.
    """
    env = env if env is not None else os.environ
    providers: Dict[str, Dict[str, Any]] = {}
    configured_count = 0
    configured_search_count = 0
    configured_extract_count = 0
    for item in _PROVIDER_CATALOG:
        key = item["env"]
        configured = bool((env.get(key) or "").strip())
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
        "answer_configured": configured_search_count > 0,
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
            f"extraction={'yes' if status['extract_configured'] else 'no'}, "
            f"answer={'yes' if status['answer_configured'] else 'no'}"
        )
        if status["search_configured"] and not status["extract_configured"]:
            lines.append(
                "Tip: add LINKUP_API_KEY (preferred) or another extraction key "
                "for fuller web_answer_plus citations and web_extract_plus."
            )
        return "\n".join(lines)

    lines = [
        "web-search-plus is installed but no provider keys are configured.",
        "No single key is mandatory, but at least one search-capable provider is needed for web_search_plus/web_answer_plus.",
        "Add LINKUP_API_KEY or another extraction-capable provider for web_extract_plus and fuller cited answers.",
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
            _capability_badge(status["answer_configured"], "answers", color=color),
        ]),
        f"│ Providers: {status['configured_count']}/{status['total']} configured",
    ]
    if configured:
        lines.append("│ Active: " + ", ".join(configured))
    else:
        lines.append("│ Active: none yet — add one search provider to unlock the tools")
    if status["search_configured"] and not status["extract_configured"]:
        lines.append("│ Tip: add Linkup for cleaner citations and web_extract_plus.")
    elif not status["search_configured"]:
        lines.append("│ Starter: Tavily + Linkup + Brave is the best first setup.")
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
        names = {"tavily", "linkup", "brave"}
    elif preset == "lean":
        names = {"tavily", "linkup"}
    elif preset == "search":
        names = {"tavily", "brave", "serper"}
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
        "Default setup prompts every provider. Presets: starter=Tavily+Linkup+Brave, lean=Tavily+Linkup, "
        "search=Tavily+Brave+Serper, extract=Linkup+Firecrawl+Tavily."
    )
    subs = parser.add_subparsers(dest="web_search_plus_command")
    status = subs.add_parser("status", help="Show a setup dashboard without printing secrets")
    status.add_argument("--plain", action="store_true", help="Print compact legacy text instead of the dashboard")
    setup = subs.add_parser("setup", help="Run the provider-key setup wizard")
    setup.add_argument("providers", nargs="*", help="Provider names to configure (overrides --preset)")
    setup.add_argument("--preset", default="all", help="starter, lean, search, extract, or all (default: all)")
    setup.add_argument("--open", action="store_true", help="Open signup URLs in a browser before prompting")
    setup.add_argument("--env-path", help="Override Hermes .env path")
    setup.add_argument("--show-values", action="store_true", help="Use visible input instead of hidden secret prompts")
    setup.add_argument("--dry-run", action="store_true", help="Show the setup plan without writing .env")
    list_cmd = subs.add_parser("list", help="List supported providers, capabilities, and signup URLs")
    list_cmd.add_argument("--json", action="store_true", help="Print provider catalog as JSON")
    parser.set_defaults(func=_web_search_plus_cli_command)


def _web_search_plus_cli_command(args: Any) -> None:
    command = getattr(args, "web_search_plus_command", None) or "status"
    if command == "list":
        print(_render_provider_catalog(json_output=getattr(args, "json", False)))
        return

    if command == "status":
        print(_render_setup_guidance(fancy=not getattr(args, "plain", False)))
        return

    if command == "setup":
        selected = set(getattr(args, "providers", None) or [])
        catalog = [item for item in _PROVIDER_CATALOG if item["provider"] in selected] if selected else _providers_for_preset(getattr(args, "preset", "all"))
        if not catalog:
            raise SystemExit("No matching providers. Run `python ~/.hermes/plugins/web-search-plus/setup.py list`.")

        env_path = Path(getattr(args, "env_path", None) or _get_hermes_env_path())
        print(_render_status_dashboard(_provider_config_status(_read_env_file(env_path))))
        print("\nSetup plan:")
        for item in catalog:
            rec = " recommended" if item.get("recommended") else ""
            caps = ", ".join(item.get("capabilities", []))
            print(f"  • {item['display_name']} ({item['provider']}) — {item['env']} — {caps}{rec}")
            print(f"    {item['signup_url']}")
        print(f"\nTarget env file: {env_path}")
        if getattr(args, "dry_run", False):
            print("Dry run only; no keys written.")
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
                # Non-interactive stdin (CI, scripted smoke tests) should behave like
                # pressing Enter for the remaining prompts, not crash the setup helper.
                value = ""
            if value:
                values[item["env"]] = value
        if not values:
            print("No keys entered; nothing changed.")
            return
        result = _upsert_env_values(env_path, values)
        changed = sorted(result["updated"] + result["added"])
        print(f"\n✓ Configured {len(changed)} provider key(s) in {env_path}: " + ", ".join(changed))
        print("✓ Secrets were not printed.")
        print("Next: restart Hermes or run /reset so tools re-register with the new credentials.")
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


def _detect_answer_freshness(query: str, requested: str = "auto") -> Dict[str, str]:
    """Resolve answer freshness from an explicit value or lightweight query signals."""
    requested = requested or "auto"
    if requested != "auto":
        return {"requested": requested, "applied": "none" if requested == "none" else requested, "reason": "explicit freshness requested"}

    q = query.lower()
    day_terms = ["today", "right now", "breaking", "heute", "gerade", "aktuell", "now"]
    week_terms = ["latest", "this week", "past week", "recent", "news", "updates", "neueste", "diese woche", "nachrichten"]
    month_terms = ["this month", "past month", "dieser monat", "letzter monat"]
    if any(term in q for term in day_terms):
        return {"requested": requested, "applied": "day", "reason": "query looked time-sensitive"}
    if any(term in q for term in week_terms) or re.search(r"\b20[2-9][0-9]\b", q):
        return {"requested": requested, "applied": "week", "reason": "query looked time-sensitive"}
    if any(term in q for term in month_terms):
        return {"requested": requested, "applied": "month", "reason": "query looked time-sensitive"}
    return {"requested": requested, "applied": "none", "reason": "no freshness signals detected"}


def _detect_answer_locale(query: str, language: str = "auto", country: str = "auto") -> Dict[str, str]:
    """Small locale detector for web_answer_plus. Deliberately conservative."""
    q = query.lower()
    detected_language = None if language == "auto" else language
    detected_country = None if country == "auto" else country.upper()
    confidence = "explicit" if detected_language else "low"

    if not detected_language:
        language_signals = [
            ("fr", ["meilleur", "meilleurs", "pas cher", "comparaison", "avis", "france"]),
            ("es", ["precio", "barato", "comparación", "alternativas", "españa", "méxico"]),
            ("it", ["prezzo", "migliori", "confronto", "italia"]),
            ("pt", ["preço", "melhores", "comparação", "brasil", "portugal"]),
            ("de", ["preis", "günstig", "vergleich", "österreich", "deutschland", "schweiz"]),
        ]
        for code, terms in language_signals:
            if any(term in q for term in terms):
                detected_language = code
                confidence = "medium"
                break
    if not detected_language:
        if re.search(r"[\u3040-\u30ff]", query):
            detected_language, confidence = "ja", "medium"
        elif re.search(r"[\u4e00-\u9fff]", query):
            detected_language, confidence = "zh", "medium"
        elif re.search(r"[\u0600-\u06ff]", query):
            detected_language, confidence = "ar", "medium"
        elif re.search(r"[\u0400-\u04ff]", query):
            detected_language, confidence = "ru", "medium"
        else:
            detected_language, confidence = "en", "low"

    if not detected_country:
        country_signals = {
            "AT": ["österreich", "austria", "graz", "wien", "vienna"],
            "DE": ["deutschland", "germany", "berlin", "münchen"],
            "FR": ["france", "paris"],
            "ES": ["españa", "spain", "madrid"],
            "MX": ["méxico", "mexico"],
            "IT": ["italia", "italy"],
            "BR": ["brasil", "brazil"],
            "JP": ["日本", "japan"],
        }
        for code, terms in country_signals.items():
            if any(term in q for term in terms):
                detected_country = code
                break
    if not detected_country:
        detected_country = {"de": "DE", "fr": "FR", "es": "ES", "it": "IT", "pt": "BR", "ja": "JP"}.get(detected_language, "US")

    return {"language": detected_language, "country": detected_country, "language_confidence": confidence}


def _source_type_for_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if any(part in host for part in ("docs.", "developer.", "github.com", "readthedocs", "developer.mozilla")):
        return "docs"
    if any(part in host for part in ("reddit.com", "forum", "community", "discourse")):
        return "forum"
    if any(part in host for part in ("news", "reuters", "apnews", "bbc", "orf.at", "nytimes")):
        return "news"
    if any(part in host for part in ("shop", "amazon", "geizhals", "idealo")):
        return "shopping"
    return "web"


def _normalize_answer_sources(results: List[Dict[str, Any]], provider: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Normalize heterogeneous provider results into citation-ready source records."""
    sources: List[Dict[str, Any]] = []
    seen = set()
    for item in results:
        url = item.get("url") or item.get("link") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        title = item.get("title") or url
        domain = urlparse(url).netloc.lower()
        published = item.get("date") or item.get("published_date") or item.get("publishedDate")
        date_label = f", {published}" if published else ""
        sources.append({
            "title": title,
            "domain": domain,
            "url": url,
            "published_date": published,
            "source_type": _source_type_for_url(url),
            "provider": item.get("provider", provider),
            "extracted_status": item.get("extracted_status", "not_requested"),
            "used_in_answer": True,
            "citation": f"[{title} ({domain}{date_label})]({url})",
            "snippet": item.get("snippet") or item.get("description") or item.get("content") or "",
        })
        if limit and len(sources) >= limit:
            break
    return sources


def _clean_answer_evidence(text: str, max_chars: int = 380) -> str:
    """Turn raw extracted page text into a short readable evidence sentence."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"\[[^\]]*\]\(\s*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]\((?:#|javascript:|data:)[^)]*\)", " ", text)
    text = re.sub(r"data:image/[^\s)]+", " ", text)
    noisy_phrases = [
        "Skip to content",
        "Skip to main content",
        "You signed in with another tab or window",
        "Reload to refresh your session",
        "You signed out in another tab or window",
        "You switched accounts on another tab or window",
    ]
    for phrase in noisy_phrases:
        text = text.replace(phrase, " ")
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip(" -—|\n\t")
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].strip()
    return cut + "…"


def _build_source_backed_answer(
    query: str,
    sources: List[Dict[str, Any]],
    extract_data: Dict[str, Any],
    extract_count: int,
    max_chars: int = 6000,
) -> str:
    """Build a user-readable answer-shaped brief without pretending to do LLM synthesis."""
    if not sources:
        return f"No usable sources for: {query}"

    extracted_results = extract_data.get("results", []) or []
    usable_extracted = [r for r in extracted_results if not r.get("error") and (r.get("content") or r.get("raw_content"))]
    lines = [f"Source-backed brief for: {query}", "", f"Based on {len(usable_extracted)} of {min(len(sources), extract_count)} selected source(s) with {len(sources)} citation-ready source(s) found.", ""]
    for idx, src in enumerate(sources[:max(1, extract_count)], 1):
        extracted = next((r for r in extracted_results if r.get("url") == src["url"]), {})
        raw_text = extracted.get("content") or extracted.get("raw_content") or src.get("snippet") or ""
        evidence = _clean_answer_evidence(raw_text)
        if not evidence:
            evidence = _clean_answer_evidence(src.get("snippet", "")) or "No readable snippet available."
        lines.append(f"- [{idx}] {src['title']} — {evidence}")

    if len(sources) > extract_count:
        lines.append("")
        lines.append(f"Also found {len(sources) - extract_count} additional citation-ready source(s) below.")
    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit("\n", 1)[0].strip()
    return cut + "\n…"


class _ExtractionTimeout(Exception):
    pass


def _run_extract_with_timeout(timeout_seconds: int, **kwargs) -> Dict[str, Any]:
    """Run extraction with a best-effort local timeout for answer-mode UX."""
    kwargs.setdefault("subprocess_timeout", max(1, int(timeout_seconds)))
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        return _run_extract(**kwargs)

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _handle_timeout(signum, frame):  # noqa: ARG001
        raise _ExtractionTimeout(f"Extract timed out after {timeout_seconds}s")

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(timeout_seconds)
    try:
        return _run_extract(**kwargs)
    except _ExtractionTimeout as exc:
        return {"provider": kwargs.get("provider"), "error": str(exc), "results": []}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _preferred_answer_extract_provider(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Choose an extraction provider for web_answer_plus.

    Linkup is preferred because it is cheap and citation-friendly. If Linkup is
    not configured, fall back to the normal extraction provider chain when any
    extraction-capable key is present. If no extraction provider is configured,
    answer mode degrades honestly to search snippets.
    """
    env = env if env is not None else os.environ
    if (env.get("LINKUP_API_KEY") or "").strip():
        return "linkup"
    if any((env.get(k) or "").strip() for k in _EXTRACT_PROVIDER_ENV_KEYS):
        return "auto"
    return None


def _compose_answer_payload(
    query: str,
    mode: str = "quick",
    sources: Optional[int] = None,
    freshness: str = "auto",
    language: str = "auto",
    country: str = "auto",
    include_counterpoints: Optional[bool] = None,
    max_extracts: Optional[int] = None,
) -> Dict[str, Any]:
    """Search, extract a few top sources, and return a citation-ready answer payload."""
    mode = mode if mode in {"quick", "deep"} else "quick"
    source_count = sources or (6 if mode == "deep" else 3)
    requested_extract_count = max_extracts if max_extracts is not None else 2
    extract_cap = 5
    extract_count = min(requested_extract_count, extract_cap, source_count)
    counterpoints = bool(include_counterpoints)
    warnings: List[str] = []
    global_budget_seconds = 35.0 if mode == "deep" else 20.0
    started_at = time.monotonic()

    def remaining_budget_seconds() -> float:
        return max(0.0, global_budget_seconds - (time.monotonic() - started_at))

    if requested_extract_count > extract_cap:
        warnings.append(f"max_extracts capped at {extract_cap} to protect provider budget.")
    freshness_info = _detect_answer_freshness(query, freshness)
    locale = _detect_answer_locale(query, language, country)

    search_data = _run_search(
        query=query,
        provider="auto",
        count=source_count,
        time_range=None if freshness_info["applied"] == "none" else freshness_info["applied"],
        mode="research" if mode == "deep" else "normal",
        quality_report=True,
        research_time_budget=min(55.0 if mode == "deep" else 20.0, remaining_budget_seconds()),
        language=locale["language"],
        country=locale["country"],
        subprocess_timeout=max(1, int(remaining_budget_seconds())),
    )
    results = search_data.get("results", [])[:source_count]
    normalized = _normalize_answer_sources(results, provider=search_data.get("provider"), limit=source_count)

    if search_data.get("error"):
        warnings.append(f"Search issue: {search_data['error']}")

    urls_to_extract = [s["url"] for s in normalized[:extract_count]]
    extract_data: Dict[str, Any] = {"results": []}
    extract_provider = _preferred_answer_extract_provider()
    if urls_to_extract and not extract_provider:
        warnings.append(
            "Extraction skipped: no extraction-capable provider configured. "
            "Add LINKUP_API_KEY (preferred), FIRECRAWL_API_KEY, TAVILY_API_KEY, EXA_API_KEY, or YOU_API_KEY for fuller cited answers."
        )
        extract_data = {"provider": None, "results": [], "error": "no extraction-capable provider configured"}
    elif urls_to_extract:
        remaining = remaining_budget_seconds()
        if remaining < 1.0:
            warnings.append("Extraction skipped: answer wall-clock budget exhausted after search.")
            extract_data = {"provider": extract_provider, "results": [], "error": "answer wall-clock budget exhausted"}
        else:
            extract_data = _run_extract_with_timeout(
                timeout_seconds=max(1, int(min(12 if mode == "quick" else 25, remaining))),
                urls=urls_to_extract,
                provider=extract_provider,
                output_format="markdown",
            )
        extracted_by_url = {r.get("url"): r for r in extract_data.get("results", [])}
        extract_error = extract_data.get("error")
        if extract_error:
            warnings.append(f"Extraction failed: {extract_error}")
        for src in normalized:
            if src["url"] not in urls_to_extract:
                continue
            if extract_error:
                src["extracted_status"] = "failed"
                continue
            if src["url"] in extracted_by_url:
                extracted = extracted_by_url[src["url"]]
                if extracted.get("error"):
                    src["extracted_status"] = "failed"
                    warnings.append(f"Extraction failed for {src['domain']}: {extracted['error']}")
                else:
                    src["extracted_status"] = "full" if (extracted.get("content") or extracted.get("raw_content")) else "partial"
            else:
                src["extracted_status"] = "failed"

    answer = _build_source_backed_answer(query, normalized, extract_data, extract_count)

    if len(normalized) < source_count:
        warnings.append(f"Only {len(normalized)} citation-ready sources found for requested {source_count}.")
    extracted_count = sum(1 for s in normalized if s["extracted_status"] in {"full", "partial"})
    if urls_to_extract and extracted_count < len(urls_to_extract):
        warnings.append(f"Only {extracted_count} of {len(urls_to_extract)} selected sources had extractable content.")
    if counterpoints:
        warnings.append("Counterpoint search is not implemented in this MVP yet.")

    confidence = "high" if len(normalized) >= 4 and extracted_count >= 3 else "medium" if normalized else "low"
    confidence_reason = {
        "sources": len(normalized),
        "requested_sources": source_count,
        "extracts_succeeded": extracted_count,
        "extracts_requested": len(urls_to_extract),
        "freshness_applied": freshness_info["applied"],
    }
    actual_extract_provider = (extract_data.get("provider") or extract_provider) if urls_to_extract else None
    cost_estimate = {
        "extract_provider": actual_extract_provider,
        "extracts_requested": len(urls_to_extract),
        # Linkup's public pricing is roughly usage/credit based and cheap enough
        # to show a tiny estimate. Other providers use different credit models, so
        # pretending every extractor costs the same would be worse than omission.
        "approx_eur": round(len(urls_to_extract) * 0.001, 4) if actual_extract_provider == "linkup" else None,
    }
    return {
        "query": query,
        "mode": mode,
        "answer": answer,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "freshness": freshness_info,
        "locale": locale,
        "sources": normalized,
        "warnings": warnings,
        "cost_estimate": cost_estimate,
        "budget": {"wall_clock_seconds": global_budget_seconds, "elapsed_seconds": round(time.monotonic() - started_at, 3)},
        "search": {"provider": search_data.get("provider"), "routing": search_data.get("routing", {})},
        "extraction": {"provider": extract_data.get("provider"), "requested_urls": urls_to_extract},
    }


def _format_answer_payload(payload: Dict[str, Any], output: str = "answer") -> str:
    if output == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if output == "sources":
        return "\n".join(f"- {s['citation']} — {s['source_type']}" for s in payload.get("sources", []))
    answer = payload.get("answer", "")
    if output == "brief":
        answer = answer[:900]
    lines = ["**Answer**", answer, "", "**Sources**"]
    lines.extend(f"- {s['citation']} — {s['source_type']}" for s in payload.get("sources", []))
    lines.extend([
        "",
        f"**Confidence:** {payload.get('confidence', 'unknown')}",
        f"**Freshness:** {payload.get('freshness', {}).get('applied', 'none')} ({payload.get('freshness', {}).get('reason', '')})",
    ])
    if payload.get("warnings"):
        lines.append("**Warnings:** " + "; ".join(payload["warnings"]))
    return "\n".join(lines).strip()


def register(ctx: Any) -> None:
    """Register web-search-plus tools with Hermes plugin system."""

    schema = {
        "name": "web_search_plus",
        "description": (
            "Multi-provider web search with intelligent auto-routing. "
            "Automatically selects the best provider based on query intent: "
            "Serper for shopping/news/facts, Tavily for research/analysis, "
            "Exa for semantic discovery, Querit for multilingual/real-time, "
            "Brave for general web search, "
            "Linkup for source-backed grounding/citations, "
            "Firecrawl for web search plus optional scrape-ready results, "
            "Perplexity for direct answers, You.com for real-time snippets, "
            "and SearXNG for privacy-focused/self-hosted search. "
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
                    "enum": ["auto", "serper", "brave", "tavily", "exa", "querit", "linkup", "firecrawl", "perplexity", "you", "searxng"],
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
            "Multi-provider URL content extraction. Use Firecrawl for robust scraping, "
            "Linkup for clean markdown fetches with monthly free credits, Tavily for extraction, Exa Contents, or You.com Contents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to extract"},
                "provider": {"type": "string", "enum": ["auto", "firecrawl", "linkup", "tavily", "exa", "you"], "default": "auto"},
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

    answer_schema = {
        "name": "web_answer_plus",
        "description": (
            "User-facing cited web answer tool. Searches, deduplicates, extracts top sources, "
            "and returns a concise answer with citation-ready sources. Keep the interface simple: "
            "choose quick or deep, source count, freshness, output shape, and optional locale."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question or research query to answer from the web."},
                "mode": {"type": "string", "enum": ["quick", "deep"], "default": "quick", "description": "quick = fast answer from a few sources; deep = broader research-mode answer."},
                "sources": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10, "description": "Number of citation-ready sources to return. Default quick=3; deep usually uses 6."},
                "freshness": {"type": "string", "enum": ["auto", "none", "day", "week", "month", "year"], "default": "auto", "description": "Recency filter. auto detects current/news/latest/date-sensitive queries."},
                "output": {"type": "string", "enum": ["answer", "brief", "sources", "json"], "default": "answer", "description": "Return markdown answer, short brief, sources-only list, or structured JSON."},
                "language": {"type": "string", "default": "auto", "description": "BCP-47-ish language code such as de/en/es/fr, or auto."},
                "country": {"type": "string", "default": "auto", "description": "Country/region code such as AT/DE/US/FR, or auto."},
                "include_counterpoints": {"type": "boolean", "default": False, "description": "Request counterpoint coverage. MVP records this as a warning until implemented."},
                "max_extracts": {"type": "integer", "minimum": 0, "maximum": 5, "description": "Advanced: number of top URLs to extract. Defaults to 2 and hard-caps at 5 for cost safety."},
            },
            "required": ["query"],
        },
    }

    def answer_handler(args_or_query, mode: str = "quick", sources: Optional[int] = None,
                       freshness: str = "auto", output: str = "answer", language: str = "auto",
                       country: str = "auto", include_counterpoints: Optional[bool] = None,
                       max_extracts: Optional[int] = None, **kwargs) -> str:
        if isinstance(args_or_query, dict):
            query = args_or_query.get("query", "")
            mode = args_or_query.get("mode", mode)
            sources = args_or_query.get("sources", sources)
            freshness = args_or_query.get("freshness", freshness)
            output = args_or_query.get("output", output)
            language = args_or_query.get("language", language)
            country = args_or_query.get("country", country)
            include_counterpoints = args_or_query.get("include_counterpoints", include_counterpoints)
            max_extracts = args_or_query.get("max_extracts", max_extracts)
        else:
            query = args_or_query
        payload = _compose_answer_payload(
            query=query,
            mode=mode,
            sources=sources,
            freshness=freshness,
            language=language,
            country=country,
            include_counterpoints=include_counterpoints,
            max_extracts=max_extracts,
        )
        return _format_answer_payload(payload, output=output)

    ctx.register_tool(
        name="web_answer_plus",
        toolset=_TOOLSET_NAME,
        schema=answer_schema,
        handler=answer_handler,
        check_fn=check_fn,
        requires_env=[],
        description="Cited web answers from search plus extraction",
        emoji="🧭",
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
