#!/usr/bin/env python3
"""
Web Search Plus — Unified Multi-Provider Search and Extraction with Intelligent Auto-Routing
Version: 5.0.0
Supports search providers: You.com, Serper, Exa, Firecrawl, Tavily, Linkup,
Brave Search, SerpBase, Querit, Parallel, SearXNG, Keenable.
Supports extract providers: Firecrawl, Linkup, Parallel, Tavily, Exa, You.com, Keenable, Serper.

Automatic routing (provider "auto") labels the query with an intent and picks
the first provider from a measured table: Exa for academic and documentation
queries, Serper for shopping, Brave otherwise; the fallback chain follows
auto_routing.provider_priority. See docs/ROUTING.md.

Usage:
    python3 search.py --query "..."                    # Auto-route based on query
    python3 search.py --provider [you|serper|exa|firecrawl|tavily|linkup|brave|serpbase|querit|searxng|auto] --query "..." [options]

Examples:
    python3 search.py -q "arXiv paper LLM scaling laws"            # → Exa (academic)
    python3 search.py -q "iPhone 16 Pro Max price"                 # → Serper (shopping)
    python3 search.py -q "latest OpenSSH CVE mitigation"           # → Brave (security)
"""

from __future__ import annotations


import argparse
import copy
import json
import os
import queue
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import List, Dict, Any, Optional, Tuple
from .daemon_tasks import DaemonTask
from .http_client import ProviderRequestError, request_timeout_cap
from .cache import (
    CACHE_DIR,
    DEFAULT_CACHE_TTL,
    cache_clear,
    cache_get,
    cache_put,
    cache_stats,
    effective_search_cache_ttl,
    routing_class_of,
)
from .budget_preflight_v3 import daily_preflight_budget as _daily_preflight_budget

from .config import (
    add_provider_setup_guidance,
    ProviderConfigError,
    SELF_HOSTED_SEARCH_PROVIDER_IDS,
    apply_profile_effects,
    get_api_key,
    is_self_hosted_profile,
    keyless_public_allowed,
    load_config,
    provider_configured,
    validate_api_key,
)
from .provider_health import (
    execute_provider_with_retry,
    mark_provider_failure,
    provider_in_cooldown,
    reset_provider_health,
)
from .provider_stats import latency_quantile, record_provider_outcome
from .quality import (
    build_quality_report,
    deduplicate_results_across_providers,
    extract_domain_constraints,
    filter_spam_results,
    rerank_domain_diversity,
    rerank_results_for_intent,
    select_research_providers,
)
from .diversity_v3 import DEFAULT_NEAR_DUPLICATE_THRESHOLD
from .provider_adapter_protocol import validate_adapter_result
from .provider_dispatch import SEARCH_DISPATCH
from .provider_registry import (
    EXTRACT_PROVIDER_IDS,
    PROVIDER_STARTUP_DIAGNOSTICS,
    PROVIDER_SPECS,
    SEARCH_PROVIDER_IDS,
    doctor_catalog,
)
from .request_gate_v3 import validate_provider_mode
from .search_locale import (
    AUTO_LANGUAGE,
    apply_auto_language,
    is_auto_language,
    provider_supports_locale,
    resolve_locale,
)
from .env_loader import load_env_files
from .research import run_research_mode
from .attempt_engine_v3 import AttemptEngine, provider_attempt_context, request_deadline
from .cache_v3 import ResponseCacheV3
from .compat_v3 import legacy_request_to_v3, v3_response_to_legacy_search
from .contract_v3 import Capability, ErrorClass, RequestV3, ResponseV3, SkipReason
from .orchestrator_v3 import (
    CapabilityAdapter,
    CapabilityExecution,
    ProviderPlan,
    execute_v3_request,
)
from .runtime_v3 import response_from_legacy
from .state_store_v3 import SQLiteStateStore
from .urls import domain_filter_tokens, domain_filters
from . import providers as _providers
from . import extract as _extract
from .routing import (
    ROUTING_POLICY,
    _provider_auto_allowed,
    auto_route_provider,
    explain_routing,
)
from .extract import extract_plus


def _load_env_file():
    """Load plugin-local, legacy parent, and Hermes profile .env files."""
    load_env_files(__file__)


EXTRACT_PROVIDER_PRIORITY = _extract.EXTRACT_PROVIDER_PRIORITY
resolve_extract_provider_priority = _extract.resolve_extract_provider_priority


PROVIDER_DOCTOR_CATALOG = doctor_catalog()


def _doctor_error(error_type: str, message: str) -> Dict[str, str]:
    """Return a deliberately sanitized doctor error.

    Doctor output is often pasted into issues/support chats. Keep it useful without
    echoing private URLs, filesystem paths, tokens, or corrupt raw cache values.
    """
    safe_messages = {
        "config": "Provider configuration is invalid; inspect local config/env for this provider.",
        "cooldown": "Provider health cache has an invalid cooldown entry; reset provider health cache if it persists.",
    }
    return {"type": error_type, "message": safe_messages.get(error_type, "Provider diagnostic failed.")}


def _doctor_provider_has_error_type(provider_report: Dict[str, Any], error_type: str) -> bool:
    error = provider_report.get("error")
    if isinstance(error, dict):
        return error.get("type") == error_type
    if isinstance(error, list):
        return any(isinstance(item, dict) and item.get("type") == error_type for item in error)
    return False


def _build_doctor_report(config: Dict[str, Any], *, live: bool = False) -> Dict[str, Any]:
    auto_config = config.get("auto_routing", {})
    disabled = set(auto_config.get("disabled_providers", []) or [])
    providers = []
    for provider, spec in PROVIDER_DOCTOR_CATALOG.items():
        errors = []
        try:
            in_cooldown, remaining = provider_in_cooldown(provider)
            cooldown = {"active": bool(in_cooldown), "remaining_seconds": int(remaining)}
        except (TypeError, ValueError):
            cooldown = {"active": False, "remaining_seconds": 0}
            errors.append(_doctor_error("cooldown", "invalid provider health cache value"))

        try:
            key_present = bool(get_api_key(provider, config))
        except (ProviderConfigError, ValueError):
            key_present = False
            errors.append(_doctor_error("config", "invalid provider configuration"))

        spec_obj = PROVIDER_SPECS.get(provider)
        keyless = bool(spec_obj and spec_obj.keyless)
        keyless_enabled = keyless and keyless_public_allowed(provider, config)

        provider_report = {
            "provider": provider,
            "env_var": spec["env_var"],
            "search_capable": spec["search_capable"],
            "extract_capable": spec["extract_capable"],
            "key_present": key_present,
            "keyless": keyless,
            "keyless_public_enabled": keyless_enabled,
            "auto_allowed": _provider_auto_allowed(provider, auto_config),
            "disabled": provider in disabled,
            "cooldown": cooldown,
        }
        if errors:
            provider_report["error"] = errors[0] if len(errors) == 1 else errors
        providers.append(provider_report)

    usable = [p for p in providers if (p["key_present"] or p["keyless_public_enabled"]) and not p["disabled"]]
    config_errors = [p for p in providers if _doctor_provider_has_error_type(p, "config")]
    return {
        "ok": bool(usable) and not config_errors,
        "mode": "live" if live else "offline",
        "config": {
            "auto_routing_enabled": auto_config.get("enabled", True),
            "default_provider": config.get("default_provider"),
            "fallback_provider": auto_config.get("fallback_provider"),
            "disabled_providers": sorted(disabled),
        },
        "cache": {
            "dir": str(CACHE_DIR),
            "exists": CACHE_DIR.exists(),
            "writable": os.access(CACHE_DIR if CACHE_DIR.exists() else CACHE_DIR.parent, os.W_OK),
            "provider_health_file": str(CACHE_DIR / "provider_health.json"),
        },
        "providers": providers,
        "startup_diagnostics": [
            {"module": diagnostic.module, "code": diagnostic.code}
            for diagnostic in PROVIDER_STARTUP_DIAGNOSTICS
        ],
    }


def run_provider_bench(config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """Run the in-process provider bakeoff (see bench.py) and return its report.

    Uses provider implementations directly and never records provider health
    cooldowns or provider stats.
    """
    from . import bench

    return bench.run_bench(config, search_module=_providers, **kwargs)


def format_bench_text(report: Dict[str, Any]) -> str:
    from . import bench

    return bench.format_bench_text(report)


def _format_doctor_text(report: Dict[str, Any]) -> str:
    lines = ["Web Search Plus Doctor", f"Mode: {report['mode']}", f"OK: {report['ok']}", "", "Providers:"]
    for provider in report["providers"]:
        capabilities = []
        if provider["search_capable"]:
            capabilities.append("search")
        if provider["extract_capable"]:
            capabilities.append("extract")
        cooldown = provider["cooldown"]
        cooldown_text = f"cooldown {cooldown['remaining_seconds']}s" if cooldown["active"] else "no cooldown"
        keyless_text = ""
        if provider.get("keyless"):
            keyless_text = f"keyless={'on' if provider.get('keyless_public_enabled') else 'off'} "
        lines.append(
            f"- {provider['provider']}: env={provider['env_var']} "
            f"key={'yes' if provider['key_present'] else 'no'} "
            f"{keyless_text}"
            f"capabilities={','.join(capabilities)} "
            f"auto_allowed={'yes' if provider['auto_allowed'] else 'no'} "
            f"disabled={'yes' if provider['disabled'] else 'no'} "
            f"{cooldown_text}"
        )
    lines.extend(["", f"Cache: {report['cache']['dir']} (writable={report['cache']['writable']})"])
    return "\n".join(lines)


def build_parser_for_tests() -> argparse.ArgumentParser:
    """Return a minimal parser exposing registry-backed provider choices for drift tests."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--provider",
        "-p",
        choices=[*SEARCH_PROVIDER_IDS, "auto"],
        help="Search provider (auto=intelligent routing)",
    )
    return parser


class _StoreQueryText(argparse.Action):
    """Preserve literal -- on Python 3.10 as well as newer argparse versions."""

    def __call__(self, parser, namespace, values, option_string=None):
        # Python 3.10 strips '--' even from a single attached option value.
        # With nargs=None this empty list cannot represent omitted input.
        setattr(namespace, self.dest, "--" if values == [] else values)


def build_parser(config: Dict[str, Any]) -> argparse.ArgumentParser:
    """Build the search.py CLI argument parser.

    Shared by the CLI ``main()`` entry and the in-process ``run_search_request``
    helper so the Hermes plugin can route argv through the exact same parsing and
    defaults without spawning a subprocess.
    """
    from . import extract_bench_v3 as _extract_bench  # CLI-only; not loaded by searches

    parser = argparse.ArgumentParser(
        description="Web Search Plus — Intelligent multi-provider search with smart auto-routing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Automatic routing (provider "auto"):
  The query gets one of eight intents; the intent picks the first provider.

  academic, docs   → Exa      papers, studies, DOIs; API docs, error messages, code
  shopping         → Serper   buy, price, deals, "best ... under 300 euros"
  everything else  → Brave    community, general, local, news, security

  A missing provider is skipped (Brave, Serper, Exa, Tavily, then
  auto_routing.provider_priority). Fallback follows provider_priority.

Examples:
  python3 search.py -q "arXiv paper LLM scaling laws"            # → Exa (academic)
  python3 search.py -q "python asyncio TaskGroup documentation"  # → Exa (docs)
  python3 search.py -q "iPhone 16 Pro Max price"                 # → Serper (shopping)
  python3 search.py -q "reddit best budget mechanical keyboard"  # → Brave (community)
  python3 search.py --explain-routing -q "your query"            # Show the decision

Full docs: See README.md and SKILL.md
        """,
    )
    
    # Command arguments
    parser.add_argument(
        "command",
        nargs="?",
        choices=["doctor", "bench", "extract-bench", "state-migrate"],
        help="Run doctor, benchmark, or reversible state-migration maintenance",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON for maintenance commands")
    parser.add_argument("--live", action="store_true", help="Allow doctor to run live provider smokes (reserved; offline by default)")
    parser.add_argument(
        "--bench",
        action="store_true",
        help="Benchmark configured search providers against a fixed live query suite and recommend an auto_routing.provider_priority (alias for the 'bench' command; spends real provider quota)",
    )
    parser.add_argument(
        "--bench-providers",
        nargs="+",
        choices=list(EXTRACT_PROVIDER_IDS),
        help="Limit extract-bench to these direct extraction providers",
    )
    parser.add_argument(
        "--bench-timeout-budget",
        type=float,
        default=_extract_bench.DEFAULT_TIMEOUT_BUDGET_SECONDS,
        help="Best-effort whole-run wall-clock budget for extract-bench",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not persist the completed extract-bench summary for the Operator Console",
    )
    migration_action = parser.add_mutually_exclusive_group()
    migration_action.add_argument(
        "--apply",
        action="store_true",
        help="Apply state-migrate after creating a verified backup (dry-run is default)",
    )
    migration_action.add_argument(
        "--rollback",
        metavar="BACKUP_ID",
        help="Rollback state-migrate using a verified backup ID",
    )
    parser.add_argument(
        "--migration-backup-root",
        type=Path,
        help="Override the default v3/migration-backups directory",
    )

    # Common arguments
    parser.add_argument(
        "--provider", "-p", 
        choices=[*SEARCH_PROVIDER_IDS, "auto"],
        help="Search provider (auto=intelligent routing)"
    )
    parser.add_argument(
        "--query", "-q", 
        action=_StoreQueryText, help="Search query"
    )
    parser.add_argument(
        "--extract-urls",
        nargs="*",
        help="Extract content from one or more URLs instead of running a search"
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        default="markdown",
        choices=["markdown", "html"],
        help="Extraction output format"
    )
    parser.add_argument("--extract-images", action="store_true", help="Extract image metadata when supported")
    parser.add_argument("--include-raw-html", action="store_true", help="Include raw HTML when supported")
    parser.add_argument("--render-js", action="store_true", help="Render JavaScript before extraction when supported")
    parser.add_argument("--spans", action="store_true", help="Select deterministic semantic spans from extracted text")
    parser.add_argument("--spans-query", action=_StoreQueryText, help="Query used to rank extracted semantic spans")
    parser.add_argument(
        "--max-results", "-n", 
        type=int, 
        default=config.get("defaults", {}).get("max_results", 5),
        help="Maximum results (default: 5)"
    )
    parser.add_argument(
        "--images", 
        action="store_true",
        help="Include images (Serper/Tavily)"
    )
    
    # Auto-routing options
    parser.add_argument(
        "--auto", "-a",
        action="store_true",
        help="Use intelligent auto-routing (default when no provider specified)"
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Allow explicit --provider calls to fall back to other configured providers on failure. Auto-routing already uses fallback."
    )
    parser.add_argument(
        "--explain-routing",
        action="store_true",
        help="Show detailed routing analysis (debug mode)"
    )
    
    # Locale (providers with country/language request parameters). Left unset,
    # search_locale.resolve_locale applies: explicit provider config > query
    # location hint > defaults.locale > us/en fallback. Explicit flags win.
    serper_config = config.get("serper", {})
    parser.add_argument(
        "--country",
        default=None,
        help="ISO 3166-1 alpha-2 country override (e.g. at, fr); beats config defaults and query location hints"
    )
    parser.add_argument(
        "--language",
        default=None,
        help="ISO 639-1 language override (e.g. de), or 'auto' to detect it from the query; beats config defaults"
    )
    parser.add_argument(
        "--type", 
        dest="search_type", 
        default=serper_config.get("type", "search"),
        choices=["search", "news", "images", "videos", "places", "shopping"]
    )
    parser.add_argument(
        "--search-type",
        dest="search_type",
        type=str.lower,
        choices=list(_providers.SEARCH_TYPE_VALUES),
        # Shares dest with the legacy serper-only --type flag above; SUPPRESS
        # keeps --type's config-driven default when neither flag is passed.
        default=argparse.SUPPRESS,
        help=(
            "Unified result vertical (search or news; case-insensitive). Providers with a "
            "native news vertical (currently serper and tinyfish) serve it directly; all other providers "
            "run the normal search and result metadata reports search_type.applied=false"
        )
    )
    parser.add_argument(
        "--time-range",
        choices=["hour", "day", "week", "month", "year"]
    )
    parser.add_argument(
        "--freshness",
        type=str.lower,
        choices=list(_providers.FRESHNESS_VALUES),
        help=(
            "Unified recency filter (day, week, month, year; case-insensitive). "
            "Applied natively where the provider supports it (serper, brave, querit, firecrawl, "
            "keenable, you, searxng, exa, tavily, and tinyfish); otherwise the search runs "
            "unfiltered and result metadata reports freshness.applied=false"
        )
    )
    
    # Tavily-specific
    tavily_config = config.get("tavily", {})
    parser.add_argument(
        "--depth", 
        default=tavily_config.get("depth", "basic"), 
        choices=["basic", "advanced"]
    )
    parser.add_argument(
        "--topic", 
        default=tavily_config.get("topic", "general"), 
        choices=["general", "news"]
    )
    parser.add_argument("--raw-content", action="store_true")
    
    # Querit-specific
    querit_config = config.get("querit", {})
    parser.add_argument(
        "--querit-base-url",
        default=querit_config.get("base_url", "https://api.querit.ai"),
        help="Querit API base URL"
    )
    parser.add_argument(
        "--querit-base-path",
        default=querit_config.get("base_path", "/v1/search"),
        help="Querit API path"
    )

    # Linkup-specific
    linkup_config = config.get("linkup", {})
    parser.add_argument(
        "--linkup-depth",
        default=linkup_config.get("depth", "standard"),
        choices=["fast", "standard", "deep"],
        help="Linkup search depth: fast, standard, or deep"
    )
    parser.add_argument(
        "--linkup-output-type",
        default=linkup_config.get("output_type", "searchResults"),
        choices=["searchResults"],
        help="Linkup source-results output (fixed by the source-only charter)"
    )

    # Exa-specific
    exa_config = config.get("exa", {})
    parser.add_argument(
        "--exa-type",
        default=exa_config.get("type", "neural"),
        choices=["neural", "fast", "auto", "keyword", "instant"],
        help="Exa search type (for standard search, ignored when --exa-depth is set)"
    )
    parser.add_argument(
        "--exa-depth",
        default=exa_config.get("depth", "normal"),
        choices=["normal"],
        help="Exa source-result depth (fixed to normal by the source-only charter)"
    )
    parser.add_argument(
        "--exa-verbosity",
        default=exa_config.get("verbosity", "standard"),
        choices=["compact", "standard", "full"],
        help="Exa text verbosity for content extraction"
    )
    parser.add_argument(
        "--category",
        choices=[
            "company", "research paper", "news", "pdf", "github", 
            "tweet", "personal site", "linkedin profile"
        ]
    )
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--similar-url")

    # Firecrawl-specific
    firecrawl_config = config.get("firecrawl", {})
    parser.add_argument(
        "--firecrawl-scrape",
        action="store_true",
        help="Firecrawl: scrape result pages and include markdown as raw_content"
    )
    parser.add_argument(
        "--firecrawl-sources",
        nargs="+",
        default=firecrawl_config.get("sources", ["web"]),
        choices=["web", "news", "images"],
        help="Firecrawl result sources"
    )
    
    # You.com-specific
    you_config = config.get("you", {})
    parser.add_argument(
        "--you-safesearch",
        default=you_config.get("safesearch", "moderate"),
        choices=["off", "moderate", "strict"],
        help="You.com SafeSearch filter"
    )
    parser.add_argument(
        "--livecrawl",
        choices=["web", "news", "all"],
        help="You.com: fetch full page content"
    )
    parser.add_argument(
        "--no-news",
        action="store_true",
        help="You.com: exclude news results (included by default)"
    )
    
    # SearXNG-specific
    searxng_config = config.get("searxng", {})
    parser.add_argument(
        "--searxng-url",
        default=searxng_config.get("base_url") or searxng_config.get("instance_url"),
        help="SearXNG instance URL (e.g., https://searx.example.com)"
    )
    parser.add_argument(
        "--searxng-safesearch",
        type=int,
        default=searxng_config.get("safesearch", 0),
        choices=[0, 1, 2],
        help="SearXNG SafeSearch: 0=off, 1=moderate, 2=strict"
    )
    parser.add_argument(
        "--engines",
        nargs="+",
        default=searxng_config.get("engines"),
        help="SearXNG: specific engines to use (e.g., google bing duckduckgo)"
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        help="SearXNG: search categories (general, images, news, videos, etc.)"
    )
    
    # Domain filters
    parser.add_argument("--include-domains", nargs="+")
    parser.add_argument("--exclude-domains", nargs="+")
    
    # Output
    parser.add_argument("--compact", action="store_true")
    parser.add_argument(
        "--quality-report",
        action="store_true",
        help="Attach transparent routing/result diagnostics to the JSON output"
    )
    parser.add_argument(
        "--mode",
        default="normal",
        choices=["normal", "research"],
        help="Search mode: normal single-provider route or research multi-provider + extraction"
    )
    parser.add_argument(
        "--research-providers",
        nargs="+",
        help="Explicit provider list for --mode research"
    )
    parser.add_argument(
        "--research-extract-count",
        type=int,
        default=3,
        help="Number of top research-mode URLs to extract for grounding"
    )
    parser.add_argument(
        "--research-time-budget",
        type=float,
        default=55.0,
        help="Best-effort wall-clock budget for research mode; skips remaining providers/extraction between calls when exhausted"
    )
    
    # Caching options
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help=f"Cache TTL in seconds (default: {DEFAULT_CACHE_TTL} = 1 hour)"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass cache (always fetch fresh results)"
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear all cached results and exit"
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Show cache statistics and exit"
    )

    return parser


def default_search_args(config: Dict[str, Any]) -> SimpleNamespace:
    """Search options with the CLI defaults, built without argparse.

    The plugin calls the pipeline directly and never parses argv. These are
    exactly the defaults of ``build_parser(config).parse_args([])`` minus the
    maintenance-only CLI options (pinned by tests/test_direct_call_path.py).
    """
    defaults = config.get("defaults", {})
    tavily = config.get("tavily", {})
    querit = config.get("querit", {})
    linkup = config.get("linkup", {})
    exa = config.get("exa", {})
    firecrawl = config.get("firecrawl", {})
    you = config.get("you", {})
    searxng = config.get("searxng", {})
    return SimpleNamespace(
        provider=None,
        query=None,
        output_format="markdown",
        extract_images=False,
        include_raw_html=False,
        render_js=False,
        spans=False,
        spans_query=None,
        max_results=defaults.get("max_results", 5),
        images=False,
        allow_fallback=False,
        country=None,
        language=None,
        search_type=config.get("serper", {}).get("type", "search"),
        time_range=None,
        freshness=None,
        depth=tavily.get("depth", "basic"),
        topic=tavily.get("topic", "general"),
        raw_content=False,
        querit_base_url=querit.get("base_url", "https://api.querit.ai"),
        querit_base_path=querit.get("base_path", "/v1/search"),
        linkup_depth=linkup.get("depth", "standard"),
        linkup_output_type=linkup.get("output_type", "searchResults"),
        exa_type=exa.get("type", "neural"),
        exa_depth=exa.get("depth", "normal"),
        exa_verbosity=exa.get("verbosity", "standard"),
        category=None,
        start_date=None,
        end_date=None,
        similar_url=None,
        firecrawl_scrape=False,
        firecrawl_sources=list(firecrawl.get("sources", ["web"])),
        you_safesearch=you.get("safesearch", "moderate"),
        livecrawl=None,
        no_news=False,
        searxng_url=searxng.get("base_url") or searxng.get("instance_url"),
        searxng_safesearch=searxng.get("safesearch", 0),
        engines=searxng.get("engines"),
        categories=None,
        include_domains=None,
        exclude_domains=None,
        quality_report=False,
        mode="normal",
        research_providers=None,
        research_extract_count=3,
        research_time_budget=55.0,
        cache_ttl=DEFAULT_CACHE_TTL,
        no_cache=False,
    )


def main():
    # Maintenance commands live in modules a search never needs; load them here.
    from . import extract_bench_v3 as _extract_bench
    from . import state_migration_v3 as _state_migration

    config = load_config()
    parser = build_parser(config)
    args = parser.parse_args()
    args.language, config = apply_auto_language(args.language, config)

    migration_options_used = bool(
        args.apply or args.rollback or args.migration_backup_root is not None
    )
    if args.command != "state-migrate" and migration_options_used:
        parser.error("--apply, --rollback, and --migration-backup-root require state-migrate")

    if args.command == "state-migrate":
        state_path = CACHE_DIR / "v3" / "state.sqlite3"
        backup_root = args.migration_backup_root or state_path.parent / "migration-backups"
        if args.rollback:
            report = _state_migration.rollback_legacy_state(
                state_path=state_path,
                backup_root=backup_root,
                backup_id=args.rollback,
            )
        else:
            report = _state_migration.migrate_legacy_state(
                cache_root=CACHE_DIR,
                state_path=state_path,
                backup_root=backup_root,
                dry_run=not args.apply,
            )
        print(_state_migration.render_migration_report(report))
        if report.status not in {"ready", "applied", "unchanged", "rolled_back"}:
            raise SystemExit(2)
        return

    if args.command == "doctor":
        report = _build_doctor_report(config, live=args.live)
        if args.json or args.compact:
            indent = None if args.compact else 2
            print(json.dumps(report, indent=indent, ensure_ascii=False))
        else:
            print(_format_doctor_text(report))
        return

    if args.command == "bench" or args.bench:
        report = run_provider_bench(config, max_results=args.max_results)
        if args.json or args.compact:
            indent = None if args.compact else 2
            print(json.dumps(report, indent=indent, ensure_ascii=False))
        else:
            print(format_bench_text(report))
        return

    if args.command == "extract-bench":
        if not args.extract_urls:
            parser.error("extract-bench requires one or more --extract-urls")
        report = _extract_bench.run_extract_bench(
            config,
            urls=args.extract_urls,
            providers=args.bench_providers,
            timeout_budget=args.bench_timeout_budget,
        )
        if not args.no_history:
            report["history_written"] = _extract_bench.BenchmarkHistoryJournal(
                CACHE_DIR
            ).append(_extract_bench.benchmark_history_record(report))
        else:
            report["history_written"] = False
        if args.json or args.compact:
            indent = None if args.compact else 2
            print(json.dumps(report, indent=indent, ensure_ascii=False))
        else:
            print(_extract_bench.format_extract_bench_text(report))
            print("History written: {}".format(report["history_written"]))
        return

    # Handle cache management commands first (before query validation). The
    # tools answer repeats from the v3 response cache, so both commands cover it.
    response_cache = ResponseCacheV3((config.get("v3") or {}).get("cache_dir") or CACHE_DIR)
    if args.clear_cache:
        result = cache_clear()
        result["v3_response_cleared"] = response_cache.clear()
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False))
        return

    if args.cache_stats:
        result = cache_stats()
        result["v3_response"] = response_cache.stats()
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False))
        return

    if args.extract_urls is not None:
        result = extract_plus(
            urls=args.extract_urls,
            provider=args.provider or "auto",
            output_format=args.output_format,
            include_images=args.extract_images,
            include_raw_html=args.include_raw_html,
            render_js=args.render_js,
            spans=args.spans,
            spans_query=args.spans_query,
            config=config,
        )
        indent = None if args.compact else 2
        succeeded = not result.get("error") and any(
            isinstance(item, dict) and not item.get("error")
            and any(item.get(field) for field in ("content", "text", "raw_content", "markdown", "html"))
            for item in result.get("results", [])
        )
        print(json.dumps(result, indent=indent, ensure_ascii=False),
              file=sys.stdout if succeeded else sys.stderr)
        if not succeeded:
            sys.exit(1)
        return
    
    if not args.query and not args.similar_url:
        parser.error("--query is required (unless using --similar-url with Exa)")
    try:
        domain_filters(args.include_domains, args.exclude_domains)
    except ValueError as exc:
        parser.error(str(exc))
    if (
        str(args.provider or "").lower() in _providers.DOMAIN_SUFFIX_FILTER_UNSUPPORTED
        and _providers.public_suffix_entries(args.include_domains, args.exclude_domains)
    ):
        parser.error(_providers.domain_suffix_unsupported_message(str(args.provider).lower()))
    
    # Handle --explain-routing
    if args.explain_routing:
        if not args.query:
            parser.error("--query is required for --explain-routing")
        explanation = explain_routing(args.query, config)
        indent = None if args.compact else 2
        print(json.dumps(explanation, indent=indent, ensure_ascii=False))
        return

    payload, exit_code = _execute_search_request_core(args, config)
    if exit_code == 0:
        indent = None if args.compact else 2
        print(json.dumps(payload, indent=indent, ensure_ascii=False))
    else:
        print(json.dumps(payload, indent=2), file=sys.stderr)
        sys.exit(1)


def _apply_result_quality_pipeline(
    result: Dict[str, Any],
    config: Dict[str, Any],
    query: str = "",
    include_domains: Optional[List[str]] = None,
) -> None:
    """Filter mirror/SEO-spam domains and cap per-domain dominance, in place.

    Explicit domain constraints (``site:`` operators, ``include_domains``)
    express user intent and win: constrained domains are exempt from the spam
    filter, and the diversity rerank is skipped entirely — a deliberately
    one-domain query must not have its order shuffled.

    Both steps are truthful: removals and demotions are reported in
    ``result["metadata"]`` so quality reports and callers can see what changed.
    """
    results = result.get("results")
    if not isinstance(results, list) or not results:
        return
    quality_config = config.get("quality") if isinstance(config.get("quality"), dict) else {}
    constrained_domains = extract_domain_constraints(query, include_domains)
    if quality_config.get("filter_spam", True):
        allowed = list(quality_config.get("allowed_domains") or []) + constrained_domains
        kept, removed_domains = filter_spam_results(
            results,
            extra_blocked=quality_config.get("blocked_domains"),
            allowed=allowed,
        )
        if removed_domains:
            result["results"] = kept
            result.setdefault("metadata", {})["spam_filtered"] = {
                "removed_count": len(results) - len(kept),
                "domains": removed_domains,
            }
            results = kept
    if constrained_domains:
        return
    try:
        max_per_domain = int(quality_config.get("max_results_per_domain", 2))
    except (TypeError, ValueError):
        max_per_domain = 2
    if max_per_domain > 0:
        reranked, demoted = rerank_domain_diversity(results, max_per_domain=max_per_domain, query=query)
        if demoted:
            result["results"] = reranked
            result.setdefault("metadata", {})["domain_diversity_demoted"] = demoted


def _legacy_search_cache_context(
    args, provider: str, config: Dict[str, Any]
) -> Dict[str, Any]:
    locale_country, locale_language, _locale_meta = resolve_locale(
        provider,
        config,
        args.query,
        cli_country=args.country,
        cli_language=args.language,
    )
    return {
        "locale": f"{locale_country}:{locale_language or ''}",
        "freshness": args.freshness,
        "time_range": getattr(args, "time_range", None),
        "include_domains": sorted(args.include_domains)
        if args.include_domains
        else None,
        "exclude_domains": sorted(args.exclude_domains)
        if args.exclude_domains
        else None,
        "topic": args.topic,
        "search_engines": sorted(args.engines) if args.engines else None,
        "include_news": not args.no_news,
        "search_type": args.search_type,
        "exa_type": args.exa_type,
        "exa_depth": args.exa_depth,
        "exa_verbosity": args.exa_verbosity,
        "category": args.category,
        "similar_url": args.similar_url,
        "mode": args.mode,
        "quality_report": args.quality_report,
    }


def _diversity_settings(config: Dict[str, Any]) -> Tuple[bool, float]:
    """Read the validated diversity settings with safe direct-call fallbacks."""
    quality_config = config.get("quality") if isinstance(config.get("quality"), dict) else {}
    diversity_config = (
        quality_config.get("diversity")
        if isinstance(quality_config.get("diversity"), dict)
        else {}
    )
    rerank = diversity_config.get("rerank") is True
    threshold = diversity_config.get(
        "near_duplicate_threshold", DEFAULT_NEAR_DUPLICATE_THRESHOLD
    )
    if isinstance(threshold, bool):
        threshold = DEFAULT_NEAR_DUPLICATE_THRESHOLD
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_NEAR_DUPLICATE_THRESHOLD
    if threshold < 0.0 or threshold > 1.0:
        threshold = DEFAULT_NEAR_DUPLICATE_THRESHOLD
    return rerank, threshold


def _research_quorum_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Read validated Research early-return controls for direct callers too."""
    raw_quality_config = config.get("quality")
    quality_config = raw_quality_config if isinstance(raw_quality_config, dict) else {}
    configured = quality_config.get("research_quorum")
    configured = configured if isinstance(configured, dict) else {}
    defaults = {
        "enabled": True,
        "min_contributing_providers": 2,
        "result_target_cap": 5,
        "min_unique_domains": 3,
    }
    settings = {**defaults, **configured}
    # Validation owns persisted config; conservative fallbacks keep callers that
    # invoke the public search helpers with hand-built config dicts safe.
    if settings["enabled"] is not True:
        settings["enabled"] = False
    for name, minimum in (
        ("min_contributing_providers", 2),
        ("result_target_cap", 1),
        ("min_unique_domains", 1),
    ):
        value = settings[name]
        if isinstance(value, bool):
            settings[name] = defaults[name]
            continue
        try:
            settings[name] = max(minimum, int(value))
        except (TypeError, ValueError):
            settings[name] = defaults[name]
    return settings


def _result_freshness(provider, requested, result):
    """Preserve execution evidence, including cached and per-provider receipts."""
    metadata = result.setdefault("metadata", {})
    existing = metadata.get("freshness")
    if isinstance(existing, dict) and existing.get("requested") == requested:
        return existing
    return _providers.freshness_metadata(
        provider, requested,
        applied_published_dates=metadata.pop("applied_published_dates", None) or {},
    )


def _finalize_research_result(
    result: Dict[str, Any],
    *,
    args,
    config: Dict[str, Any],
    routing_info: Dict[str, Any],
    providers_considered: List[str],
    research_providers: List[str],
    cooldown_skips: List[Dict[str, Any]],
    provider_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Apply the shared public Research Mode metadata and quality envelope."""
    final_routing = dict(routing_info)
    final_routing["mode"] = "research"
    final_routing["provider"] = "research"
    result.setdefault("routing", {}).update(final_routing)
    if cooldown_skips:
        result["routing"]["providers_skipped"] = [
            {
                "provider": item.get("provider"),
                "reason": "cooldown",
                "cooldown_remaining_seconds": item.get(
                    "cooldown_remaining_seconds"
                ),
            }
            for item in cooldown_skips
        ]
    requested_freshness = _providers.effective_recency(
        getattr(args, "time_range", None), args.freshness
    )
    if requested_freshness:
        result.setdefault("metadata", {})["freshness"] = {
            "requested": requested_freshness,
            "providers": [
                _result_freshness(
                    provider, requested_freshness,
                    (provider_payloads or {}).get(provider) or {},
                )
                for provider in research_providers
            ],
        }
    research_search_type = getattr(args, "search_type", None)
    if (
        research_search_type in _providers.SEARCH_TYPE_VALUES
        and research_search_type != "search"
    ):
        result.setdefault("metadata", {})["search_type"] = {
            "requested": research_search_type,
            "providers": [
                _providers.search_type_metadata(provider, research_search_type)
                for provider in research_providers
            ],
        }
    _apply_result_quality_pipeline(
        result,
        config,
        query=args.query or "",
        include_domains=args.include_domains,
    )
    _diversity_rerank, near_duplicate_threshold = _diversity_settings(config)
    result["quality_report"] = build_quality_report(
        query=args.query,
        result=result,
        routing_info=final_routing,
        providers_considered=providers_considered,
        eligible_providers=research_providers,
        cooldown_skips=cooldown_skips,
        errors=result.get("routing", {}).get("provider_errors", []),
        near_duplicate_threshold=near_duplicate_threshold,
    )
    return result


def _execute_search_request_core(args, config: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Run the search/research pipeline for parsed args.

    Returns ``(payload, exit_code)`` where exit_code 0 means a success payload bound
    for stdout and 1 means an error payload bound for stderr. This function never
    prints or calls ``sys.exit`` so the Hermes plugin can invoke it in-process
    instead of spawning a subprocess.
    """
    config = apply_profile_effects(config)
    if getattr(args, "query", None):
        from .jev_optional import maybe_search_type

        resolved, _jev_meta = maybe_search_type(
            args.query, getattr(args, "search_type", None), config=config
        )
        args.search_type = resolved

    # Determine provider
    if args.provider == "auto" or (args.provider is None and not args.similar_url):
        if args.query:
            routing = auto_route_provider(args.query, config)
            if routing.get("error_type"):
                return {
                    "error": routing["error"],
                    "error_type": routing["error_type"],
                    "provider": "auto",
                    "query": args.query,
                    "results": [],
                    "metadata": {"profile": config.get("profile")},
                }, 1
            provider = routing["provider"]
            routing_info = {
                "auto_routed": True,
                "provider": provider,
                "confidence": routing["confidence"],
                "confidence_level": routing["confidence_level"],
                "reason": routing["reason"],
                "routing_policy": routing.get("routing_policy", ROUTING_POLICY),
                "top_signals": routing["top_signals"],
                "scores": routing["scores"],
                "auto_allow_excluded": routing.get("auto_allow_excluded", []),
                "analysis_summary": routing.get("analysis_summary", {}),
            }
        else:
            provider = "exa"
            routing_info = {
                "auto_routed": True,
                "provider": "exa",
                "confidence": 1.0,
                "confidence_level": "high",
                "reason": "similar_url_specified",
                "routing_policy": ROUTING_POLICY,
            }
    else:
        provider = args.provider or "serper"
        routing_info = {"auto_routed": False, "provider": provider, "routing_policy": ROUTING_POLICY}
    profile_deviation = (
        is_self_hosted_profile(config)
        and args.provider not in (None, "auto")
        and provider not in SELF_HOSTED_SEARCH_PROVIDER_IDS
    )
    
    # Build provider fallback list
    auto_config = config.get("auto_routing", {})
    provider_priority = auto_config.get("provider_priority", list(SEARCH_PROVIDER_IDS))
    disabled_providers = auto_config.get("disabled_providers", [])

    # Start with the selected provider, then try others in priority order.
    # Explicit provider calls are strict by default: requested provider must be
    # the actual provider. Use --allow-fallback to opt into legacy fallback.
    # Fixed-provider mode is intentionally strict: if auto-routing is disabled
    # and the saved default was selected via provider=auto, do not silently
    # fall back to other providers. Users who want fallback should keep
    # auto-routing enabled and tune priority/fallback instead.
    explicit_provider_mode = args.provider not in (None, "auto")
    fixed_provider_mode = (
        auto_config.get("enabled", True) is False
        and provider == config.get("default_provider")
        and (args.provider == "auto" or (args.provider is None and not args.similar_url))
    )
    strict_provider_mode = fixed_provider_mode or (explicit_provider_mode and not args.allow_fallback)
    providers_to_try = [provider] if provider else []
    if not strict_provider_mode:
        for p in provider_priority:
            if p not in providers_to_try and p not in disabled_providers and _provider_auto_allowed(p, auto_config) and provider_configured(p, config):
                providers_to_try.append(p)

    # The v3 AttemptEngine owns admission/circuit state. Its single-provider
    # adapter must not consult or mutate the legacy provider-health system.
    engine_owned_attempt = bool(getattr(args, "_v3_engine_owned_attempt", False))
    eligible_providers = []
    cooldown_skips = []
    if engine_owned_attempt:
        eligible_providers = list(providers_to_try)
    else:
        for p in providers_to_try:
            in_cd, remaining = provider_in_cooldown(p)
            if in_cd:
                cooldown_skips.append({"provider": p, "cooldown_remaining_seconds": remaining})
            else:
                eligible_providers.append(p)

    if not eligible_providers:
        eligible_providers = providers_to_try[:1]

    # Helper function to execute search for a provider. Provider-specific
    # kwargs-building lives in provider_dispatch.SEARCH_DISPATCH; the caller
    # providers module is passed so adapters resolve implementations late.
    provider_payloads = {}

    def execute_search(prov: str) -> Dict[str, Any]:
        validate_provider_mode(prov, "search")
        key = validate_api_key(prov, config)
        adapter = SEARCH_DISPATCH.get(prov)
        if adapter is None:
            raise ValueError(f"Unknown provider: {prov}")
        provider_result = validate_adapter_result(
            prov,
            "search",
            adapter(_providers, prov, args, key, config, routing_info),
        )
        if engine_owned_attempt:
            provider_result["_v3_raw_results"] = [
                dict(item)
                for item in (provider_result.get("results") or [])
                if isinstance(item, dict)
            ]
        provider_payloads[prov] = provider_result
        return provider_result

    def execute_with_retry(prov: str) -> Dict[str, Any]:
        # The v3 AttemptEngine owns retries and circuit state, so an
        # engine-owned call runs once here and the engine retries around it.
        # Latency samples (provider_stats) are a separate signal and are
        # recorded on both paths; the hedged fallback reads them to time the
        # next attempt.
        started = time.monotonic()
        try:
            if engine_owned_attempt:
                provider_result = execute_search(prov)
            else:
                provider_result = execute_provider_with_retry(prov, lambda: execute_search(prov))
        except ProviderRequestError:
            # Only real provider interactions count as performance signal;
            # config/validation errors (e.g. missing keys) are not recorded.
            record_provider_outcome(prov, latency_seconds=time.monotonic() - started, result_count=0, error=True)
            raise
        record_provider_outcome(
            prov,
            latency_seconds=time.monotonic() - started,
            result_count=len(provider_result.get("results", []) or []),
            error=False,
        )
        return provider_result

    cache_context = _legacy_search_cache_context(args, provider or "", config)

    providers_considered = providers_to_try.copy()

    if args.mode == "research":
        available_research_providers = {
            p for p in providers_to_try
            if p not in disabled_providers and _provider_auto_allowed(p, auto_config) and provider_configured(p, config) and not provider_in_cooldown(p)[0]
        }
        if provider and provider_configured(provider, config) and not provider_in_cooldown(provider)[0]:
            available_research_providers.add(provider)
        if args.research_providers:
            # Explicit research selection follows the same contract as an
            # explicit single-provider call: auto-routing allowlists do not
            # veto the caller's named provider. Disabled/unconfigured providers
            # remain unavailable, and cooldown skips stay visible in receipts.
            research_providers = []
            for p in args.research_providers:
                if p in disabled_providers or not provider_configured(p, config):
                    continue
                in_cooldown, remaining = provider_in_cooldown(p)
                if in_cooldown:
                    if not any(item.get("provider") == p for item in cooldown_skips):
                        cooldown_skips.append(
                            {
                                "provider": p,
                                "cooldown_remaining_seconds": remaining,
                            }
                        )
                    continue
                if p not in research_providers:
                    research_providers.append(p)
        else:
            research_providers = select_research_providers(
                primary_provider=provider,
                provider_priority=provider_priority,
                available_providers=available_research_providers,
                max_providers=3,
            )

        if not research_providers:
            error_result = {
                "error": "No configured providers available for research mode",
                "provider": provider,
                "query": args.query,
                "routing": routing_info,
                "cooldown_skips": cooldown_skips,
            }
            return error_result, 1

        diversity_rerank, near_duplicate_threshold = _diversity_settings(config)
        quorum_settings = _research_quorum_settings(config)
        result = run_research_mode(
            query=args.query,
            research_providers=research_providers,
            execute_search=execute_with_retry,
            extract_urls=lambda urls: extract_plus(
                urls=urls,
                provider="auto",
                output_format="markdown",
                config=config,
            ),
            max_results=args.max_results,
            max_extract_urls=args.research_extract_count,
            time_budget_seconds=args.research_time_budget,
            diversity_rerank=diversity_rerank,
            near_duplicate_threshold=near_duplicate_threshold,
            quorum_enabled=quorum_settings["enabled"],
            quorum_min_contributing_providers=quorum_settings[
                "min_contributing_providers"
            ],
            quorum_result_target_cap=quorum_settings["result_target_cap"],
            quorum_min_unique_domains=quorum_settings["min_unique_domains"],
        )
        result = _finalize_research_result(
            result,
            args=args,
            config=config,
            routing_info=routing_info,
            providers_considered=providers_considered,
            research_providers=research_providers,
            cooldown_skips=cooldown_skips,
            provider_payloads=provider_payloads,
        )
        return result, 0

    # Check cache first (unless --no-cache is set)
    cached_result = None
    cache_hit = False
    search_cache_ttl = effective_search_cache_ttl(
        args.query or "",
        freshness=getattr(args, "time_range", None) or getattr(args, "freshness", None),
        requested_ttl=args.cache_ttl,
        routing_class=routing_class_of(
            getattr(args, "_v3_planned_routing", None) or routing_info
        ),
    )
    if not args.no_cache and args.query:
        cached_result = cache_get(
            query=args.query,
            provider=provider,
            max_results=args.max_results,
            ttl=search_cache_ttl,
            params=cache_context,
        )
        if cached_result:
            cache_hit = True
            result = {k: v for k, v in cached_result.items() if not k.startswith("_cache_")}
            if "query" in result:
                # The key ignores case and spacing; show this caller's own query.
                result["query"] = args.query
            result["cached"] = True
            result["cache_age_seconds"] = int(time.time() - cached_result.get("_cache_timestamp", 0))

    errors = []
    successful_provider = None
    successful_results: List[Tuple[str, Dict[str, Any]]] = []
    result = None if not cache_hit else result

    for idx, current_provider in enumerate(eligible_providers):
        if cache_hit:
            successful_provider = provider
            break
        try:
            provider_result = execute_with_retry(current_provider)
            if not engine_owned_attempt:
                reset_provider_health(current_provider)
            successful_results.append((current_provider, provider_result))
            successful_provider = current_provider

            # If we have enough results, stop.
            if len(provider_result.get("results", [])) >= args.max_results:
                break

            # Only continue collecting from lower-priority providers when fallback was needed.
            if not errors:
                break
        except ProviderConfigError as e:
            if engine_owned_attempt:
                raise
            # Missing/invalid local credentials are configuration errors, not
            # provider health failures. Do not poison shared cooldown state for
            # a provider the runtime never actually contacted.
            try:
                detail = json.loads(str(e))
            except (ValueError, TypeError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            errors.append({
                "provider": current_provider,
                "error": detail.get("error") or str(e),
                **{key: detail[key] for key in ("env_var", "how_to_fix") if key in detail},
            })
            continue
        except Exception as e:
            if engine_owned_attempt:
                raise
            error_msg = str(e)
            cooldown_info = mark_provider_failure(current_provider, error_msg, retry_after=getattr(e, "retry_after", None))
            errors.append({
                "provider": current_provider,
                "error": error_msg,
                "cooldown_seconds": cooldown_info.get("cooldown_seconds"),
            })
            if len(eligible_providers) > 1:
                remaining = eligible_providers[idx + 1:]
                if remaining:
                    print(json.dumps({
                        "fallback": True,
                        "failed_provider": current_provider,
                        "error": error_msg,
                        "trying_next": remaining[0],
                    }), file=sys.stderr)
            continue

    if successful_results:
        if len(successful_results) == 1:
            result = successful_results[0][1]
        else:
            primary = successful_results[0][1].copy()
            deduped_results, dedup_count = deduplicate_results_across_providers(successful_results, args.max_results)
            primary["results"] = deduped_results
            primary["deduplicated"] = dedup_count > 0
            primary.setdefault("metadata", {})
            primary["metadata"]["dedup_count"] = dedup_count
            primary["metadata"]["providers_merged"] = [p for p, _ in successful_results]
            result = primary

    if result is not None:
        if engine_owned_attempt and getattr(args, "_v3_research_member", False):
            return result, 0
        if successful_provider != provider:
            routing_info["fallback_used"] = True
            routing_info["original_provider"] = provider
            routing_info["provider"] = successful_provider
            routing_info["fallback_errors"] = errors

        if cooldown_skips:
            routing_info["cooldown_skips"] = cooldown_skips

        # A v3 attempt is a fixed-provider search; rank and report with the
        # routing the engine planned for the request.
        planned_routing = getattr(args, "_v3_planned_routing", None)
        ranking_routing = (
            {**planned_routing, "provider": routing_info.get("provider")}
            if planned_routing
            else routing_info
        )
        routing_class = ranking_routing.get("analysis_summary", {}).get("routing_class", "general")
        if not cache_hit and isinstance(result.get("results"), list):
            reranked, rerank_metadata = rerank_results_for_intent(
                args.query or "", routing_class, result.get("results", []),
                window=getattr(args, "_v3_requested_results", None),
            )
            result["results"] = reranked
            if rerank_metadata.get("reranked"):
                result.setdefault("metadata", {})["intent_rerank"] = rerank_metadata
            _apply_result_quality_pipeline(result, config, query=args.query or "", include_domains=args.include_domains)
        requested_results = getattr(args, "_v3_requested_results", None)
        if requested_results and isinstance(result.get("results"), list):
            result["results"] = result["results"][:requested_results]

        result["routing"] = routing_info

        requested_freshness = _providers.effective_recency(
            getattr(args, "time_range", None), args.freshness
        )
        if requested_freshness:
            result.setdefault("metadata", {})["freshness"] = _result_freshness(
                successful_provider or provider, requested_freshness, result,
            )

        requested_search_type = getattr(args, "search_type", None)
        if requested_search_type in _providers.SEARCH_TYPE_VALUES and requested_search_type != "search":
            result.setdefault("metadata", {})["search_type"] = _providers.search_type_metadata(
                successful_provider or provider, requested_search_type
            )

        # Locale transparency (freshness-metadata pattern): report the
        # resolved country/language and where each came from, but only for
        # providers whose request actually carries locale parameters.
        final_provider = successful_provider or provider
        if provider_supports_locale(final_provider):
            _, _, locale_meta = resolve_locale(
                final_provider, config, args.query,
                cli_country=args.country, cli_language=args.language,
            )
            result.setdefault("metadata", {})["locale"] = locale_meta

        if profile_deviation:
            result.setdefault("metadata", {})["profile_deviation"] = True

        if (
            not cache_hit
            and not args.no_cache
            and args.query
        ):
            cache_put(
                query=args.query,
                provider=successful_provider or provider,
                max_results=args.max_results,
                result=result,
                params=cache_context,
            )

        result["cached"] = bool(cache_hit)
        if "deduplicated" not in result:
            result["deduplicated"] = False
            result.setdefault("metadata", {})
            result["metadata"].setdefault("dedup_count", 0)

        if args.quality_report:
            _diversity_rerank, near_duplicate_threshold = _diversity_settings(config)
            result["quality_report"] = build_quality_report(
                query=args.query,
                result=result,
                routing_info=ranking_routing,
                providers_considered=providers_considered,
                eligible_providers=eligible_providers,
                cooldown_skips=cooldown_skips,
                errors=errors,
                near_duplicate_threshold=near_duplicate_threshold,
            )

        return result, 0
    else:
        error_result = {
            "error": "All providers failed",
            "provider": provider,
            "query": args.query,
            "routing": routing_info,
            "provider_errors": errors,
            "cooldown_skips": cooldown_skips,
        }
        add_provider_setup_guidance(error_result, "search", eligible_providers, config,
                                    requested_provider=getattr(args, "provider", None) or "auto")
        return error_result, 1


def execute_search_request(args, config: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Backward-compatible standalone CLI seam over the unchanged provider core."""
    return _execute_search_request_core(args, config)


def _plan_search_v3(request: RequestV3, config: Dict[str, Any]) -> ProviderPlan:
    config = apply_profile_effects(config)
    routing_request = request.routing
    requested = str(routing_request.get("provider") or "auto")
    auto_config = config.get("auto_routing", {})
    if requested == "auto":
        routed = auto_route_provider(request.input["query"], config)
        if routed.get("error_type"):
            # ProviderPlan is intentionally non-empty even for a local
            # preflight error; execution returns before this placeholder can
            # be contacted.
            return ProviderPlan(("keenable",), "keenable", routing_metadata=dict(routed))
        selected = str(routed["provider"])
    else:
        selected = requested
        routed = {}
    candidates = [selected]
    disabled = set(auto_config.get("disabled_providers", []))
    research_mode = str(request.options.get("mode") or "normal") == "research"
    fixed_provider_mode = (
        requested == "auto" and auto_config.get("enabled", True) is False
    )
    expand_candidates = routing_request.get("allow_fallback", requested == "auto")
    if not fixed_provider_mode and (
        (research_mode and requested == "auto") or expand_candidates
    ):
        for provider in auto_config.get("provider_priority", list(SEARCH_PROVIDER_IDS)):
            if (
                provider not in candidates
                and provider not in disabled
                and _provider_auto_allowed(provider, auto_config)
                and provider_configured(provider, config)
            ):
                candidates.append(provider)
    if research_mode:
        # Research is a bounded fan-out, not a fallback chain. Use the same
        # diversity-biased provider selection as the standalone research path,
        # constrained to the v3 planner's eligible candidates.
        candidates = select_research_providers(
            primary_provider=selected,
            provider_priority=list(
                auto_config.get("provider_priority", list(SEARCH_PROVIDER_IDS))
            ),
            available_providers=set(candidates),
            max_providers=3,
        )
    return ProviderPlan(tuple(candidates), selected, routing_metadata=dict(routed))


# Spare results requested per search attempt so filtering can refill the top N.
SEARCH_OVERFETCH_EXTRA = 5
SEARCH_OVERFETCH_CAP = 20


def _search_args_from_v3(request: RequestV3, config: Dict[str, Any]):
    # CLI defaults without argparse; request data is never parsed as argv.
    args = default_search_args(config)
    options = request.options
    routing_request = request.routing
    args.query = request.input["query"]
    args.provider = routing_request.get("provider") or "auto"
    args.max_results = options.get("max_results", 5)
    args.exa_depth = options.get("depth", "normal")
    for name in ("time_range", "freshness", "search_type", "quality_report"):
        if options.get(name):
            setattr(args, name, options[name])
    for name in ("include_domains", "exclude_domains"):
        if options.get(name):
            setattr(args, name, list(options[name]))
    args.mode = options.get("mode", "normal")
    if args.mode != "normal":
        args.research_time_budget = options.get("research_time_budget", 55.0)
    locale = options.get("locale") or {}
    for name in ("language", "country"):
        if locale.get(name):
            setattr(args, name, locale[name])
    if routing_request.get("allow_fallback") and routing_request.get("mode") == "fixed":
        args.allow_fallback = True
    if request.cache.get("mode") == "bypass":
        args.no_cache = True
    if "ttl_seconds" in request.cache:
        args.cache_ttl = request.cache["ttl_seconds"]
    return args


def _execute_research_v3(
    request: RequestV3, plan: ProviderPlan, config: Dict[str, Any]
) -> CapabilityExecution:
    """Execute the planned research fan-out through authoritative v3 attempts."""
    v3_config = config.get("v3") or {}
    state_path = v3_config.get("state_path") or os.path.join(
        str(CACHE_DIR), "v3", "state.sqlite3"
    )
    store = SQLiteStateStore(state_path)
    budget_limit = int(
        request.budget.get(
            "max_provider_attempts",
            v3_config.get("default_max_provider_attempts", 3),
        )
    )
    # Research already gets resilience from independent provider fan-out. A
    # single try per member prevents an overdue daemon task from starting a new
    # billable retry after the caller's research deadline has elapsed.
    engine = AttemptEngine(store, max_attempts=1)
    providers = list(plan.candidate_order)
    scope = request.request_id or plan.execution_id
    contexts = {}
    receipts_by_provider = {}
    payloads_by_provider: Dict[str, Dict[str, Any]] = {}
    operation_started: Dict[str, Tuple[float, float]] = {}
    timed_out_providers: set[str] = set()
    daily_budget = _daily_preflight_budget(config)

    for provider in providers:
        contexts[provider] = provider_attempt_context(
            store, provider, Capability.SEARCH, config.get(provider) or {}, get_api_key(provider, config),
            budget_scope=scope, budget_limit_units=budget_limit, **daily_budget,
        )

    def execute_provider(provider: str) -> Dict[str, Any]:
        def operation() -> Dict[str, Any]:
            operation_started[provider] = (time.time(), time.monotonic())
            args = _search_args_from_v3(request, config)
            args.provider = provider
            args.mode = "normal"
            args.research_providers = ""
            args.allow_fallback = False
            args.no_cache = True
            args._v3_engine_owned_attempt = True
            args._v3_research_member = True
            provider_payload, exit_code = _execute_search_request_core(args, config)
            if exit_code:
                raise ProviderRequestError(
                    str(provider_payload.get("error") or "provider failed"),
                    transient=False,
                )
            return provider_payload

        attempted = engine.execute(contexts[provider], operation)
        receipts_by_provider[provider] = attempted.receipt
        if attempted.payload is None:
            message = (
                attempted.receipt.error.message
                if attempted.receipt.error is not None
                else "Provider attempt did not return a payload."
            )
            raise ProviderRequestError(message)
        payloads_by_provider[provider] = attempted.payload
        return attempted.payload

    args = _search_args_from_v3(request, config)
    time_budget_seconds = float(
        request.options.get("research_time_budget")
        or getattr(args, "research_time_budget", 55.0)
    )
    max_wall_time_ms = request.budget.get("max_wall_time_ms")
    if (
        isinstance(max_wall_time_ms, int)
        and not isinstance(max_wall_time_ms, bool)
        and max_wall_time_ms > 0
    ):
        time_budget_seconds = min(time_budget_seconds, max_wall_time_ms / 1000)
    diversity_rerank, near_duplicate_threshold = _diversity_settings(config)
    quorum_settings = _research_quorum_settings(config)
    payload = run_research_mode(
        query=str(request.input.get("query") or ""),
        research_providers=providers,
        execute_search=execute_provider,
        extract_urls=lambda urls: extract_plus(
            urls=urls,
            provider="auto",
            config=config,
        ),
        max_results=int(request.options.get("max_results") or 5),
        max_extract_urls=int(getattr(args, "research_extract_count", 3) or 3),
        time_budget_seconds=time_budget_seconds,
        on_provider_timeout=timed_out_providers.add,
        diversity_rerank=diversity_rerank,
        near_duplicate_threshold=near_duplicate_threshold,
        quorum_enabled=quorum_settings["enabled"],
        quorum_min_contributing_providers=quorum_settings[
            "min_contributing_providers"
        ],
        quorum_result_target_cap=quorum_settings["result_target_cap"],
        quorum_min_unique_domains=quorum_settings["min_unique_domains"],
    )
    extraction_error = str(
        (payload.get("routing") or {}).get("extraction_error") or ""
    ).lower()
    if "research time budget exhausted" in extraction_error:
        payload["_v3_budget_limited"] = True

    completed_providers = set(
        (payload.get("routing") or {}).get("providers_queried") or []
    )
    receipts = []
    for provider in providers:
        receipt = (
            None
            if provider in timed_out_providers
            else receipts_by_provider.get(provider)
        )
        if receipt is None:
            started = operation_started.get(provider)
            if started is not None:
                wall_started, monotonic_started = started
                receipt = engine.cancel_started(
                    contexts[provider],
                    started_at=wall_started,
                    duration_ms=int(
                        max(0.0, time.monotonic() - monotonic_started) * 1000
                    ),
                ).receipt
            else:
                receipt = engine.skip(
                    contexts[provider], SkipReason.DEADLINE_EXCEEDED
                ).receipt
        receipts.append(receipt)

    if not completed_providers:
        payload["error"] = "All research providers failed"

    routing_info = dict(plan.routing_metadata)
    routing_info.setdefault(
        "auto_routed", str(request.routing.get("provider") or "auto") == "auto"
    )
    routing_info.setdefault("routing_policy", ROUTING_POLICY)
    payload = _finalize_research_result(
        payload,
        args=args,
        config=config,
        routing_info=routing_info,
        providers_considered=providers,
        research_providers=providers,
        cooldown_skips=[],
        provider_payloads=payloads_by_provider,
    )

    raw_results = []
    for provider in providers:
        if provider not in completed_providers:
            continue
        provider_payload = payloads_by_provider.get(provider) or {}
        provider_items = provider_payload.get("_v3_raw_results") or provider_payload.get(
            "results"
        ) or []
        for item in provider_items:
            if not isinstance(item, dict):
                continue
            observation = dict(item)
            observation.setdefault("provider", provider)
            raw_results.append(observation)
    payload["_v3_raw_results"] = raw_results

    stages = ["admission", "provider_attempt"]
    if any(receipt.error is not None for receipt in receipts):
        stages.append("error_classification")
    stages.extend(["retry_circuit_update", "dedup_fingerprint"])
    return CapabilityExecution(
        payload=payload,
        provider_attempts=tuple(receipts),
        stages=tuple(stages),
    )


def _execute_search_v3(
    request: RequestV3, plan: ProviderPlan, config: Dict[str, Any]
) -> CapabilityExecution:
    profile_error = plan.routing_metadata.get("error_type")
    if profile_error:
        return CapabilityExecution(
            payload={
                "error": plan.routing_metadata.get("error"),
                "error_type": profile_error,
                "provider": "auto",
                "query": request.input["query"],
                "results": [],
                "metadata": {"profile": config.get("profile")},
            },
            stages=("admission",),
        )
    if str(request.options.get("mode") or "normal") == "research":
        return _execute_research_v3(request, plan, config)
    v3_config = config.get("v3") or {}
    state_path = v3_config.get("state_path") or os.path.join(
        str(CACHE_DIR), "v3", "state.sqlite3"
    )
    store = SQLiteStateStore(state_path)
    budget_limit = int(
        request.budget.get(
            "max_provider_attempts",
            v3_config.get("default_max_provider_attempts", 3),
        )
    )
    candidates = list(plan.candidate_order)
    has_fallback = len(candidates) > 1
    # With a fallback waiting, one quick try beats two slow ones: a provider
    # gets a single attempt and a shorter socket timeout. Without one (an
    # explicit provider), the retry and the provider's own timeout stay.
    engine = AttemptEngine(
        store,
        max_attempts=1 if has_fallback else int(v3_config.get("max_attempts_per_provider", 2)),
    )
    attempt_timeout = (
        _positive_float(v3_config.get("attempt_timeout_seconds"), 10.0) if has_fallback else None
    )
    hedge_floor = _positive_float(v3_config.get("hedge_min_delay_seconds"), 2.5)
    scope = request.request_id or plan.execution_id
    daily_budget = _daily_preflight_budget(config)
    deadline = request_deadline(request.budget)
    contexts = {
        provider: provider_attempt_context(
            store, provider, Capability.SEARCH, config.get(provider) or {}, get_api_key(provider, config),
            budget_scope=scope, budget_limit_units=budget_limit, deadline_monotonic=deadline, **daily_budget,
        )
        for provider in candidates
    }

    def operation_for(current_provider: str):
        def operation():
            args = _search_args_from_v3(request, config)
            args.provider = current_provider
            args.allow_fallback = False
            args.no_cache = True
            args._v3_engine_owned_attempt = True
            # Ask for a few spare results: spam removal and the per-domain cap
            # then refill the list instead of leaving duplicates in the top N.
            requested = int(args.max_results or 5)
            args._v3_requested_results = requested
            args.max_results = min(SEARCH_OVERFETCH_CAP, requested + SEARCH_OVERFETCH_EXTRA)
            if str(request.routing.get("provider") or "auto") == "auto":
                # Rank and report with the auto-routing decision, not the
                # fixed-provider routing of this attempt.
                args._v3_planned_routing = plan.routing_metadata
            with request_timeout_cap(attempt_timeout):
                provider_payload, exit_code = _execute_search_request_core(args, config)
            if exit_code:
                raise ProviderRequestError(
                    str(provider_payload.get("error") or "provider failed"),
                    transient=False,
                )
            return provider_payload
        return operation

    def hedge_delay(provider: str) -> float:
        usual = latency_quantile(provider, 0.75) or 0.0
        delay = max(hedge_floor, usual)
        return min(delay, attempt_timeout) if attempt_timeout else delay

    race = _race_providers(engine, candidates, contexts, operation_for, hedge_delay, deadline)
    if race.provider is None and candidates and all(
        receipt.skip_reason in _OUTAGE_SKIPS for receipt in race.receipts
    ):
        race = _last_resort_probe(engine, store, candidates[0], contexts, operation_for, race)
    receipts = race.receipts
    payload = race.payload
    successful_provider = race.provider

    if payload is None:
        payload = {
            "error": "All providers failed",
            "provider": plan.selected_provider,
            "query": request.input["query"],
            "results": [],
            "routing": {
                "provider": plan.selected_provider,
                "fallback_used": False,
            },
            "provider_errors": [
                {
                    "provider": receipt.provider,
                    "error": (
                        receipt.error.message
                        if receipt.error is not None
                        else _SKIP_REASON_TEXT.get(receipt.skip_reason, receipt.skip_reason.value)
                        if receipt.skip_reason is not None
                        else "provider attempt failed"
                    ),
                }
                for receipt in receipts
            ],
        }
        add_provider_setup_guidance(payload, "search", list(plan.candidate_order), config,
                                    requested_provider=str(request.routing.get("provider") or "auto"))
    else:
        if race.superseded:
            payload["_v3_superseded_providers"] = list(race.superseded)
        routing = payload.setdefault("routing", {})
        requested = str(request.routing.get("provider") or "auto")
        if requested == "auto":
            routing.update(plan.routing_metadata)
            routing["auto_routed"] = True
            routing["provider"] = successful_provider
            payload["provider"] = successful_provider
        if successful_provider != plan.selected_provider:
            routing["fallback_used"] = True
            routing["original_provider"] = plan.selected_provider
            routing["provider"] = successful_provider
            # No value says "slower than usual and lost the race". For that
            # case selected_failed is the closest: the receipt rules tie it to
            # a cancelled or failed prior attempt, which is what is recorded.
            routing["fallback_reason"] = (
                "insufficient_results"
                if plan.selected_provider in race.empty_providers
                else "selected_failed"
            )

    stages = ["admission", "provider_attempt"]
    if any(receipt.error is not None for receipt in receipts):
        stages.append("error_classification")
    stages.append("retry_circuit_update")
    if sum(receipt.decision == "attempted" for receipt in receipts) > 1:
        stages.append("fallback")
    stages.append("dedup_fingerprint")
    return CapabilityExecution(
        payload=payload,
        provider_attempts=tuple(receipts),
        stages=tuple(stages),
    )


_MAX_IN_FLIGHT = 2

# Skips that mean "recently down", not "not allowed". When they exclude every
# candidate, the top one still gets a probe, as 4.3.5 did: an answer from a
# provider that may have recovered beats a guaranteed "All providers failed".
_OUTAGE_SKIPS = frozenset({SkipReason.CIRCUIT_OPEN})


def _last_resort_probe(engine, store, provider, contexts, operation_for, race):
    context = contexts[provider]
    now = int(time.time())
    for error_class in (ErrorClass.TRANSIENT, ErrorClass.TIMEOUT):
        store.record_success(context.circuit_key, error_class, now=now)
    execution = engine.execute(context, operation_for(provider))
    receipts = [execution.receipt if r.provider == provider else r for r in race.receipts]
    payload = execution.payload
    if payload is None:
        return _Race(None, None, receipts, race.empty_providers)
    empty = [] if payload.get("results") else [provider]
    return _Race(provider, payload, receipts, empty)


# Why a provider was not called, as the search error lists it. The first
# failure names its cause ("Out of credits: ..."); while the resulting block
# lasts, these keep it readable instead of a bare "quota_blocked". WSP's own
# fixed text, never the provider's.
_SKIP_REASON_TEXT = {
    SkipReason.QUOTA_BLOCKED: (
        "Out of credits or quota at its last call; skipped for up to an hour. "
        "Top up the account or remove its key"
    ),
    SkipReason.AUTH_BLOCKED: "Authentication failed at its last call; skipped for a few minutes. Check its API key",
    SkipReason.RATE_LIMITED: "Rate limit reached at its last call; skipped until it resets",
    SkipReason.CIRCUIT_OPEN: "Failed several times in a row; skipped for about a minute",
    SkipReason.BUDGET_BLOCKED: "Call budget for this request or day is used up",
    SkipReason.DEADLINE_EXCEEDED: "Not tried: the request ran out of time",
}


@dataclass
class _Race:
    provider: Optional[str]
    payload: Optional[Dict[str, Any]]
    receipts: List[Any]
    empty_providers: List[str]
    # Still running when another provider's non-empty answer won; cancelled by
    # that win, not by the request's time budget.
    superseded: List[str] = field(default_factory=list)


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _race_providers(engine, candidates, contexts, operation_for, hedge_delay, deadline) -> _Race:
    """Run candidates in order, hedged: the first non-empty answer wins.

    The next candidate starts as soon as the running one fails or answers
    empty, or when it is slower than its hedge delay (its usual p75 latency,
    at least the configured floor). At most two run at once. A provider that
    is still running when another wins is reported as cancelled; it finishes
    in the background without affecting the answer.
    """
    done: "queue.Queue[str]" = queue.Queue()
    tasks: Dict[str, Tuple[DaemonTask, float, float]] = {}
    finished: Dict[str, Any] = {}
    pending = list(candidates)
    winner = None
    hedge_at = None
    deadline_hit = False

    def launch() -> None:
        nonlocal hedge_at
        provider = pending.pop(0)
        if deadline is not None and time.monotonic() >= deadline:
            finished[provider] = engine.skip(contexts[provider], SkipReason.DEADLINE_EXCEEDED)
            return
        task = DaemonTask(engine.execute, contexts[provider], operation_for(provider))
        tasks[provider] = (task, time.time(), time.monotonic())
        task.add_done_callback(lambda _task, name=provider: done.put(name))
        hedge_at = time.monotonic() + hedge_delay(provider)

    def in_flight() -> List[str]:
        return [provider for provider in tasks if provider not in finished]

    launch()
    while True:
        running = in_flight()
        if not running:
            if winner is not None or not pending:
                break
            launch()
            continue
        can_hedge = bool(pending) and len(running) < _MAX_IN_FLIGHT
        wait = max(0.0, hedge_at - time.monotonic()) if can_hedge and hedge_at is not None else None
        if deadline is not None:
            remaining = max(0.0, deadline - time.monotonic())
            wait = remaining if wait is None else min(wait, remaining)
        try:
            provider = done.get(timeout=wait)
        except queue.Empty:
            if deadline is not None and time.monotonic() >= deadline:
                deadline_hit = True
                break
            launch()  # hedge: the running provider is slower than usual
            continue
        try:
            execution = tasks[provider][0].result(timeout=0)
        except Exception:  # pragma: no cover - engine.execute converts provider errors
            execution = None
        finished[provider] = execution
        payload = execution.payload if execution is not None else None
        if payload is not None and (payload.get("results") or []):
            winner = provider
            break
        if pending and len(in_flight()) < _MAX_IN_FLIGHT:
            launch()

    empty_providers = [
        provider for provider in candidates
        if finished.get(provider) is not None and finished[provider].payload is not None
        and not (finished[provider].payload.get("results") or [])
    ]
    if winner is None and empty_providers:
        winner = empty_providers[0]  # every answer was empty: report the truthful empty result
    receipts = []
    superseded = []
    for provider in candidates:
        execution = finished.get(provider)
        if execution is not None:
            receipts.append(execution.receipt)
        elif provider in tasks:
            if not deadline_hit:
                superseded.append(provider)
            _task, started_wall, started_monotonic = tasks[provider]
            receipts.append(engine.cancel_started(
                contexts[provider],
                started_at=started_wall,
                duration_ms=int(max(0.0, time.monotonic() - started_monotonic) * 1000),
            ).receipt)
        else:
            reason = SkipReason.DEADLINE_EXCEEDED if deadline_hit else SkipReason.POLICY_EXCLUDED
            receipts.append(engine.skip(contexts[provider], reason).receipt)
    payload = finished[winner].payload if winner is not None else None
    return _Race(winner, payload, receipts, empty_providers, superseded)


def _search_cache_vary(
    request: RequestV3, plan: ProviderPlan, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Keep language "auto" apart from fixed-language entries in the v3 cache.

    A per-call "auto" never appears in the request (the contract has no value
    for it), so the cache key would equal that of a plain request while the
    provider call differs.
    """
    locale = (config.get("defaults") or {}).get("locale") or {}
    return {"language": AUTO_LANGUAGE} if is_auto_language(locale.get("language")) else {}


def _search_cache_write_eligible(
    _request: RequestV3,
    _plan: ProviderPlan,
    response: ResponseV3,
    _legacy_payload: Dict[str, Any],
    _config: Dict[str, Any],
) -> bool:
    """Cache only answers that have results.

    When every candidate answers with nothing, the cause is often transient. A
    cached empty answer would keep repeats from reaching a provider for the
    whole TTL, which defeats the empty-answer fallback.
    """
    return bool(response.results)


def _search_adapter() -> CapabilityAdapter:
    return CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=_plan_search_v3,
        execute=_execute_search_v3,
        normalize=response_from_legacy,
        cache_vary=_search_cache_vary,
        cache_write_eligible=_search_cache_write_eligible,
    )


def run_search_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3:
    """Execute a native search RequestV3 through the canonical orchestrator."""
    runtime_config = apply_profile_effects(config) if config is not None else load_config()
    return execute_v3_request(request, _search_adapter(), runtime_config).response


def run_search_request(
    *,
    query: str,
    provider: str = "auto",
    count: int = 5,
    exa_depth: str = "normal",
    time_range: Optional[str] = None,
    freshness: Optional[str] = None,
    search_type: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    mode: str = "normal",
    quality_report: bool = False,
    research_time_budget: float = 55.0,
    language: Optional[str] = None,
    country: Optional[str] = None,
    no_cache: bool = False,
    cache_ttl: Optional[int] = None,
    max_wall_time_ms: Optional[int] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a search in-process and return the result dict the CLI would emit.

    Mirrors the argv the Hermes plugin used to pass to the subprocess, then routes
    it through the same parser and pipeline so behaviour is identical without the
    interpreter-startup and JSON round-trip cost of spawning ``search.py``.
    """
    if not query and not (include_domains or exclude_domains):
        return {"error": "query is required", "provider": provider, "query": query, "results": []}
    requested = str(provider or "auto").strip().lower()
    suffix_filters = _providers.public_suffix_entries(include_domains, exclude_domains)
    try:
        freshness = _providers.normalize_freshness(freshness)
        search_type = _providers.normalize_search_type(search_type)
        domain_filters(include_domains, exclude_domains)  # raises when include_domains has no usable domain
        if suffix_filters and requested in _providers.DOMAIN_SUFFIX_FILTER_UNSUPPORTED:
            raise ValueError(_providers.domain_suffix_unsupported_message(requested))
    except ValueError as exc:
        return {"error": str(exc), "provider": provider, "query": query, "results": []}
    # One list of entries whatever the caller sent ("a.com, b.com" or a bare string).
    include_domains = domain_filter_tokens(include_domains) or None
    exclude_domains = domain_filter_tokens(exclude_domains) or None
    config = apply_profile_effects(config) if config is not None else load_config()
    if suffix_filters:
        # A suffix filter (".gov") must not route to a provider that cannot apply
        # it, neither first nor as fallback or research member.
        config = copy.deepcopy(config)
        auto = config.setdefault("auto_routing", {})
        auto["disabled_providers"] = sorted(
            set(auto.get("disabled_providers") or []) | _providers.DOMAIN_SUFFIX_FILTER_UNSUPPORTED
        )
    language, config = apply_auto_language(language, config)
    policy_mode = str((config.get("routing") or {}).get("policy_mode", "classic"))
    request = legacy_request_to_v3(
        Capability.SEARCH,
        policy_mode=policy_mode,
        payload={
            "query": query,
            "provider": provider or "auto",
            "count": count,
            "depth": exa_depth,
            "time_range": time_range,
            "freshness": freshness,
            "search_type": search_type,
            "include_domains": include_domains,
            "exclude_domains": exclude_domains,
            "mode": mode,
            "quality_report": quality_report,
            "research_time_budget": research_time_budget,
            "language": language,
            "country": country,
            "no_cache": bool(no_cache),
            "cache_ttl": int(cache_ttl) if cache_ttl is not None else 3600,
            "max_wall_time_ms": max_wall_time_ms,
        },
    )
    execution = execute_v3_request(request, _search_adapter(), config)
    return v3_response_to_legacy_search(execution)


def run_extract_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3:
    """Execute a native extract RequestV3 through the canonical orchestrator."""
    return _extract.run_extract_request_v3(request, config=config or load_config())


def run_extract_request(
    urls: List[str],
    *,
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    spans: bool = False,
    spans_query: Optional[str] = None,
    max_wall_time_ms: Optional[int] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run URL extraction in-process and return the result dict."""
    config = config or load_config()
    return extract_plus(
        urls=urls,
        provider=provider or "auto",
        output_format=output_format,
        include_images=include_images,
        include_raw_html=include_raw_html,
        render_js=render_js,
        spans=spans,
        spans_query=spans_query,
        max_wall_time_ms=max_wall_time_ms,
        config=config,
    )


if __name__ == "__main__":
    main()
