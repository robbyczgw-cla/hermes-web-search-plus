#!/usr/bin/env python3
"""
Web Search Plus — Unified Multi-Provider Search and Extraction with Intelligent Auto-Routing
Version: 2.9.1
Supports search providers: You.com, Serper, Exa, Firecrawl, Tavily, Linkup,
Brave Search, SerpBase, Querit, Parallel, Perplexity, Kilo Perplexity, SearXNG, Keenable.
Supports extract providers: Firecrawl, Linkup, Parallel, Tavily, Exa, You.com, Keenable, Serper.

Smart Routing uses multi-signal analysis:
  - Routing v2 language/script and query-class detection
  - Query intent classification (shopping, research, discovery)
  - Linguistic pattern detection (how much vs how does)
  - Product/brand recognition
  - URL detection
  - Confidence scoring

Usage:
    python3 search.py --query "..."                    # Auto-route based on query
    python3 search.py --provider [you|serper|exa|firecrawl|tavily|linkup|brave|serpbase|querit|searxng|auto] --query "..." [options]

Examples:
    python3 search.py -q "東京 AI ニュース 今日"              # → You.com (multilingual current)
    python3 search.py -q "arXiv 2024 LLM scaling laws"      # → Exa (academic discovery)
    python3 search.py -q "latest OpenSSH CVE mitigation"    # → Serper (security/current)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from http_client import (  # noqa: F401 - re-exported for backward-compatible tests/imports
    ProviderRequestError,
    TRANSIENT_HTTP_CODES,
    _read_json_response,
    _read_response_body,
    make_get_request,
    make_request,
)
from cache import (
    CACHE_DIR,
    DEFAULT_CACHE_TTL,
    cache_clear,
    cache_get,
    cache_put,
    cache_stats,
)
from config import (  # noqa: F401 - re-exported for backward-compatible tests/imports
    DEFAULT_CONFIG,
    ProviderConfigError,
    _clean_env_value,
    _deepcopy_default_config,
    _validate_runtime_config,
    _validate_searxng_url,
    get_api_key,
    keyless_public_allowed,
    load_config,
    provider_configured,
    validate_api_key,
)
from provider_health import (  # noqa: F401 - re-exported for backward-compatible tests/imports
    RETRY_BACKOFF_SECONDS,
    RETRY_JITTER_FRACTION,
    COOLDOWN_STEPS_SECONDS,
    execute_provider_with_retry,
    mark_provider_failure,
    provider_in_cooldown,
    reset_provider_health,
)
from provider_stats import record_provider_outcome
from quality import (  # noqa: F401 - re-exported for backward-compatible tests/imports
    _choose_tie_winner,
    _domain_matches_rule,
    build_authority_signals,
    build_quality_report,
    deduplicate_results_across_providers,
    extract_domain_constraints,
    filter_spam_results,
    rerank_domain_diversity,
    rerank_results_for_intent,
    select_research_providers,
)
from provider_dispatch import SEARCH_DISPATCH
from provider_registry import (
    EXTRACT_PROVIDER_IDS,
    PROVIDER_SPECS,
    SEARCH_PROVIDER_IDS,
    doctor_catalog,
)
from request_gate_v3 import validate_provider_mode
from search_locale import provider_supports_locale, resolve_locale
from env_loader import load_env_files
from research import run_research_mode
from attempt_engine_v3 import AttemptContext, AttemptEngine
from cache_v3 import peek_legacy_search
from compat_v3 import legacy_request_to_v3, v3_response_to_legacy_search
from contract_v3 import Capability, RequestV3, ResponseV3, SkipReason
from orchestrator_v3 import (
    CapabilityAdapter,
    CapabilityExecution,
    ProviderPlan,
    execute_v3_request,
)
from runtime_v3 import response_from_legacy
from state_store_v3 import SQLiteStateStore
import providers as _providers
import routing as _routing
import extract as _extract
import bench as _bench
import extract_bench_v3 as _extract_bench
import state_migration_v3 as _state_migration

# Backward-compatible cache helper aliases for older imports/tests.
get_cached_result = cache_get
cache_search_result = cache_put
clear_cache = cache_clear
get_cache_stats = cache_stats


def _load_env_file():
    """Load plugin-local, legacy parent, and Hermes profile .env files."""
    load_env_files(__file__)


ROUTING_POLICY = "routing-v2"

COMPATIBILITY_SHIM_DEPRECATION = {
    "public_surface": [
        "QueryAnalyzer",
        "auto_route_provider",
        "search_provider",
        "extract_plus",
        "get_cached_result",
        "cache_search_result",
        "clear_cache",
        "get_cache_stats",
    ],
    "internal_shims": [
        "_sync_routing_dependencies",
        "_sync_provider_dependencies",
        "_sync_extract_dependencies",
        "provider function wrappers",
    ],
    "removal_target": "after ProviderSpec registry stabilization and one documented minor release window",
    "tracking_issue": "#34",
    "policy": "Keep search.py imports working while tests/users migrate to module-level seams; do not remove wrappers in feature PRs.",
}


def get_compatibility_shim_policy() -> Dict[str, Any]:
    """Return the documented compatibility-shim policy for tests and release notes."""
    return {
        key: value.copy() if isinstance(value, list) else value
        for key, value in COMPATIBILITY_SHIM_DEPRECATION.items()
    }


def _sync_routing_dependencies() -> None:
    """Keep moved routing implementation compatible with search.py monkeypatches.

    Removal target: after ProviderSpec registry stabilization and one documented minor release window.
    """
    _routing.get_api_key = get_api_key


class QueryAnalyzer(_routing.QueryAnalyzer):
    def __init__(self, *args, **kwargs):
        _sync_routing_dependencies()
        super().__init__(*args, **kwargs)


def auto_route_provider(*args, **kwargs):
    _sync_routing_dependencies()
    return _routing.auto_route_provider(*args, **kwargs)


def explain_routing(*args, **kwargs):
    _sync_routing_dependencies()
    return _routing.explain_routing(*args, **kwargs)


def _provider_auto_allowed(*args, **kwargs):
    return _routing._provider_auto_allowed(*args, **kwargs)






# =============================================================================
# Intelligent Auto-Routing Engine
# =============================================================================













# ProviderRequestError and TRANSIENT_HTTP_CODES live in http_client.py.
# Provider cooldown/backoff constants live in provider_health.py.






# =============================================================================
# HTTP Client
# =============================================================================



# HTTP request helpers live in http_client.py and are imported above for backward-compatible monkeypatching.


# =============================================================================
# Serper (Google Search API)
# =============================================================================



# =============================================================================
# SerpBase (Google SERP API)
# =============================================================================







# =============================================================================
# Brave Search
# =============================================================================



# =============================================================================
# Tavily (Research Search)
# =============================================================================



# =============================================================================
# Querit (Multi-lingual search API for AI, with rich metadata and real-time information)
# =============================================================================





# =============================================================================
# Linkup Search
# =============================================================================



# =============================================================================
# Firecrawl Search
# =============================================================================





# =============================================================================
# Extract Plus (URL Content Extraction)
# =============================================================================


















def _sync_provider_dependencies() -> None:
    """Keep moved provider implementations compatible with search.py monkeypatches.

    Removal target: after ProviderSpec registry stabilization and one documented minor release window.
    """
    _providers.make_request = make_request
    _providers.make_get_request = make_get_request
    _providers.get_api_key = get_api_key
    _providers.load_config = load_config
    _providers.provider_in_cooldown = provider_in_cooldown
    _providers.mark_provider_failure = mark_provider_failure
    _providers.reset_provider_health = reset_provider_health
    _providers.execute_provider_with_retry = execute_provider_with_retry


# Unified freshness helpers (re-exported for tests and callers).
FRESHNESS_VALUES = _providers.FRESHNESS_VALUES
PROVIDER_FRESHNESS_FORMATS = _providers.PROVIDER_FRESHNESS_FORMATS


def normalize_freshness(*args, **kwargs):
    return _providers.normalize_freshness(*args, **kwargs)


def provider_supports_freshness(*args, **kwargs):
    return _providers.provider_supports_freshness(*args, **kwargs)


def map_freshness_for_provider(*args, **kwargs):
    return _providers.map_freshness_for_provider(*args, **kwargs)


def freshness_metadata(*args, **kwargs):
    return _providers.freshness_metadata(*args, **kwargs)


# Unified search_type helpers (re-exported for tests and callers).
SEARCH_TYPE_VALUES = _providers.SEARCH_TYPE_VALUES
PROVIDER_SEARCH_TYPES = _providers.PROVIDER_SEARCH_TYPES


def normalize_search_type(*args, **kwargs):
    return _providers.normalize_search_type(*args, **kwargs)


def provider_supports_search_type(*args, **kwargs):
    return _providers.provider_supports_search_type(*args, **kwargs)


def search_type_metadata(*args, **kwargs):
    return _providers.search_type_metadata(*args, **kwargs)


def search_serper(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_serper(*args, **kwargs)


def _strip_tracking_params(*args, **kwargs):
    return _providers._strip_tracking_params(*args, **kwargs)


def _serpbase_related_search_query(*args, **kwargs):
    return _providers._serpbase_related_search_query(*args, **kwargs)


def search_serpbase(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_serpbase(*args, **kwargs)


def search_brave(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_brave(*args, **kwargs)


def search_tavily(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_tavily(*args, **kwargs)


def _map_querit_time_range(*args, **kwargs):
    return _providers._map_querit_time_range(*args, **kwargs)


def search_querit(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_querit(*args, **kwargs)


def search_linkup(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_linkup(*args, **kwargs)


def _map_firecrawl_time_range(*args, **kwargs):
    return _providers._map_firecrawl_time_range(*args, **kwargs)


def search_firecrawl(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_firecrawl(*args, **kwargs)


def _normalize_extract_result(*args, **kwargs):
    return _providers._normalize_extract_result(*args, **kwargs)


def extract_firecrawl(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_firecrawl(*args, **kwargs)


def extract_linkup(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_linkup(*args, **kwargs)


def extract_tavily(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_tavily(*args, **kwargs)


def extract_exa(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_exa(*args, **kwargs)


def extract_you(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_you(*args, **kwargs)


def extract_parallel(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_parallel(*args, **kwargs)


def search_exa(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_exa(*args, **kwargs)


def search_parallel(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_parallel(*args, **kwargs)


def search_perplexity(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_perplexity(*args, **kwargs)


def search_you(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_you(*args, **kwargs)


def search_searxng(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_searxng(*args, **kwargs)


def search_keenable(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.search_keenable(*args, **kwargs)


def extract_keenable(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_keenable(*args, **kwargs)


def extract_serper(*args, **kwargs):
    _sync_provider_dependencies()
    return _providers.extract_serper(*args, **kwargs)



# =============================================================================
# Exa (Neural/Semantic/Deep Search)
# =============================================================================



# =============================================================================
# Parallel (LLM-ready web search)
# =============================================================================



# =============================================================================
# Perplexity-compatible Direct Answers
# =============================================================================




# =============================================================================
# You.com (LLM-Ready Web & News Search)
# =============================================================================



# =============================================================================
# SearXNG (Privacy-First Meta-Search)
# =============================================================================



# =============================================================================
# CLI
# =============================================================================


def _sync_extract_dependencies() -> None:
    """Keep moved extract orchestrator compatible with search.py monkeypatches.

    Removal target: after ProviderSpec registry stabilization and one documented minor release window.
    """
    _extract.get_api_key = get_api_key
    _extract.load_config = load_config
    _extract.provider_in_cooldown = provider_in_cooldown
    _extract.mark_provider_failure = mark_provider_failure
    _extract.reset_provider_health = reset_provider_health
    _extract.execute_provider_with_retry = execute_provider_with_retry
    _extract.extract_firecrawl = extract_firecrawl
    _extract.extract_linkup = extract_linkup
    _extract.extract_tavily = extract_tavily
    _extract.extract_exa = extract_exa
    _extract.extract_you = extract_you
    _extract.extract_parallel = extract_parallel
    _extract.extract_keenable = extract_keenable
    _extract.extract_serper = extract_serper


EXTRACT_PROVIDER_PRIORITY = _extract.EXTRACT_PROVIDER_PRIORITY
resolve_extract_provider_priority = _extract.resolve_extract_provider_priority


PROVIDER_DOCTOR_CATALOG = doctor_catalog()


def extract_plus(*args, **kwargs):
    _sync_extract_dependencies()
    return _extract.extract_plus(*args, **kwargs)


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
    }


def run_provider_bench(config: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    """Run the in-process provider bakeoff (see bench.py) and return its report.

    Passes this module as the provider seam so bench honours the same
    monkeypatch surface as the rest of the pipeline (``search.search_you``
    etc.), and never records provider health cooldowns or provider stats.
    """
    return _bench.run_bench(config, search_module=sys.modules[__name__], **kwargs)


format_bench_text = _bench.format_bench_text


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


def build_parser(config: Dict[str, Any]) -> argparse.ArgumentParser:
    """Build the search.py CLI argument parser.

    Shared by the CLI ``main()`` entry and the in-process ``run_search_request``
    helper so the Hermes plugin can route argv through the exact same parsing and
    defaults without spawning a subprocess.
    """
    parser = argparse.ArgumentParser(
        description="Web Search Plus — Intelligent multi-provider search with smart auto-routing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Intelligent Auto-Routing:
  The query is analyzed using multi-signal detection to find the optimal provider:
  
  Shopping Intent → Serper (Google)
    "how much", "price of", "buy", product+brand combos, deals, specs
  
  Research Intent → Tavily  
    "how does", "explain", "what is", analysis, pros/cons, tutorials

  Multilingual + Real-Time AI Search → Querit
    multilingual search, metadata-rich results, current information for AI workflows
  
  Discovery Intent → Exa (Neural)
    "similar to", "companies like", "alternatives", URLs, startups, papers

Examples:
  python3 search.py -q "iPhone 16 Pro Max price"          # → Serper (shopping)
  python3 search.py -q "how does HTTPS encryption work"   # → Tavily (research)
  python3 search.py -q "startups similar to Notion"       # → Exa (discovery)
  python3 search.py --explain-routing -q "your query"     # Debug routing

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
        help="Search query"
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
        help="ISO 639-1 language override (e.g. de); beats config defaults and query language inference"
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
            "native news vertical (currently serper) serve it directly; all other providers "
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
            "keenable, you, and searxng); otherwise the search runs "
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
        default=searxng_config.get("instance_url"),
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


def main():
    config = load_config()
    parser = build_parser(config)
    args = parser.parse_args()

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

    # Handle cache management commands first (before query validation)
    if args.clear_cache:
        result = cache_clear()
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False))
        return
    
    if args.cache_stats:
        result = cache_stats()
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
            config=config,
        )
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False))
        return
    
    if not args.query and not args.similar_url:
        parser.error("--query is required (unless using --similar-url with Exa)")
    
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
        reranked, demoted = rerank_domain_diversity(results, max_per_domain=max_per_domain)
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
        "locale": f"{locale_country}:{locale_language}",
        "freshness": args.freshness,
        "time_range": args.time_range,
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


def _execute_search_request_core(args, config: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Run the search/research pipeline for parsed args.

    Returns ``(payload, exit_code)`` where exit_code 0 means a success payload bound
    for stdout and 1 means an error payload bound for stderr. This function never
    prints or calls ``sys.exit`` so the Hermes plugin can invoke it in-process
    instead of spawning a subprocess.
    """
    # Determine provider
    if args.provider == "auto" or (args.provider is None and not args.similar_url):
        if args.query:
            routing = auto_route_provider(args.query, config)
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
    # namespace (globals()) is passed so adapters resolve search_<provider>
    # late and honour monkeypatches on this module (search.search_you etc.).
    def execute_search(prov: str) -> Dict[str, Any]:
        validate_provider_mode(prov, "search")
        key = validate_api_key(prov, config)
        adapter = SEARCH_DISPATCH.get(prov)
        if adapter is None:
            raise ValueError(f"Unknown provider: {prov}")
        provider_result = adapter(globals(), prov, args, key, config, routing_info)
        if engine_owned_attempt:
            provider_result["_v3_raw_results"] = [
                dict(item)
                for item in (provider_result.get("results") or [])
                if isinstance(item, dict)
            ]
        return provider_result

    def execute_with_retry(prov: str) -> Dict[str, Any]:
        if engine_owned_attempt:
            return execute_search(prov)
        started = time.monotonic()
        try:
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
            research_providers = [
                p for p in args.research_providers
                if p not in disabled_providers and _provider_auto_allowed(p, auto_config) and provider_configured(p, config) and not provider_in_cooldown(p)[0]
            ]
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
        )
        routing_info["mode"] = "research"
        routing_info["provider"] = "research"
        result["routing"].update(routing_info)
        if args.freshness:
            result.setdefault("metadata", {})["freshness"] = {
                "requested": args.freshness,
                "providers": [
                    _providers.freshness_metadata(p, args.freshness) for p in research_providers
                ],
            }
        research_search_type = getattr(args, "search_type", None)
        if research_search_type in _providers.SEARCH_TYPE_VALUES and research_search_type != "search":
            result.setdefault("metadata", {})["search_type"] = {
                "requested": research_search_type,
                "providers": [
                    _providers.search_type_metadata(p, research_search_type) for p in research_providers
                ],
            }
        _apply_result_quality_pipeline(result, config, query=args.query or "", include_domains=args.include_domains)
        result["quality_report"] = build_quality_report(
            query=args.query,
            result=result,
            routing_info=routing_info,
            providers_considered=providers_considered,
            eligible_providers=research_providers,
            cooldown_skips=cooldown_skips,
            errors=result.get("routing", {}).get("provider_errors", []),
        )
        return result, 0

    # Check cache first (unless --no-cache is set)
    cached_result = None
    cache_hit = False
    if not args.no_cache and args.query:
        cached_result = cache_get(
            query=args.query,
            provider=provider,
            max_results=args.max_results,
            ttl=args.cache_ttl,
            params=cache_context,
        )
        if cached_result:
            cache_hit = True
            result = {k: v for k, v in cached_result.items() if not k.startswith("_cache_")}
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
            errors.append({
                "provider": current_provider,
                "error": str(e),
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
        if successful_provider != provider:
            routing_info["fallback_used"] = True
            routing_info["original_provider"] = provider
            routing_info["provider"] = successful_provider
            routing_info["fallback_errors"] = errors

        if cooldown_skips:
            routing_info["cooldown_skips"] = cooldown_skips

        routing_class = routing_info.get("analysis_summary", {}).get("routing_class", "general")
        if not cache_hit and isinstance(result.get("results"), list):
            reranked, rerank_metadata = rerank_results_for_intent(args.query or "", routing_class, result.get("results", []))
            result["results"] = reranked
            if rerank_metadata.get("reranked"):
                result.setdefault("metadata", {})["intent_rerank"] = rerank_metadata
            _apply_result_quality_pipeline(result, config, query=args.query or "", include_domains=args.include_domains)

        result["routing"] = routing_info

        if args.freshness:
            result.setdefault("metadata", {})["freshness"] = _providers.freshness_metadata(
                successful_provider or provider, args.freshness
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

        if (
            not cache_hit
            and not args.no_cache
            and args.query
            and not getattr(args, "_v3_no_legacy_cache_write", False)
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
            result["quality_report"] = build_quality_report(
                query=args.query,
                result=result,
                routing_info=routing_info,
                providers_considered=providers_considered,
                eligible_providers=eligible_providers,
                cooldown_skips=cooldown_skips,
                errors=errors,
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
        return error_result, 1


def execute_search_request(args, config: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Backward-compatible standalone CLI seam over the unchanged provider core."""
    return _execute_search_request_core(args, config)


def _plan_search_v3(request: RequestV3, config: Dict[str, Any]) -> ProviderPlan:
    routing_request = request.routing
    requested = str(routing_request.get("provider") or "auto")
    auto_config = config.get("auto_routing", {})
    if requested == "auto":
        routed = auto_route_provider(request.input["query"], config)
        selected = str(routed["provider"])
    else:
        selected = requested
        routed = {}
    candidates = [selected]
    if routing_request.get("allow_fallback", requested == "auto"):
        disabled = set(auto_config.get("disabled_providers", []))
        for provider in auto_config.get("provider_priority", list(SEARCH_PROVIDER_IDS)):
            if (
                provider not in candidates
                and provider not in disabled
                and _provider_auto_allowed(provider, auto_config)
                and provider_configured(provider, config)
            ):
                candidates.append(provider)
    return ProviderPlan(tuple(candidates), selected, routing_metadata=dict(routed))


def _search_args_from_v3(request: RequestV3, config: Dict[str, Any]):
    options = request.options
    routing_request = request.routing
    argv: List[str] = [
        "--query", request.input["query"],
        "--provider", str(routing_request.get("provider") or "auto"),
        "--max-results", str(options.get("max_results", 5)),
    ]
    if options.get("depth", "normal") != "normal":
        argv += ["--exa-depth", str(options["depth"])]
    if options.get("time_range"):
        argv += ["--time-range", str(options["time_range"])]
    if options.get("freshness"):
        argv += ["--freshness", str(options["freshness"])]
    if options.get("search_type"):
        argv += ["--search-type", str(options["search_type"])]
    if options.get("include_domains"):
        argv += ["--include-domains", *options["include_domains"]]
    if options.get("exclude_domains"):
        argv += ["--exclude-domains", *options["exclude_domains"]]
    if options.get("mode", "normal") != "normal":
        argv += ["--mode", str(options["mode"]), "--research-time-budget", str(options.get("research_time_budget", 55.0))]
    if options.get("quality_report"):
        argv += ["--quality-report"]
    locale = options.get("locale") or {}
    if locale.get("language"):
        argv += ["--language", str(locale["language"])]
    if locale.get("country"):
        argv += ["--country", str(locale["country"])]
    if routing_request.get("allow_fallback") and routing_request.get("mode") == "fixed":
        argv += ["--allow-fallback"]
    if request.cache.get("mode") == "bypass":
        argv += ["--no-cache"]
    if "ttl_seconds" in request.cache:
        argv += ["--cache-ttl", str(request.cache["ttl_seconds"])]
    args = build_parser(config).parse_args(argv)
    args._v3_no_legacy_cache_write = True
    return args


def _lookup_legacy_search_v3(
    request: RequestV3, plan: ProviderPlan, config: Dict[str, Any]
) -> CapabilityExecution | None:
    legacy_args = _search_args_from_v3(request, config)
    legacy_args.provider = plan.selected_provider
    if legacy_args.mode == "research":
        return None
    legacy_lookup = peek_legacy_search(
        CACHE_DIR,
        query=legacy_args.query,
        provider=plan.selected_provider,
        max_results=legacy_args.max_results,
        params=_legacy_search_cache_context(
            legacy_args, plan.selected_provider, config
        ),
        ttl_seconds=int(request.cache.get("ttl_seconds", 3600)),
        now=int(time.time()),
    )
    if legacy_lookup.legacy_payload is None:
        return None
    return CapabilityExecution(
        payload=legacy_lookup.legacy_payload,
        provider_attempts=(),
        stages=("dedup_fingerprint",),
    )


def _execute_search_v3(
    request: RequestV3, plan: ProviderPlan, config: Dict[str, Any]
) -> CapabilityExecution:
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
    engine = AttemptEngine(
        store,
        max_attempts=int(v3_config.get("max_attempts_per_provider", 2)),
    )
    receipts = []
    payload = None
    successful_provider = None
    scope = request.request_id or plan.execution_id

    for provider in plan.candidate_order:
        provider_config = config.get(provider) or {}
        endpoint = str(
            provider_config.get("endpoint")
            or provider_config.get("base_url")
            or provider_config.get("url")
            or f"provider://{provider}/search"
        )
        credential = get_api_key(provider, config) or f"keyless:{provider}"
        context = AttemptContext(
            provider=provider,
            capability=Capability.SEARCH,
            endpoint=endpoint,
            credential_fingerprint=store.fingerprint_credential(credential),
            budget_scope=scope,
            budget_window="request",
            budget_limit_units=budget_limit,
        )
        if payload is not None:
            receipts.append(
                engine.skip(context, SkipReason.POLICY_EXCLUDED).receipt
            )
            continue

        def operation(current_provider=provider):
            args = _search_args_from_v3(request, config)
            args.provider = current_provider
            args.allow_fallback = False
            args.no_cache = True
            args._v3_engine_owned_attempt = True
            provider_payload, exit_code = _execute_search_request_core(args, config)
            if exit_code:
                raise ProviderRequestError(
                    str(provider_payload.get("error") or "provider failed"),
                    transient=False,
                )
            return provider_payload

        attempted = engine.execute(context, operation)
        receipts.append(attempted.receipt)
        if attempted.payload is not None:
            payload = attempted.payload
            successful_provider = provider
            continue

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
                        else receipt.skip_reason.value
                        if receipt.skip_reason is not None
                        else "provider attempt failed"
                    ),
                }
                for receipt in receipts
            ],
        }
    else:
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


def _search_adapter() -> CapabilityAdapter:
    return CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=_plan_search_v3,
        execute=_execute_search_v3,
        normalize=response_from_legacy,
        legacy_cache_lookup=_lookup_legacy_search_v3,
    )


def run_search_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3:
    """Execute a native search RequestV3 through the canonical orchestrator."""
    runtime_config = config or load_config()
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
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a search in-process and return the result dict the CLI would emit.

    Mirrors the argv the Hermes plugin used to pass to the subprocess, then routes
    it through the same parser and pipeline so behaviour is identical without the
    interpreter-startup and JSON round-trip cost of spawning ``search.py``.
    """
    if not query and not (include_domains or exclude_domains):
        return {"error": "query is required", "provider": provider, "query": query, "results": []}
    try:
        freshness = _providers.normalize_freshness(freshness)
        search_type = _providers.normalize_search_type(search_type)
    except ValueError as exc:
        return {"error": str(exc), "provider": provider, "query": query, "results": []}
    config = config or load_config()
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
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
    _sync_extract_dependencies()
    return _extract.run_extract_request_v3(request, config=config or load_config())


def run_extract_request(
    urls: List[str],
    *,
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
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
        config=config,
    )


if __name__ == "__main__":
    main()
