#!/usr/bin/env python3
"""
Web Search Plus — Unified Multi-Provider Search and Extraction with Intelligent Auto-Routing
Version: 2.1.0
Supports search providers: You.com, Serper, Exa, Firecrawl, Tavily, Linkup,
Brave Search, SerpBase, Querit, Perplexity, Kilo Perplexity, SearXNG.
Supports extract providers: Firecrawl, Linkup, Tavily, Exa, You.com.

Smart Routing uses multi-signal analysis:
  - Routing v2 language/script and query-class detection
  - Query intent classification (shopping, research, discovery)
  - Linguistic pattern detection (how much vs how does)
  - Product/brand recognition
  - URL detection
  - Confidence scoring

Usage:
    python3 search.py --query "..."                    # Auto-route based on query
    python3 search.py --provider [you|serper|exa|firecrawl|tavily|linkup|brave|serpbase|querit|perplexity|kilo-perplexity|searxng|auto] --query "..." [options]

Examples:
    python3 search.py -q "東京 AI ニュース 今日"              # → You.com (multilingual current)
    python3 search.py -q "arXiv 2024 LLM scaling laws"      # → Exa (academic discovery)
    python3 search.py -q "latest OpenSSH CVE mitigation"    # → Serper (security/current)
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import IncompleteRead
import gzip
import hashlib
import json
import os
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, parse_qsl, urlunparse

ROUTING_POLICY = "routing-v2"


# =============================================================================
# Result Caching
# =============================================================================

CACHE_DIR = Path(os.environ.get("WSP_CACHE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")))
PROVIDER_HEALTH_FILE = CACHE_DIR / "provider_health.json"
DEFAULT_CACHE_TTL = 3600  # 1 hour in seconds


def _build_cache_payload(query: str, provider: str, max_results: int, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build normalized payload used for cache key hashing."""
    payload = {
        "query": query,
        "provider": provider,
        "max_results": max_results,
    }
    if params:
        payload.update(params)
    return payload


def _get_cache_key(query: str, provider: str, max_results: int, params: Optional[Dict[str, Any]] = None) -> str:
    """Generate a unique cache key from all relevant query parameters."""
    payload = _build_cache_payload(query, provider, max_results, params)
    key_string = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(key_string.encode("utf-8")).hexdigest()[:32]


def _get_cache_path(cache_key: str) -> Path:
    """Get the file path for a cache entry."""
    return CACHE_DIR / f"{cache_key}.json"


def _ensure_cache_dir() -> None:
    """Create cache directory if it doesn't exist."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_get(query: str, provider: str, max_results: int, ttl: int = DEFAULT_CACHE_TTL, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieve cached search results if they exist and are not expired.
    
    Args:
        query: The search query
        provider: The search provider
        max_results: Maximum results requested
        ttl: Time-to-live in seconds (default: 1 hour)
    
    Returns:
        Cached result dict or None if not found/expired
    """
    cache_key = _get_cache_key(query, provider, max_results, params)
    cache_path = _get_cache_path(cache_key)
    
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        
        cached_time = cached.get("_cache_timestamp", 0)
        if time.time() - cached_time > ttl:
            # Cache expired, remove it
            cache_path.unlink(missing_ok=True)
            return None
        
        return cached
    except (json.JSONDecodeError, IOError, KeyError):
        # Corrupted cache file, remove it
        cache_path.unlink(missing_ok=True)
        return None


def cache_put(query: str, provider: str, max_results: int, result: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> None:
    """
    Store search results in cache.
    
    Args:
        query: The search query
        provider: The search provider  
        max_results: Maximum results requested
        result: The search result to cache
    """
    _ensure_cache_dir()
    
    cache_key = _get_cache_key(query, provider, max_results, params)
    cache_path = _get_cache_path(cache_key)
    
    # Add cache metadata
    cached_result = result.copy()
    cached_result["_cache_timestamp"] = time.time()
    cached_result["_cache_key"] = cache_key
    cached_result["_cache_query"] = query
    cached_result["_cache_provider"] = provider
    cached_result["_cache_max_results"] = max_results
    cached_result["_cache_params"] = params or {}
    
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cached_result, f, ensure_ascii=False, indent=2)
    except IOError as e:
        # Non-fatal: log to stderr but don't fail
        print(json.dumps({"cache_write_error": str(e)}), file=sys.stderr)


def cache_clear() -> Dict[str, Any]:
    """
    Clear all cached results.
    
    Returns:
        Stats about what was cleared
    """
    if not CACHE_DIR.exists():
        return {"cleared": 0, "message": "Cache directory does not exist"}
    
    count = 0
    size_freed = 0
    
    for cache_file in CACHE_DIR.glob("*.json"):
        if cache_file.name == PROVIDER_HEALTH_FILE.name:
            continue
        try:
            size_freed += cache_file.stat().st_size
            cache_file.unlink()
            count += 1
        except IOError:
            pass
    
    return {
        "cleared": count,
        "size_freed_bytes": size_freed,
        "size_freed_kb": round(size_freed / 1024, 2),
        "message": f"Cleared {count} cached entries"
    }


def cache_stats() -> Dict[str, Any]:
    """
    Get statistics about the cache.
    
    Returns:
        Dict with cache statistics
    """
    if not CACHE_DIR.exists():
        return {
            "total_entries": 0,
            "total_size_bytes": 0,
            "total_size_kb": 0,
            "oldest": None,
            "newest": None,
            "cache_dir": str(CACHE_DIR),
            "exists": False
        }
    
    entries = [p for p in CACHE_DIR.glob("*.json") if p.name != PROVIDER_HEALTH_FILE.name]
    total_size = 0
    oldest_time = None
    newest_time = None
    oldest_query = None
    newest_query = None
    provider_counts = {}
    
    for cache_file in entries:
        try:
            stat = cache_file.stat()
            total_size += stat.st_size
            
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            
            ts = cached.get("_cache_timestamp", 0)
            query = cached.get("_cache_query", "unknown")
            provider = cached.get("_cache_provider", "unknown")
            
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            
            if oldest_time is None or ts < oldest_time:
                oldest_time = ts
                oldest_query = query
            if newest_time is None or ts > newest_time:
                newest_time = ts
                newest_query = query
        except (json.JSONDecodeError, IOError):
            pass
    
    return {
        "total_entries": len(entries),
        "total_size_bytes": total_size,
        "total_size_kb": round(total_size / 1024, 2),
        "providers": provider_counts,
        "oldest": {
            "timestamp": oldest_time,
            "age_seconds": int(time.time() - oldest_time) if oldest_time else None,
            "query": oldest_query
        } if oldest_time else None,
        "newest": {
            "timestamp": newest_time,
            "age_seconds": int(time.time() - newest_time) if newest_time else None,
            "query": newest_query
        } if newest_time else None,
        "cache_dir": str(CACHE_DIR),
        "exists": True
    }


# =============================================================================
# Auto-load .env from skill directory (if exists)
# =============================================================================
def _load_env_file():
    """Load .env files from plugin-local and legacy parent locations."""
    env_paths = [
        Path(__file__).parent / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for env_path in env_paths:
        if not env_path.exists():
            continue
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    # Handle export VAR=value or VAR=value
                    if line.startswith("export "):
                        line = line[7:]
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value

_load_env_file()


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_CONFIG = {
    "version": 1,
    "default_provider": None,
    "defaults": {
        "provider": "serper",
        "max_results": 5
    },
    "auto_routing": {
        "enabled": True,
        "fallback_provider": "serper",
        # Low-trust / experimental providers can stay configured for explicit use
        # without being selected automatically.
        "provider_priority": ["you", "serper", "exa", "firecrawl", "tavily", "linkup", "brave", "serpbase", "querit", "kilo-perplexity", "perplexity", "searxng"],
        "disabled_providers": [],
        "auto_allow": {
            "serpbase": False,
            "querit": False,
            "brave": False,
            "kilo-perplexity": False,
            "perplexity": False,
        },
        "confidence_threshold": 0.3,  # Below this, note low confidence
    },
    "serper": {
        "country": "us",
        "language": "en",
        "type": "search"
    },
    "brave": {
        "country": "US",
        "search_lang": "en",
        "safesearch": "moderate",
    },
    "tavily": {
        "depth": "basic",
        "topic": "general"
    },
    "querit": {
        "base_url": "https://api.querit.ai",
        "base_path": "/v1/search",
        "timeout": 10
    },
    "linkup": {
        "api_url": "https://api.linkup.so/v1/search",
        "depth": "standard",
        "output_type": "searchResults",
        "timeout": 30
    },
    "exa": {
        "type": "neural",
        "depth": "normal",
        "verbosity": "standard"
    },
    "perplexity": {
        "api_url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar-pro"
    },
    "kilo-perplexity": {
        "api_url": "https://api.kilo.ai/api/gateway/chat/completions",
        "model": "perplexity/sonar-pro"
    },
    "firecrawl": {
        "api_url": "https://api.firecrawl.dev/v2/search",
        "country": "US",
        "timeout": 30000,
        "sources": ["web"],
        "ignore_invalid_urls": False
    },
    "you": {
        "country": "us",
        "safesearch": "moderate"
    },
    "serpbase": {
        "api_url": "https://api.serpbase.dev/google/search",
        "country": "us",
        "language": "en",
        "page": 1,
        "timeout": 30,
    },
    "searxng": {
        "instance_url": None,  # Required - user must set their own instance
        "safesearch": 0,  # 0=off, 1=moderate, 2=strict
        "engines": None,  # Optional list of engines to use
        "language": "en"
    }
}


def _deepcopy_default_config() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


_ROUTING_PROVIDER_NAMES = {"tavily", "linkup", "querit", "exa", "firecrawl", "perplexity", "kilo-perplexity", "brave", "serper", "serpbase", "you", "searxng"}


def _normalize_routing_provider_config(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized == "kilo_perplexity":
        normalized = "kilo-perplexity"
    if normalized not in _ROUTING_PROVIDER_NAMES:
        raise ValueError(f"unknown routing provider: {provider}")
    return normalized


def _normalize_routing_provider_list_config(value: Any) -> List[str]:
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        raw_values = [str(item).strip() for item in value]
    else:
        raise ValueError("provider list must be a string or list")
    providers = []
    seen = set()
    for raw in raw_values:
        if not raw:
            continue
        provider = _normalize_routing_provider_config(raw)
        if provider in seen:
            continue
        seen.add(provider)
        providers.append(provider)
    if not providers:
        raise ValueError("provider list cannot be empty")
    return providers


def _validate_runtime_config(config: Dict[str, Any]) -> Dict[str, Any]:
    auto = config.get("auto_routing", {})
    if not isinstance(auto, dict):
        raise ValueError("auto_routing must be an object")
    if config.get("default_provider"):
        config["default_provider"] = _normalize_routing_provider_config(str(config["default_provider"]))
    if auto.get("fallback_provider"):
        auto["fallback_provider"] = _normalize_routing_provider_config(str(auto["fallback_provider"]))
    if auto.get("provider_priority"):
        auto["provider_priority"] = _normalize_routing_provider_list_config(auto["provider_priority"])
    if "disabled_providers" in auto:
        disabled = auto.get("disabled_providers") or []
        if disabled:
            auto["disabled_providers"] = _normalize_routing_provider_list_config(disabled)
        else:
            auto["disabled_providers"] = []
    if "auto_allow" in auto:
        raw_allow = auto.get("auto_allow") or {}
        if not isinstance(raw_allow, dict):
            raise ValueError("auto_allow must be an object mapping provider names to booleans")
        normalized_allow = dict(DEFAULT_CONFIG["auto_routing"].get("auto_allow", {}))
        for raw_provider, allowed in raw_allow.items():
            provider = _normalize_routing_provider_config(str(raw_provider))
            normalized_allow[provider] = bool(allowed)
        auto["auto_allow"] = normalized_allow
    else:
        auto["auto_allow"] = dict(DEFAULT_CONFIG["auto_routing"].get("auto_allow", {}))
    if "confidence_threshold" in auto:
        threshold = float(auto["confidence_threshold"])
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("confidence_threshold must be between 0.0 and 1.0")
        auto["confidence_threshold"] = threshold
    if config.get("default_provider") and config["default_provider"] in set(auto.get("disabled_providers", [])):
        raise ValueError("default_provider cannot be disabled")
    config["auto_routing"] = auto
    return config


def _unique_timestamped_path(path: Path, marker: str) -> Path:
    base = path.with_name(path.name + f".{marker}-{int(time.time())}")
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(base.name + f"-{suffix}")
        suffix += 1
    return candidate


def _quarantine_runtime_config(config_path: Path, reason: str) -> None:
    broken = _unique_timestamped_path(config_path, "broken")
    try:
        config_path.rename(broken)
        print(json.dumps({
            "warning": f"Invalid config moved to {broken}: {reason}",
            "using": "default configuration",
        }), file=sys.stderr)
    except OSError as exc:
        print(json.dumps({
            "warning": f"Invalid config could not be moved: {exc}; reason: {reason}",
            "using": "default configuration",
        }), file=sys.stderr)


def load_config() -> Dict[str, Any]:
    """Load configuration from config.json if it exists, with defaults."""
    config = _deepcopy_default_config()
    config_path = Path(os.environ.get("WEB_SEARCH_PLUS_CONFIG") or (Path(__file__).parent.parent / "config.json"))

    if config_path.exists():
        try:
            with open(config_path) as f:
                user_config = json.load(f)
                for key, value in user_config.items():
                    if isinstance(value, dict) and key in config:
                        config[key] = {**config.get(key, {}), **value}
                    else:
                        config[key] = value
            config = _validate_runtime_config(config)
        except (json.JSONDecodeError, IOError, ValueError, TypeError) as e:
            _quarantine_runtime_config(config_path, str(e))
            config = _deepcopy_default_config()

    return config


def get_api_key(provider: str, config: Dict[str, Any] = None) -> Optional[str]:
    """Get API key for provider from config.json or environment.
    
    Priority: config.json > .env > environment variable
    
    Note: SearXNG doesn't require an API key, but returns instance_url if configured.
    """
    # Special case: SearXNG uses instance_url instead of API key
    if provider == "searxng":
        return get_searxng_instance_url(config)
    
    # Check config.json first
    if config:
        provider_config = config.get(provider, {})
        if isinstance(provider_config, dict):
            key = provider_config.get("api_key") or provider_config.get("apiKey")
            if key:
                return key
    
    # Then check environment
    if provider == "perplexity":
        return os.environ.get("PERPLEXITY_API_KEY")
    if provider == "kilo-perplexity":
        return os.environ.get("KILOCODE_API_KEY")
    key_map = {
        "serper": "SERPER_API_KEY",
        "serpbase": "SERPBASE_API_KEY",
        "brave": "BRAVE_API_KEY",
        "tavily": "TAVILY_API_KEY",
        "querit": "QUERIT_API_KEY",
        "linkup": "LINKUP_API_KEY",
        "exa": "EXA_API_KEY",
        "you": "YOU_API_KEY",
        "firecrawl": "FIRECRAWL_API_KEY",
    }
    return os.environ.get(key_map.get(provider, ""))


def _validate_searxng_url(url: str) -> str:
    """Validate and sanitize SearXNG instance URL to prevent SSRF.
    
    Enforces http/https scheme and blocks requests to private/internal networks
    including cloud metadata endpoints, loopback, link-local, and RFC1918 ranges.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"SearXNG URL must use http or https scheme, got: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("SearXNG URL must include a hostname")

    hostname = parsed.hostname

    # Block cloud metadata endpoints by hostname
    BLOCKED_HOSTS = {
        "169.254.169.254",        # AWS/GCP/Azure metadata
        "metadata.google.internal",
        "metadata.internal",
    }
    if hostname in BLOCKED_HOSTS:
        raise ValueError(f"SearXNG URL blocked: {hostname} is a cloud metadata endpoint")

    # Resolve hostname and check for private/internal IPs
    # Operators who intentionally self-host on private networks can opt out
    allow_private = os.environ.get("SEARXNG_ALLOW_PRIVATE", "").strip() == "1"
    if not allow_private:
        try:
            resolved_ips = socket.getaddrinfo(hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
            for family, _type, _proto, _canonname, sockaddr in resolved_ips:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
                    raise ValueError(
                        f"SearXNG URL blocked: {hostname} resolves to private/internal IP {ip}. "
                        f"If this is intentional, set SEARXNG_ALLOW_PRIVATE=1 in your environment."
                    )
        except socket.gaierror:
            raise ValueError(f"SearXNG URL blocked: cannot resolve hostname {hostname}")

    return url


def get_searxng_instance_url(config: Dict[str, Any] = None) -> Optional[str]:
    """Get SearXNG instance URL from config or environment.
    
    SearXNG is self-hosted, so no API key needed - just the instance URL.
    Priority: config.json > SEARXNG_INSTANCE_URL environment variable
    
    Security: URL is validated to prevent SSRF via scheme enforcement.
    Both config sources (config.json, env var) are operator-controlled,
    not agent-controlled, so private IPs like localhost are permitted.
    """
    # Check config.json first
    if config:
        searxng_config = config.get("searxng", {})
        if isinstance(searxng_config, dict):
            url = searxng_config.get("instance_url")
            if url:
                return _validate_searxng_url(url)
    
    # Then check environment
    env_url = os.environ.get("SEARXNG_INSTANCE_URL")
    if env_url:
        return _validate_searxng_url(env_url)
    return None


# Backward compatibility alias
def get_env_key(provider: str) -> Optional[str]:
    """Get API key for provider from environment (legacy function)."""
    return get_api_key(provider)


def validate_api_key(provider: str, config: Dict[str, Any] = None) -> str:
    """Validate and return API key (or instance URL for SearXNG), with helpful error messages."""
    key = get_api_key(provider, config)
    
    # Special handling for SearXNG - it needs instance URL, not API key
    if provider == "searxng":
        if not key:
            error_msg = {
                "error": "Missing SearXNG instance URL",
                "env_var": "SEARXNG_INSTANCE_URL",
                "how_to_fix": [
                    "1. Set up your own SearXNG instance: https://docs.searxng.org/admin/installation.html",
                    "2. Add to config.json: \"searxng\": {\"instance_url\": \"https://your-instance.example.com\"}",
                    "3. Or set environment variable: export SEARXNG_INSTANCE_URL=\"https://your-instance.example.com\"",
                    "Note: SearXNG requires a self-hosted instance with JSON format enabled.",
                ],
                "provider": provider
            }
            raise ProviderConfigError(json.dumps(error_msg))

        # Validate URL format
        if not key.startswith(("http://", "https://")):
            raise ProviderConfigError(json.dumps({
                "error": "SearXNG instance URL must start with http:// or https://",
                "provided": key,
                "provider": provider
            }))
        
        return key
    
    if not key:
        env_var = {
            "serper": "SERPER_API_KEY",
            "serpbase": "SERPBASE_API_KEY",
            "brave": "BRAVE_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "querit": "QUERIT_API_KEY",
            "linkup": "LINKUP_API_KEY",
            "exa": "EXA_API_KEY",
            "you": "YOU_API_KEY",
            "perplexity": "PERPLEXITY_API_KEY",
            "kilo-perplexity": "KILOCODE_API_KEY",
            "firecrawl": "FIRECRAWL_API_KEY"
        }[provider]
        
        urls = {
            "serper": "https://serper.dev",
            "serpbase": "https://www.serpbase.dev",
            "brave": "https://brave.com/search/api/",
            "tavily": "https://tavily.com",
            "querit": "https://querit.ai",
            "linkup": "https://app.linkup.so",
            "exa": "https://exa.ai",
            "you": "https://api.you.com",
            "perplexity": "https://www.perplexity.ai/settings/api",
            "kilo-perplexity": "https://api.kilo.ai",
            "firecrawl": "https://www.firecrawl.dev/app/api-keys"
        }
        
        error_msg = {
            "error": f"Missing API key for {provider}",
            "env_var": env_var,
            "how_to_fix": [
                f"1. Get your API key from {urls[provider]}",
                f"2. Add to config.json: \"{provider}\": {{\"api_key\": \"your-key\"}}",
                f"3. Or set environment variable: export {env_var}=\"your-key\"",
            ],
            "provider": provider
        }
        raise ProviderConfigError(json.dumps(error_msg))

    if len(key) < 10:
        raise ProviderConfigError(json.dumps({
            "error": f"API key for {provider} appears invalid (too short)",
            "provider": provider
        }))

    return key


# =============================================================================
# Intelligent Auto-Routing Engine
# =============================================================================

class QueryAnalyzer:
    """
    Intelligent query analysis for smart provider routing.
    
    Uses multi-signal analysis:
    - Intent classification (shopping, research, discovery, local, news)
    - Linguistic patterns (question structure, phrase patterns)
    - Entity detection (products, brands, URLs, dates)
    - Complexity assessment
    """
    
    # Intent signal patterns with weights
    # Higher weight = stronger signal for that provider
    
    SHOPPING_SIGNALS = {
        # Price patterns (very strong)
        r'\bhow much\b': 4.0,
        r'\bprice of\b': 4.0,
        r'\bcost of\b': 4.0,
        r'\bprices?\b': 3.0,
        r'\$\d+|\d+\s*dollars?': 3.0,
        r'€\d+|\d+\s*euros?': 3.0,
        r'£\d+|\d+\s*pounds?': 3.0,
        
        # German price patterns (sehr stark)
        r'\bpreis(e)?\b': 3.5,
        r'\bkosten\b': 3.0,
        r'\bwieviel\b': 3.5,
        r'\bwie viel\b': 3.5,
        r'\bwas kostet\b': 4.0,
        
        # Purchase intent (strong)
        r'\bbuy\b': 3.5,
        r'\bpurchase\b': 3.5,
        r'\border\b(?!\s+by)': 3.0,  # "order" but not "order by"
        r'\bshopping\b': 3.5,
        r'\bshop for\b': 3.5,
        r'\bwhere to (buy|get|purchase)\b': 4.0,
        
        # German purchase intent (stark)
        r'\bkaufen\b': 3.5,
        r'\bbestellen\b': 3.5,
        r'\bwo kaufen\b': 4.0,
        r'\bhändler\b': 3.0,
        r'\bshop\b': 2.5,
        
        # Deal/discount signals
        r'\bdeal(s)?\b': 3.0,
        r'\bdiscount(s)?\b': 3.0,
        r'\bsale\b': 2.5,
        r'\bcheap(er|est)?\b': 3.0,
        r'\baffordable\b': 2.5,
        r'\bbudget\b': 2.5,
        r'\bbest price\b': 3.5,
        r'\bcompare prices\b': 3.5,
        r'\bcoupon\b': 3.0,
        
        # German deal/discount signals
        r'\bgünstig(er|ste)?\b': 3.0,
        r'\bbillig(er|ste)?\b': 3.0,
        r'\bangebot(e)?\b': 3.0,
        r'\brabatt\b': 3.0,
        r'\baktion\b': 2.5,
        r'\bschnäppchen\b': 3.0,
        
        # Product comparison
        r'\bvs\.?\b': 2.0,
        r'\bversus\b': 2.0,
        r'\bor\b.*\bwhich\b': 2.0,
        r'\bspecs?\b': 2.5,
        r'\bspecifications?\b': 2.5,
        r'\breview(s)?\b': 2.0,
        r'\brating(s)?\b': 2.0,
        r'\bunboxing\b': 2.5,
        
        # German product comparison
        r'\btest\b': 2.5,
        r'\bbewertung(en)?\b': 2.5,
        r'\btechnische daten\b': 3.0,
        r'\bspezifikationen\b': 2.5,
    }
    
    RESEARCH_SIGNALS = {
        # Explanation patterns (very strong)
        r'\bhow does\b': 4.0,
        r'\bhow do\b': 3.5,
        r'\bwhy does\b': 4.0,
        r'\bwhy do\b': 3.5,
        r'\bwhy is\b': 3.5,
        r'\bexplain\b': 4.0,
        r'\bexplanation\b': 4.0,
        r'\bwhat is\b': 3.0,
        r'\bwhat are\b': 3.0,
        r'\bdefine\b': 3.5,
        r'\bdefinition of\b': 3.5,
        r'\bmeaning of\b': 3.0,
        
        # Analysis patterns (strong)
        r'\banalyze\b': 3.5,
        r'\banalysis\b': 3.5,
        r'\bcompare\b(?!\s*prices?)': 3.0,  # compare but not "compare prices"
        r'\bcomparison\b': 3.0,
        r'\bstatus of\b': 3.5,
        r'\bstatus\b': 2.5,
        r'\bwhat happened with\b': 4.0,
        r'\bpros and cons\b': 4.0,
        r'\badvantages?\b': 3.0,
        r'\bdisadvantages?\b': 3.0,
        r'\bbenefits?\b': 2.5,
        r'\bdrawbacks?\b': 3.0,
        r'\bdifference between\b': 3.5,
        
        # Learning patterns
        r'\bunderstand\b': 3.0,
        r'\blearn(ing)?\b': 2.5,
        r'\btutorial\b': 3.0,
        r'\bguide\b': 2.5,
        r'\bhow to\b': 2.0,  # Lower weight - could be shopping too
        r'\bstep by step\b': 3.0,
        
        # Depth signals
        r'\bin[- ]depth\b': 3.0,
        r'\bdetailed\b': 2.5,
        r'\bcomprehensive\b': 3.0,
        r'\bthorough\b': 2.5,
        r'\bdeep dive\b': 3.5,
        r'\boverall\b': 2.0,
        r'\bsummary\b': 2.0,
        
        # Academic patterns
        r'\bstudy\b': 2.5,
        r'\bresearch shows\b': 3.5,
        r'\baccording to\b': 2.5,
        r'\bevidence\b': 3.0,
        r'\bscientific\b': 3.0,
        r'\bhistory of\b': 3.0,
        r'\bbackground\b': 2.5,
        r'\bcontext\b': 2.5,
        r'\bimplications?\b': 3.0,
        
        # German explanation patterns (sehr stark)
        r'\bwie funktioniert\b': 4.0,
        r'\bwarum\b': 3.5,
        r'\berklär(en|ung)?\b': 4.0,
        r'\bwas ist\b': 3.0,
        r'\bwas sind\b': 3.0,
        r'\bbedeutung\b': 3.0,
        
        # German analysis patterns
        r'\banalyse\b': 3.5,
        r'\bvergleich(en)?\b': 3.0,
        r'\bvor- und nachteile\b': 4.0,
        r'\bvorteile\b': 3.0,
        r'\bnachteile\b': 3.0,
        r'\bunterschied(e)?\b': 3.5,
        
        # German learning patterns
        r'\bverstehen\b': 3.0,
        r'\blernen\b': 2.5,
        r'\banleitung\b': 3.0,
        r'\bübersicht\b': 2.5,
        r'\bhintergrund\b': 2.5,
        r'\bzusammenfassung\b': 2.5,
    }
    
    DISCOVERY_SIGNALS = {
        # Similarity patterns (very strong)
        r'\bsimilar to\b': 5.0,
        r'\blike\s+\w+\.com': 4.5,  # "like notion.com"
        r'\balternatives? to\b': 5.0,
        r'\bcompetitors? (of|to)\b': 4.5,
        r'\bcompeting with\b': 4.0,
        r'\brivals? (of|to)\b': 4.0,
        r'\binstead of\b': 3.0,
        r'\breplacement for\b': 3.5,
        
        # Company/startup patterns (strong)
        r'\bcompanies (like|that|doing|building)\b': 4.5,
        r'\bstartups? (like|that|doing|building)\b': 4.5,
        r'\bwho else\b': 4.0,
        r'\bother (companies|startups|tools|apps)\b': 3.5,
        r'\bfind (companies|startups|tools|examples?)\b': 4.5,
        r'\bevents? in\b': 4.0,
        r'\bthings to do in\b': 4.5,
        
        # Funding/business patterns
        r'\bseries [a-d]\b': 4.0,
        r'\byc\b|y combinator': 4.0,
        r'\bfund(ed|ing|raise)\b': 3.5,
        r'\bventure\b': 3.0,
        r'\bvaluation\b': 3.0,
        
        # Category patterns
        r'\bresearch papers? (on|about)\b': 4.0,
        r'\barxiv\b': 4.5,
        r'\bgithub (projects?|repos?)\b': 4.5,
        r'\bopen source\b.*\bprojects?\b': 4.0,
        r'\btweets? (about|on)\b': 3.5,
        r'\bblogs? (about|on|like)\b': 3.0,
        
        # URL detection (very strong signal for Exa similar)
        r'https?://[^\s]+': 5.0,
        r'\b\w+\.(com|org|io|ai|co|dev)\b': 3.5,
    }
    
    LOCAL_NEWS_SIGNALS = {
        # Local patterns → Serper
        r'\bnear me\b': 4.0,
        r'\bnearby\b': 3.5,
        r'\blocal\b': 3.0,
        r'\bin (my )?(city|area|town|neighborhood)\b': 3.5,
        r'\brestaurants?\b': 2.5,
        r'\bhotels?\b': 2.5,
        r'\bcafes?\b': 2.5,
        r'\bstores?\b': 2.0,
        r'\bdirections? to\b': 3.5,
        r'\bmap of\b': 3.0,
        r'\bphone number\b': 3.0,
        r'\baddress of\b': 3.0,
        r'\bopen(ing)? hours\b': 3.0,
        
        # Weather/time
        r'\bweather\b': 4.0,
        r'\bforecast\b': 3.5,
        r'\btemperature\b': 3.0,
        r'\btime in\b': 3.0,
        
        # News/recency patterns → Serper (or Tavily for news depth)
        r'\blatest\b': 2.5,
        r'\brecent\b': 2.5,
        r'\btoday\b': 2.5,
        r'\bbreaking\b': 3.5,
        r'\bnews\b': 2.5,
        r'\bheadlines?\b': 3.0,
        r'\b202[4-9]\b': 2.0,  # Current year mentions
        r'\blast (week|month|year)\b': 2.0,

        # German local patterns
        r'\bin der nähe\b': 4.0,
        r'\bin meiner nähe\b': 4.0,
        r'\böffnungszeiten\b': 3.0,
        r'\badresse von\b': 3.0,
        r'\bweg(beschreibung)? nach\b': 3.5,

        # German news/recency patterns
        r'\bheute\b': 2.5,
        r'\bmorgen\b': 2.0,
        r'\baktuell\b': 2.5,
        r'\bnachrichten\b': 3.0,
    }
    
    # Source-grounded/RAG retrieval signals → Linkup
    # Linkup is strongest when the user wants source-backed evidence for LLM grounding.
    LINKUP_SOURCE_SIGNALS = {
        r'\bcitations?\b': 5.0,
        r'\bsources?\b': 4.5,
        r'\bsource.?backed\b': 5.0,
        r'\bwith sources\b': 5.0,
        r'\bwith references\b': 5.0,
        r'\breferences?\b': 4.5,
        r'\bevidence\b': 4.5,
        r'\bcredible sources?\b': 5.5,
        r'\bprimary sources?\b': 5.0,
        r'\bsupporting links?\b': 4.5,
        r'\bverify (this|the)?\b': 4.5,
        r'\bfact.?check\b': 5.0,
        r'\bground(ed|ing)?\b': 4.5,
        r'\bground this\b': 5.0,
        r'\bclaim\b': 2.5,
        r'\bfind (credible )?sources?\b': 5.5,
        r'\bfind pages? that support\b': 5.0,
        r'\bwhere did this come from\b': 5.0,
        r'\bsource material\b': 4.0,
    }

    # RAG/AI signals → You.com
    # You.com excels at providing LLM-ready snippets and combined web+news
    RAG_SIGNALS = {
        # RAG/context patterns (strong signal for You.com)
        r'\brag\b': 4.5,
        r'\bcontext for\b': 4.0,
        r'\bsummarize\b': 3.5,
        r'\bbrief(ly)?\b': 3.0,
        r'\bquick overview\b': 3.5,
        r'\btl;?dr\b': 4.0,
        r'\bkey (points|facts|info)\b': 3.5,
        r'\bmain (points|takeaways)\b': 3.5,
        
        # Combined web + news queries
        r'\b(web|online)\s+and\s+news\b': 4.0,
        r'\ball sources\b': 3.5,
        r'\bcomprehensive (search|overview)\b': 3.5,
        r'\blatest\s+(news|updates)\b': 3.0,
        r'\bcurrent (events|situation|status)\b': 3.5,
        
        # Real-time information needs
        r'\bright now\b': 3.0,
        r'\bas of today\b': 3.5,
        r'\bup.to.date\b': 3.5,
        r'\breal.time\b': 4.0,
        r'\blive\b': 2.5,
        
        # Information synthesis
        r'\bwhat\'?s happening with\b': 3.5,
        r'\bwhat\'?s the latest\b': 4.0,
        r'\bupdates?\s+on\b': 3.5,
        r'\bstatus of\b': 3.0,
        r'\bsituation (in|with|around)\b': 3.5,
    }
    
    # Direct answer / synthesis signals → Perplexity via Kilo Gateway
    DIRECT_ANSWER_SIGNALS = {
        r'\bwhat is\b': 3.0,
        r'\bwhat are\b': 2.5,
        r'\bcurrent status\b': 4.0,
        r'\bstatus of\b': 3.5,
        r'\bstatus\b': 2.5,
        r'\bwhat happened with\b': 4.0,
        r"\bwhat'?s happening with\b": 4.0,
        r'\bas of (today|now)\b': 4.0,
        r'\bthis weekend\b': 3.5,
        r'\bevents? in\b': 3.5,
        r'\bthings to do in\b': 4.0,
        r'\bnear me\b': 3.0,
        r'\bcan you (tell me|summarize|explain)\b': 3.5,
        # German
        r'\bwann\b': 3.0,
        r'\bwer\b': 3.0,
        r'\bwo\b': 2.5,
        r'\bwie viele\b': 3.0,
    }

    # Privacy/Multi-source signals → SearXNG (self-hosted meta-search)
    # SearXNG is ideal for privacy-focused queries and aggregating multiple sources
    PRIVACY_SIGNALS = {
        # Privacy signals (very strong)
        r'\bprivate(ly)?\b': 4.0,
        r'\banonymous(ly)?\b': 4.0,
        r'\bwithout tracking\b': 4.5,
        r'\bno track(ing)?\b': 4.5,
        r'\bprivacy\b': 3.5,
        r'\bprivacy.?focused\b': 4.5,
        r'\bprivacy.?first\b': 4.5,
        r'\bduckduckgo alternative\b': 4.5,
        r'\bprivate search\b': 5.0,
        
        # German privacy signals
        r'\bprivat\b': 4.0,
        r'\banonym\b': 4.0,
        r'\bohne tracking\b': 4.5,
        r'\bdatenschutz\b': 4.0,
        
        # Multi-source aggregation signals
        r'\baggregate results?\b': 4.0,
        r'\bmultiple sources?\b': 4.0,
        r'\bdiverse (results|perspectives|sources)\b': 4.0,
        r'\bfrom (all|multiple|different) (engines?|sources?)\b': 4.5,
        r'\bmeta.?search\b': 5.0,
        r'\ball engines?\b': 4.0,
        
        # German multi-source signals
        r'\bverschiedene quellen\b': 4.0,
        r'\baus mehreren quellen\b': 4.0,
        r'\balle suchmaschinen\b': 4.5,
        
        # Budget/free signals (SearXNG is self-hosted = $0 API cost)
        r'\bfree search\b': 3.5,
        r'\bno api cost\b': 4.0,
        r'\bself.?hosted search\b': 5.0,
        r'\bzero cost\b': 3.5,
        r'\bbudget\b(?!\s*(laptop|phone|option))\b': 2.5,  # "budget" alone, not "budget laptop"
        
        # German budget signals
        r'\bkostenlos(e)?\s+suche\b': 3.5,
        r'\bkeine api.?kosten\b': 4.0,
    }

    # Exa Deep Search signals → deep multi-source synthesis
    EXA_DEEP_SIGNALS = {
        r'\bsynthesi[sz]e\b': 5.0,
        r'\bdeep research\b': 5.0,
        r'\bcomprehensive (analysis|report|overview|survey)\b': 4.5,
        r'\bacross (multiple|many|several) (sources|documents|papers)\b': 4.5,
        r'\baggregat(e|ing) (information|data|results)\b': 4.0,
        r'\bcross.?referenc': 4.5,
        r'\bsec filings?\b': 4.5,
        r'\bannual reports?\b': 4.0,
        r'\bearnings (call|report|transcript)\b': 4.5,
        r'\bfinancial analysis\b': 4.0,
        r'\bliterature (review|survey)\b': 5.0,
        r'\bacademic literature\b': 4.5,
        r'\bstate of the (art|field|industry)\b': 4.0,
        r'\bcompile (a |the )?(report|findings|results)\b': 4.5,
        r'\bsummariz(e|ing) (research|papers|studies)\b': 4.0,
        r'\bmultiple documents?\b': 4.0,
        r'\bdossier\b': 4.5,
        r'\bdue diligence\b': 4.5,
        r'\bstructured (output|data|report)\b': 4.0,
        r'\bmarket research\b': 4.0,
        r'\bindustry (report|analysis|overview)\b': 4.0,
        r'\bresearch (on|about|into)\b': 4.0,
        r'\bwhitepaper\b': 4.5,
        r'\btechnical report\b': 4.0,
        r'\bsurvey of\b': 4.5,
        r'\bmeta.?analysis\b': 5.0,
        r'\bsystematic review\b': 5.0,
        r'\bcase study\b': 3.5,
        r'\bbenchmark(s|ing)?\b': 3.5,
        # German
        r'\btiefenrecherche\b': 5.0,
        r'\bumfassende (analyse|übersicht|recherche)\b': 4.5,
        r'\baus mehreren quellen zusammenfassen\b': 4.5,
        r'\bmarktforschung\b': 4.0,
    }

    # Exa Deep Reasoning signals → complex cross-reference analysis
    EXA_DEEP_REASONING_SIGNALS = {
        r'\bdeep.?reasoning\b': 6.0,
        r'\bcomplex (analysis|reasoning|research)\b': 4.5,
        r'\bcontradictions?\b': 4.5,
        r'\breconcil(e|ing)\b': 5.0,
        r'\bcritical(ly)? analyz': 4.5,
        r'\bweigh(ing)? (the )?evidence\b': 4.5,
        r'\bcompeting (claims|theories|perspectives)\b': 4.5,
        r'\bcomplex financial\b': 4.5,
        r'\bregulatory (analysis|compliance|landscape)\b': 4.5,
        r'\blegal analysis\b': 4.5,
        r'\bcomprehensive (due diligence|investigation)\b': 5.0,
        r'\bpatent (landscape|analysis|search)\b': 4.5,
        r'\bmarket intelligence\b': 4.5,
        r'\bcompetitive (intelligence|landscape)\b': 4.5,
        r'\btrade.?offs?\b': 4.0,
        r'\bpros and cons of\b': 4.0,
        r'\bshould I (use|choose|pick)\b': 3.5,
        r'\bwhich is better\b': 4.0,
        # German
        r'\bkomplexe analyse\b': 4.5,
        r'\bwidersprüche\b': 4.5,
        r'\bquellen abwägen\b': 4.5,
        r'\brechtliche analyse\b': 4.5,
        r'\bvergleich(e|en)?\b': 3.5,
    }


    # Brand/product patterns for shopping detection
    BRAND_PATTERNS = [
        # Tech brands
        r'\b(apple|iphone|ipad|macbook|airpods?)\b',
        r'\b(samsung|galaxy)\b',
        r'\b(google|pixel)\b',
        r'\b(microsoft|surface|xbox)\b',
        r'\b(sony|playstation)\b',
        r'\b(nvidia|geforce|rtx)\b',
        r'\b(amd|ryzen|radeon)\b',
        r'\b(intel|core i[3579])\b',
        r'\b(dell|hp|lenovo|asus|acer)\b',
        r'\b(lg|tcl|hisense)\b',
        
        # Product categories
        r'\b(laptop|phone|tablet|tv|monitor|headphones?|earbuds?)\b',
        r'\b(camera|lens|drone)\b',
        r'\b(watch|smartwatch|fitbit|garmin)\b',
        r'\b(router|modem|wifi)\b',
        r'\b(keyboard|mouse|gaming)\b',
    ]
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.auto_config = config.get("auto_routing", DEFAULT_CONFIG["auto_routing"])
    
    def _calculate_signal_score(
        self, 
        query: str, 
        signals: Dict[str, float]
    ) -> Tuple[float, List[Dict[str, Any]]]:
        """
        Calculate score for a signal category.
        Returns (total_score, list of matched signals with details).
        """
        query_lower = query.lower()
        matches = []
        total_score = 0.0
        
        for pattern, weight in signals.items():
            regex = re.compile(pattern, re.IGNORECASE)
            found = regex.findall(query_lower)
            if found:
                # Normalize found matches
                match_text = found[0] if isinstance(found[0], str) else found[0][0] if found[0] else pattern
                matches.append({
                    "pattern": pattern,
                    "matched": match_text,
                    "weight": weight
                })
                total_score += weight
        
        return total_score, matches
    
    def _detect_product_brand_combo(self, query: str) -> float:
        """
        Detect product + brand combinations which strongly indicate shopping intent.
        Returns a bonus score.
        """
        query_lower = query.lower()
        brand_found = False
        product_found = False
        
        for pattern in self.BRAND_PATTERNS:
            if re.search(pattern, query_lower, re.IGNORECASE):
                brand_found = True
                break
        
        # Check for product indicators
        product_indicators = [
            r'\b(buy|price|specs?|review|vs|compare)\b',
            r'\b(pro|max|plus|mini|ultra|lite)\b',  # Product tier names
            r'\b\d+\s*(gb|tb|inch|mm|hz)\b',  # Specifications
        ]
        for pattern in product_indicators:
            if re.search(pattern, query_lower, re.IGNORECASE):
                product_found = True
                break
        
        if brand_found and product_found:
            return 3.0  # Strong shopping signal
        elif brand_found:
            return 1.5  # Moderate shopping signal
        return 0.0
    
    def _detect_url(self, query: str) -> Optional[str]:
        """Detect URLs in query - strong signal for Exa similar search."""
        url_pattern = r'https?://[^\s]+'
        match = re.search(url_pattern, query)
        if match:
            return match.group()
        
        # Also check for domain-like patterns
        domain_pattern = r'\b(\w+\.(com|org|io|ai|co|dev|net|app))\b'
        match = re.search(domain_pattern, query, re.IGNORECASE)
        if match:
            return match.group()
        
        return None
    
    def _assess_query_complexity(self, query: str) -> Dict[str, Any]:
        """
        Assess query complexity - complex queries favor Tavily.
        """
        words = query.split()
        word_count = len(words)
        
        # Count question words
        question_words = len(re.findall(
            r'\b(what|why|how|when|where|which|who|whose|whom)\b', 
            query, re.IGNORECASE
        ))
        
        # Check for multiple clauses
        clause_markers = len(re.findall(
            r'\b(and|but|or|because|since|while|although|if|when)\b',
            query, re.IGNORECASE
        ))
        
        complexity_score = 0.0
        if word_count > 10:
            complexity_score += 1.5
        if word_count > 20:
            complexity_score += 1.0
        if question_words > 1:
            complexity_score += 1.0
        if clause_markers > 0:
            complexity_score += 0.5 * clause_markers
        
        return {
            "word_count": word_count,
            "question_words": question_words,
            "clause_markers": clause_markers,
            "complexity_score": complexity_score,
            "is_complex": complexity_score > 2.0
        }
    
    def _detect_recency_intent(self, query: str) -> Tuple[bool, float]:
        """
        Detect if query wants recent/timely information.
        Returns (is_recency_focused, score).
        """
        recency_patterns = [
            (r'\b(latest|newest|recent|current)\b', 2.5),
            (r'\b(today|yesterday|this week|this month)\b', 3.0),
            (r'\b(202[4-9]|2030)\b', 2.0),
            (r'\b(breaking|live|just|now)\b', 3.0),
            (r'\blast (hour|day|week|month)\b', 2.5),
            # Common non-English freshness markers from the 25-query routing benchmark.
            (r'\b(hoy|aujourd|heute|aktuell)\b', 2.5),
            (r'[今日最新]', 2.5),
            (r'(сегодня|новости)', 2.5),
            (r'(اليوم|أخبار)', 2.5),
            (r'(最新|今天)', 2.5),
        ]
        
        total = 0.0
        for pattern, weight in recency_patterns:
            if re.search(pattern, query, re.IGNORECASE):
                total += weight
        
        return total > 2.0, total

    def _detect_language_hint(self, query: str) -> str:
        """Best-effort language/script hint for routing; not user-facing translation."""
        q = query.lower()
        if re.search(r'[\u0600-\u06ff]', query):
            return "ar"
        if re.search(r'[\u0400-\u04ff]', query):
            return "ru"
        if re.search(r'[\u3040-\u30ff]', query) or re.search(r'(東京|ニュース|今日|企業|発表)', query):
            return "ja"
        if re.search(r'[\u4e00-\u9fff]', query):
            return "zh"
        if re.search(r'\b(noticias|españa|hoy|regulación|inteligencia artificial)\b', q):
            return "es"
        if re.search(r'\b(actualités|france|aujourd|ouverts?|dimanche|récents?|avis)\b', q):
            return "fr"
        if re.search(r'\b(der|die|das|und|oder|nicht|ist|sind|aktuelle?n?|preis|kaufen|öffnungszeiten|österreich)\b', q):
            return "de"
        return "en"

    def _detect_routing_class(self, query: str, language_hint: str) -> str:
        """Coarse class labels from the qualitative 25-query benchmark."""
        q = query.lower()
        # Synthesis/briefing queries should prefer broad real-time/search providers,
        # but Web Search Plus no longer exposes a separate answer tool.
        if re.search(r'\b(difference|differences|unterschiede|vergleich|compare|comparison|brief(?:ing)?|summari[sz]e|zusammenfass(?:en|ung)|was sind|what are)\b', q):
            return "briefing_synthesis"
        if re.search(r'\b(advisory|security advisory|cve|mitigation|openssl|openssh|vulnerability|zero[-\s]?day)\b', q):
            return "security_advisory"
        if re.search(r'\b(patent|patents|patentscope|espacenet|uspto|google patents)\b', q):
            return "patents"
        if re.search(r'\b(pdf|whitepaper|code of practice)\b', q) and re.search(r'\b(nist|eu ai act|regulation|regulatory|policy|commission|government|official|rmf)\b', q):
            return "policy_pdf"
        if re.search(r'\b(eu ai act|european commission|regulation|regulatory|obligations?)\b', q):
            return "official_regulatory"
        if re.search(r'\b(nvidia|earnings|gross margin|investor relations|guidance|10-[qk]|eps|revenue|quarterly results?)\b', q):
            return "finance_earnings_official"
        if re.search(r'\b(investor monthly|monthly factsheet|monthly report|aum|fund flows?)\b', q):
            return "finance_investor_monthly"
        if re.search(r'\bsite:\s*reddit\.com\b|\br/\w+|\breddit\s+(thread|post|community|users?|discussion|comments?)\b', q):
            return "reddit_community"
        if (
            re.search(r'\b(geizhals|preis|prices?|buy|kaufen|österreich|austria|shop|händler|deal|angebot)\b', q)
            and re.search(r'\b(sony|denon|iphone|samsung|bose|kef|marantz|yamaha|lg|asus|laptop|tv|headphones?|speaker|receiver|avc|wh-|[a-z]{1,5}[-\s]?\d{3,}[a-z0-9-]*)\b', q)
        ):
            if re.search(r'\b(review(s)?|test|tests?|vergleich|best|beste|under|unter|erfahrungen)\b', q):
                return "shopping_reviews_local"
            return "shopping_specs"
        if re.search(r'\b(forum|forums|community|discussion|comments?|erfahrungen|review(s)?|measurements?|head-fi|audiosciencereview|hifi-forum)\b', q):
            return "community_forum"
        if re.search(r'\barxiv\b|\bpaper(s)?\b|\bscaling laws\b|\brandomi[sz]ed trial\b|\bprimary sources?\b', q):
            return "academic_arxiv"
        if re.search(r'\b(github\b|repo(sitory)?\b|plugin docs\b)', q):
            return "github_docs"
        if re.search(r'\b(official docs?|official documentation|api reference|developer docs?|official manual)\b', q):
            return "official_docs"
        if re.search(r'\b(official|release|announcement|launch|changelog|release notes?)\b', q) and re.search(r'\b(mistral|anthropic|openai|google|meta|nvidia|apple|microsoft|claude|gemini|llama)\b', q):
            return "official_vendor_release"
        if re.search(r'\b(python|pydantic|node\.js|api docs?|documentation|docs|changelog|release notes?|taskgroup|basemodel)\b', q):
            return "docs_api"
        if re.search(r'\b(official|regulatory filing|public authority)\b', q):
            return "official_regulatory"
        if re.search(r'\b(graz|öffnungszeiten|adresse|restaurants?|vegan|hifi team)\b', q):
            return "local_at"
        if re.search(r'\b(bundesliga|standings?|fixtures?|tabelle|punkte|spieltag|matchday|lineups?|scores?|sturm|salzburg|lask)\b|\b(league|liga|standings?|points?)\s+table\b', q):
            return "sports_current"
        if re.search(r'\b(wetter|weather|forecast|regen|rain)\b', q):
            return "weather_local"
        if re.search(r'\b(alternatives? to|open source|self hosted|competitors?|similar to)\b', q):
            return "oss_discovery"
        if language_hint not in {"en", "de"}:
            return "multilingual_current"
        return "general"

    def _apply_vnext_routing_boosts(
        self,
        query: str,
        provider_scores: Dict[str, float],
        language_hint: str,
        routing_class: str,
        recency_score: float,
    ) -> None:
        """Apply conservative class-aware boosts from the qualitative routing benchmark.

        Search auto-routing still avoids slow answer-only providers unless explicitly selected.
        """
        def boost(provider: str, value: float) -> None:
            provider_scores[provider] = provider_scores.get(provider, 0.0) + value

        def boost_many(items: List[Tuple[str, float]]) -> None:
            for provider, value in items:
                boost(provider, value)

        # Script/language-aware current queries: You performed best as the safe fast default,
        # with Exa/Firecrawl/Linkup useful by script. Keep this modest so strong class rules win.
        if language_hint not in {"en", "de"}:
            if language_hint == "zh":
                boost_many([("exa", 7.0), ("you", 6.0), ("firecrawl", 4.0), ("linkup", 3.0), ("serper", 2.5)])
            elif language_hint == "ar":
                boost_many([("you", 8.0), ("linkup", 5.0), ("serper", 4.0), ("firecrawl", 2.0)])
            else:
                boost_many([("you", 8.0), ("exa", 5.0), ("firecrawl", 4.0), ("linkup", 3.0), ("tavily", 2.0)])
            boost("you", min(recency_score, 3.0))

        if routing_class == "shopping_at":
            boost_many([("serper", 8.0), ("firecrawl", 6.0), ("linkup", 4.0), ("you", 2.0), ("exa", -2.0)])
        elif routing_class == "local_at":
            boost_many([("firecrawl", 8.0), ("serper", 6.0), ("linkup", 4.0), ("you", 2.0)])
        elif routing_class == "official_vendor_release":
            boost_many([("you", 14.0), ("linkup", 10.0), ("exa", 7.0), ("serper", 4.0), ("firecrawl", 3.0)])
        elif routing_class == "official_docs":
            boost_many([("exa", 12.0), ("you", 7.0), ("firecrawl", 5.0), ("serper", 3.0), ("tavily", 2.0)])
        elif routing_class == "policy_pdf":
            boost_many([("linkup", 10.0), ("exa", 8.0), ("serper", 7.0), ("firecrawl", 6.0), ("you", 4.0)])
        elif routing_class == "official_regulatory":
            boost_many([("exa", 8.0), ("firecrawl", 6.0), ("serper", 5.0), ("you", 3.0)])
        elif routing_class == "sports_current":
            boost_many([("you", 8.0), ("serper", 6.0), ("linkup", 5.0), ("tavily", 2.0)])
        elif routing_class == "github_docs":
            boost_many([("exa", 10.0), ("you", 6.0), ("firecrawl", 5.0), ("serper", 4.0)])
        elif routing_class == "docs_api":
            boost_many([("serper", 6.0), ("exa", 5.0), ("you", 4.0), ("firecrawl", 3.0), ("tavily", 3.0)])
        elif routing_class == "academic_arxiv":
            boost_many([("exa", 12.0), ("serper", 3.0), ("linkup", 2.0), ("you", 1.5)])
        elif routing_class == "oss_discovery":
            boost_many([("exa", 8.0), ("firecrawl", 5.0), ("tavily", 4.0), ("you", 3.0)])
        elif routing_class == "reddit_community":
            boost_many([("serper", 10.0), ("firecrawl", 8.0), ("tavily", 6.0), ("exa", -20.0)])
        elif routing_class == "security_advisory":
            boost_many([("serper", 10.0), ("exa", 8.0), ("linkup", 5.0), ("you", 2.0), ("firecrawl", -20.0)])
        elif routing_class == "finance_earnings_official":
            boost_many([("linkup", 14.0), ("you", 9.0), ("exa", 7.0), ("serper", 6.0), ("firecrawl", 4.0)])
        elif routing_class == "finance_investor_monthly":
            boost_many([("linkup", 12.0), ("serper", 7.0), ("you", 5.0), ("exa", 4.0)])
        elif routing_class == "community_forum":
            boost_many([("firecrawl", 10.0), ("serper", 8.0), ("tavily", 5.0), ("you", 4.0), ("exa", -18.0)])
        elif routing_class == "shopping_specs":
            boost_many([("serper", 9.0), ("firecrawl", 6.0), ("linkup", 4.0), ("you", 2.0), ("exa", -2.0)])
        elif routing_class == "shopping_reviews_local":
            boost_many([("serper", 9.0), ("firecrawl", 7.0), ("you", 4.0), ("tavily", 3.0), ("exa", -4.0)])
        elif routing_class == "patents":
            boost_many([("exa", 10.0), ("serper", 7.0), ("linkup", 4.0), ("you", 3.0)])
        elif routing_class == "weather_local":
            boost_many([("serper", 8.0), ("firecrawl", 6.0), ("you", 2.0)])
        elif routing_class == "briefing_synthesis":
            boost_many([("you", 16.0), ("tavily", 4.0), ("linkup", 3.0), ("exa", 2.0)])
    
    def analyze(self, query: str) -> Dict[str, Any]:
        """
        Perform comprehensive query analysis.
        Returns detailed analysis with scores for each provider.
        """
        # Calculate scores for each intent category
        shopping_score, shopping_matches = self._calculate_signal_score(
            query, self.SHOPPING_SIGNALS
        )
        research_score, research_matches = self._calculate_signal_score(
            query, self.RESEARCH_SIGNALS
        )
        discovery_score, discovery_matches = self._calculate_signal_score(
            query, self.DISCOVERY_SIGNALS
        )
        local_news_score, local_news_matches = self._calculate_signal_score(
            query, self.LOCAL_NEWS_SIGNALS
        )
        rag_score, rag_matches = self._calculate_signal_score(
            query, self.RAG_SIGNALS
        )
        privacy_score, privacy_matches = self._calculate_signal_score(
            query, self.PRIVACY_SIGNALS
        )
        linkup_source_score, linkup_source_matches = self._calculate_signal_score(
            query, self.LINKUP_SOURCE_SIGNALS
        )
        direct_answer_score, direct_answer_matches = self._calculate_signal_score(
            query, self.DIRECT_ANSWER_SIGNALS
        )
        exa_deep_score, exa_deep_matches = self._calculate_signal_score(
            query, self.EXA_DEEP_SIGNALS
        )
        exa_deep_reasoning_score, exa_deep_reasoning_matches = self._calculate_signal_score(
            query, self.EXA_DEEP_REASONING_SIGNALS
        )

        # Apply product/brand bonus to shopping
        brand_bonus = self._detect_product_brand_combo(query)
        if brand_bonus > 0:
            shopping_score += brand_bonus
            shopping_matches.append({
                "pattern": "product_brand_combo",
                "matched": "brand + product detected",
                "weight": brand_bonus
            })
        
        # Detect URL → strong Exa signal
        detected_url = self._detect_url(query)
        if detected_url:
            discovery_score += 5.0
            discovery_matches.append({
                "pattern": "url_detected",
                "matched": detected_url,
                "weight": 5.0
            })
        
        # Assess complexity → favors Tavily
        complexity = self._assess_query_complexity(query)
        if complexity["is_complex"]:
            research_score += complexity["complexity_score"]
            research_matches.append({
                "pattern": "query_complexity",
                "matched": f"complex query ({complexity['word_count']} words)",
                "weight": complexity["complexity_score"]
            })
        
        # Check recency intent and benchmark-derived language/class hints
        is_recency, recency_score = self._detect_recency_intent(query)
        language_hint = self._detect_language_hint(query)
        routing_class = self._detect_routing_class(query, language_hint)
        
        # Map intents to providers with final scores
        provider_scores = {
            "serper": shopping_score + local_news_score + (recency_score * 0.35),
            "serpbase": (shopping_score * 0.8) + (local_news_score * 0.8) + (recency_score * 0.25),
            "brave": shopping_score + local_news_score + (recency_score * 0.35),
            "tavily": research_score + (complexity["complexity_score"] if not complexity["is_complex"] else 0) + (0.2 * recency_score),
            "querit": (research_score * 0.65) + (rag_score * 0.35) + (recency_score * 0.45),
            "linkup": linkup_source_score + (rag_score * 0.7) + (research_score * 0.45) + (recency_score * 0.35),
            "exa": discovery_score + (1.0 if re.search(r"\b(similar|alternatives?|examples?)\b", query, re.IGNORECASE) else 0.0) + (exa_deep_score * 0.5) + (exa_deep_reasoning_score * 0.5),
            "perplexity": direct_answer_score + (local_news_score * 0.4) + (recency_score * 0.55),
            "kilo-perplexity": direct_answer_score + (local_news_score * 0.4) + (recency_score * 0.55),
            "you": rag_score + (recency_score * 0.25),  # You.com good for real-time + RAG
            "searxng": privacy_score,  # SearXNG for privacy/multi-source queries
            "firecrawl": discovery_score + (research_score * 0.35) + (recency_score * 0.25),
        }
        self._apply_vnext_routing_boosts(
            query,
            provider_scores,
            language_hint,
            routing_class,
            recency_score,
        )
        
        # Build match details per provider
        provider_matches = {
            "serper": shopping_matches + local_news_matches,
            "serpbase": shopping_matches + local_news_matches,
            "brave": shopping_matches + local_news_matches,
            "tavily": research_matches,
            "querit": research_matches,
            "linkup": linkup_source_matches + rag_matches + research_matches,
            "exa": discovery_matches + exa_deep_matches + exa_deep_reasoning_matches,
            "perplexity": direct_answer_matches,
            "kilo-perplexity": direct_answer_matches,
            "you": rag_matches,
            "searxng": privacy_matches,
            "firecrawl": discovery_matches + research_matches,
        }
        
        return {
            "query": query,
            "provider_scores": provider_scores,
            "provider_matches": provider_matches,
            "detected_url": detected_url,
            "complexity": complexity,
            "recency_focused": is_recency,
            "recency_score": recency_score,
            "language_hint": language_hint,
            "routing_class": routing_class,
            "linkup_source_score": linkup_source_score,
            "exa_deep_score": exa_deep_score,
            "exa_deep_reasoning_score": exa_deep_reasoning_score,
        }
    
    def route(self, query: str) -> Dict[str, Any]:
        """
        Route query to optimal provider with confidence scoring.
        """
        analysis = self.analyze(query)
        scores = analysis["provider_scores"]
        
        # Filter to available providers
        disabled = set(self.auto_config.get("disabled_providers", []))
        # Filter to configured providers that are eligible for automatic routing.
        # Providers with auto_allow=false remain available for explicit calls.
        auto_excluded = [
            p for p in scores
            if get_api_key(p, self.config) and p not in disabled and not _provider_auto_allowed(p, self.auto_config)
        ]
        available = {
            p: s for p, s in scores.items()
            if p not in disabled and _provider_auto_allowed(p, self.auto_config) and get_api_key(p, self.config)
        }
        
        if not available:
            # No providers available, use fallback
            fallback = self.auto_config.get("fallback_provider", "serper")
            return {
                "provider": fallback,
                "confidence": 0.0,
                "confidence_level": "low",
                "reason": "no_available_providers",
                "routing_policy": ROUTING_POLICY,
                "scores": scores,
                "top_signals": [],
                "analysis": analysis,
                "auto_allow_excluded": auto_excluded,
            }
        
        # Find the winner
        max_score = max(available.values())
        
        # Handle ties using deterministic per-query distribution
        priority = self.auto_config.get("provider_priority", ["you", "serper", "exa", "firecrawl", "tavily", "linkup", "brave", "serpbase", "querit", "kilo-perplexity", "perplexity", "searxng"])
        winners = [p for p, s in available.items() if s == max_score]
        
        if len(winners) > 1:
            winner = _choose_tie_winner(query, winners, priority)
        else:
            winner = winners[0]
        
        # Calculate confidence
        # High confidence = clear winner with good margin
        if max_score == 0:
            confidence = 0.0
            reason = "no_signals_matched"
        else:
            # Confidence based on:
            # 1. Absolute score (is it strong enough?)
            # 2. Relative margin (is there a clear winner?)
            second_best = sorted(available.values(), reverse=True)[1] if len(available) > 1 else 0
            margin = (max_score - second_best) / max_score if max_score > 0 else 0
            
            # Normalize score to 0-1 range (assuming max reasonable score ~15)
            normalized_score = min(max_score / 15.0, 1.0)
            
            # Confidence is combination of absolute strength and relative margin
            confidence = round((normalized_score * 0.6 + margin * 0.4), 3)
            
            if confidence >= 0.7:
                reason = "high_confidence_match"
            elif confidence >= 0.4:
                reason = "moderate_confidence_match"
            else:
                reason = "low_confidence_match"
        
        # Get top signals for the winning provider
        matches = analysis["provider_matches"].get(winner, [])
        top_signals = sorted(matches, key=lambda x: x["weight"], reverse=True)[:5]
        
        # Special case: URL detected and Exa available → strong recommendation
        if analysis["detected_url"] and "exa" in available:
            if winner != "exa":
                # Override if URL is present but didn't win
                # (user might want similar search)
                pass  # Keep current winner but note it
        
        # Determine Exa search depth when routed to Exa
        exa_depth = "normal"
        if winner == "exa":
            deep_r_score = analysis.get("exa_deep_reasoning_score", 0)
            deep_score = analysis.get("exa_deep_score", 0)
            if deep_r_score >= 4.0:
                exa_depth = "deep-reasoning"
            elif deep_score >= 4.0:
                exa_depth = "deep"

        # Build detailed routing result
        threshold = self.auto_config.get("confidence_threshold", 0.3)

        return {
            "provider": winner,
            "confidence": confidence,
            "confidence_level": "high" if confidence >= 0.7 else "medium" if confidence >= 0.4 else "low",
            "reason": reason,
            "routing_policy": ROUTING_POLICY,
            "exa_depth": exa_depth,
            "scores": {p: round(s, 2) for p, s in available.items()},
            "winning_score": round(max_score, 2),
            "top_signals": [
                {"matched": s["matched"], "weight": s["weight"]}
                for s in top_signals
            ],
            "below_threshold": confidence < threshold,
            "auto_allow_excluded": auto_excluded,
            "analysis_summary": {
                "query_length": len(query.split()),
                "is_complex": analysis["complexity"]["is_complex"],
                "has_url": analysis["detected_url"] is not None,
                "recency_focused": analysis["recency_focused"],
                "language_hint": analysis.get("language_hint", "en"),
                "routing_class": analysis.get("routing_class", "general"),
            }
        }


def auto_route_provider(query: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Intelligently route query to the best provider.
    Returns detailed routing decision with confidence.
    """
    auto_config = config.get("auto_routing", DEFAULT_CONFIG["auto_routing"])
    if auto_config.get("enabled", True) is False:
        default_provider = config.get("default_provider")
        if default_provider:
            return {
                "provider": default_provider,
                "confidence": 1.0,
                "confidence_level": "high",
                "reason": "auto_routing_disabled_default_provider",
                "scores": {default_provider: 1.0},
                "top_signals": [],
                "auto_routed": False,
            }
        return {
            "provider": None,
            "confidence": 0.0,
            "confidence_level": "low",
            "reason": "auto_routing_disabled_no_default_provider",
            "scores": {},
            "top_signals": [],
            "auto_routed": False,
        }
    analyzer = QueryAnalyzer(config)
    return analyzer.route(query)


def explain_routing(query: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Provide detailed explanation of routing decision for debugging.
    """
    analyzer = QueryAnalyzer(config)
    analysis = analyzer.analyze(query)
    routing = analyzer.route(query)
    
    return {
        "query": query,
        "routing_decision": {
            "provider": routing["provider"],
            "confidence": routing["confidence"],
            "confidence_level": routing["confidence_level"],
            "reason": routing["reason"],
            "routing_policy": routing.get("routing_policy", ROUTING_POLICY),
            "exa_depth": routing.get("exa_depth", "normal"),
            "auto_allow_excluded": routing.get("auto_allow_excluded", []),
        },
        "scores": routing["scores"],
        "top_signals": routing["top_signals"],
        "intent_breakdown": {
            "shopping_signals": len(analysis["provider_matches"]["serper"]),
            "serpbase_signals": len(analysis["provider_matches"].get("serpbase", [])),
            "brave_signals": len(analysis["provider_matches"]["brave"]),
            "research_signals": len(analysis["provider_matches"]["tavily"]),
            "querit_signals": len(analysis["provider_matches"]["querit"]),
            "linkup_signals": len(analysis["provider_matches"].get("linkup", [])),
            "linkup_source_score": round(analysis.get("linkup_source_score", 0), 2),
            "discovery_signals": len(analysis["provider_matches"]["exa"]),
            "rag_signals": len(analysis["provider_matches"]["you"]),
            "exa_deep_score": round(analysis.get("exa_deep_score", 0), 2),
            "exa_deep_reasoning_score": round(analysis.get("exa_deep_reasoning_score", 0), 2),
            "firecrawl_signals": len(analysis["provider_matches"].get("firecrawl", [])),
        },
        "query_analysis": {
            "word_count": analysis["complexity"]["word_count"],
            "is_complex": analysis["complexity"]["is_complex"],
            "complexity_score": round(analysis["complexity"]["complexity_score"], 2),
            "has_url": analysis["detected_url"],
            "recency_focused": analysis["recency_focused"],
            "language_hint": analysis.get("language_hint", "en"),
            "routing_class": analysis.get("routing_class", "general"),
        },
        "all_matches": {
            provider: [
                {"matched": m["matched"], "weight": m["weight"]}
                for m in matches
            ]
            for provider, matches in analysis["provider_matches"].items()
            if matches
        },
        "available_providers": [
            p for p in ["serper", "serpbase", "brave", "tavily", "linkup", "querit", "exa", "firecrawl", "perplexity", "kilo-perplexity", "you", "searxng"]
            if get_api_key(p, config) and p not in config.get("auto_routing", {}).get("disabled_providers", []) and _provider_auto_allowed(p, config.get("auto_routing", {}))
        ]
    }




def _provider_auto_allowed(provider: str, auto_config: Dict[str, Any]) -> bool:
    """Return whether a configured provider may be selected by auto-routing/fallback.

    Explicit provider calls still work; this gate only prevents low-trust or
    experimental providers from receiving user queries automatically.
    """
    auto_allow = auto_config.get("auto_allow", {}) if isinstance(auto_config, dict) else {}
    if not isinstance(auto_allow, dict):
        return True
    return bool(auto_allow.get(provider, True))


class ProviderConfigError(Exception):
    """Raised when a provider is missing or has an invalid API key/config."""
    pass


class ProviderRequestError(Exception):
    """Structured provider error with retry/cooldown metadata."""

    def __init__(self, message: str, status_code: Optional[int] = None, transient: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


TRANSIENT_HTTP_CODES = {429, 503}
COOLDOWN_STEPS_SECONDS = [60, 300, 1500, 3600]  # 1m -> 5m -> 25m -> 1h cap
RETRY_BACKOFF_SECONDS = [1, 3, 9]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_provider_health() -> Dict[str, Any]:
    if not PROVIDER_HEALTH_FILE.exists():
        return {}
    try:
        with open(PROVIDER_HEALTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, IOError):
        return {}


def _save_provider_health(state: Dict[str, Any]) -> None:
    _ensure_parent(PROVIDER_HEALTH_FILE)
    with open(PROVIDER_HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def provider_in_cooldown(provider: str) -> Tuple[bool, int]:
    state = _load_provider_health()
    pstate = state.get(provider, {})
    cooldown_until = int(pstate.get("cooldown_until", 0) or 0)
    remaining = cooldown_until - int(time.time())
    return (remaining > 0, max(0, remaining))


def mark_provider_failure(provider: str, error_message: str) -> Dict[str, Any]:
    state = _load_provider_health()
    now = int(time.time())
    pstate = state.get(provider, {})
    fail_count = int(pstate.get("failure_count", 0)) + 1
    cooldown_seconds = COOLDOWN_STEPS_SECONDS[min(fail_count - 1, len(COOLDOWN_STEPS_SECONDS) - 1)]
    state[provider] = {
        "failure_count": fail_count,
        "cooldown_until": now + cooldown_seconds,
        "cooldown_seconds": cooldown_seconds,
        "last_error": error_message,
        "last_failure_at": now,
    }
    _save_provider_health(state)
    return state[provider]


def reset_provider_health(provider: str) -> None:
    state = _load_provider_health()
    if provider in state:
        state.pop(provider, None)
        _save_provider_health(state)


def _title_from_url(url: str) -> str:
    """Derive a readable title from a URL when none is provided."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        # Use last meaningful path segment as context
        segments = [s for s in parsed.path.strip("/").split("/") if s]
        if segments:
            last = segments[-1].replace("-", " ").replace("_", " ")
            # Strip file extensions
            last = re.sub(r'\.\w{2,4}$', '', last)
            if last:
                return f"{domain} — {last[:80]}"
        return domain
    except Exception:
        return url[:60]


def normalize_result_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    return f"{netloc}{path}"


def deduplicate_results_across_providers(results_by_provider: List[Tuple[str, Dict[str, Any]]], max_results: int) -> Tuple[List[Dict[str, Any]], int]:
    deduped = []
    seen = set()
    dedup_count = 0
    for provider_name, data in results_by_provider:
        for item in data.get("results", []):
            norm = normalize_result_url(item.get("url", ""))
            if norm and norm in seen:
                dedup_count += 1
                continue
            if norm:
                seen.add(norm)
            item = item.copy()
            item.setdefault("provider", provider_name)
            deduped.append(item)
            if len(deduped) >= max_results:
                return deduped, dedup_count
    return deduped, dedup_count

def _choose_tie_winner(query: str, winners: List[str], priority: List[str]) -> str:
    """Break score ties deterministically per query.

    Uses a stable hash of the query to distribute ties across providers while
    keeping the same query reproducible across runs.
    """
    ordered_winners = [p for p in priority if p in winners]
    if not ordered_winners:
        ordered_winners = sorted(winners)
    if len(ordered_winners) == 1:
        return ordered_winners[0]
    digest = hashlib.sha256(f"{query}|{'|'.join(ordered_winners)}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(ordered_winners)
    return ordered_winners[idx]


def _result_domain(url: str) -> str:
    try:
        netloc = urlparse(url or "").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


CANONICAL_DOMAIN_RULES: Dict[str, Dict[str, List[str]]] = {
    "official_vendor_release": {
        "boost": [
            "mistral.ai", "anthropic.com", "openai.com", "googleblog.com",
            "blog.google", "ai.google.dev", "meta.com", "ai.meta.com",
            "nvidia.com", "developer.nvidia.com", "apple.com", "microsoft.com",
        ],
        "demote": ["youtube.com", "youtu.be", "medium.com", "aizolo.com", "reddit.com"],
    },
    "official_docs": {
        "boost": ["docs.", "developer.", "github.com", "readthedocs.io", "modelcontextprotocol.io"],
        "demote": ["medium.com", "dev.to", "reddit.com", "stackoverflow.com", "youtube.com"],
    },
    "policy_pdf": {
        "boost": ["europa.eu", "ec.europa.eu", "nist.gov", "nvlpubs.nist.gov", "oecd.org", "who.int", "gov.uk", "federalregister.gov"],
        "demote": ["scribd.com", "researchgate.net", "universityofcalifornia.edu", "slideshare.net"],
    },
    "finance_earnings_official": {
        "boost": ["investor.", "ir.", "nvidia.com", "sec.gov", "nasdaq.com"],
        "demote": ["reddit.com", "fool.com", "seekingalpha.com", "youtube.com"],
    },
    "security_advisory": {
        "boost": ["nvd.nist.gov", "cve.org", "github.com/advisories", "security.", "cert.europa.eu", "kb.cert.org"],
        "demote": ["youtube.com", "medium.com", "reddit.com"],
    },
}


def _domain_matches_rule(domain: str, rule: str) -> bool:
    return domain == rule or domain.endswith(f".{rule}") or domain.startswith(rule)


def rerank_results_for_intent(
    query: str,
    routing_class: str,
    results: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Small authority reranker for classes where source authority beats snippet luck."""
    rules = CANONICAL_DOMAIN_RULES.get(routing_class, {})
    if not results or not rules:
        return results, {"reranked": False, "routing_class": routing_class}

    q = query.lower()
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for idx, item in enumerate(results):
        domain = _result_domain(item.get("url", ""))
        title = (item.get("title") or "").lower()
        snippet = (item.get("snippet") or item.get("description") or "").lower()
        score = float(len(results) - idx) * 0.01
        if any(_domain_matches_rule(domain, rule) for rule in rules.get("boost", [])):
            score += 10.0
        if any(_domain_matches_rule(domain, rule) for rule in rules.get("demote", [])):
            score -= 6.0
        if routing_class == "official_vendor_release" and any(term in domain for term in ("mistral", "anthropic", "openai", "nvidia", "google", "meta")):
            score += 3.0
        if routing_class == "policy_pdf" and (item.get("url", "").lower().endswith(".pdf") or "pdf" in title):
            score += 2.0
        if "official" in q and ("official" in title or "official" in snippet):
            score += 1.0
        scored.append((score, idx, item))

    reranked = [item.copy() for _, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))]
    before_urls = [item.get("url", "") for item in results]
    after_urls = [item.get("url", "") for item in reranked]
    changed = before_urls != after_urls
    return reranked, {
        "reranked": changed,
        "routing_class": routing_class,
        "top_domain_before": _result_domain(results[0].get("url", "")) if results else None,
        "top_domain_after": _result_domain(reranked[0].get("url", "")) if reranked else None,
    }


def _snippet_text(item: Dict[str, Any]) -> str:
    return " ".join(
        str(item.get(k) or "")
        for k in ("description", "snippet", "content", "raw_content", "summary")
    ).strip()


def build_quality_report(
    query: str,
    result: Dict[str, Any],
    routing_info: Dict[str, Any],
    providers_considered: List[str],
    eligible_providers: List[str],
    cooldown_skips: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build transparent search-quality diagnostics without changing results."""
    results = result.get("results", []) or []
    domains = [_result_domain(r.get("url", "")) for r in results]
    domains = [d for d in domains if d]
    unique_domains = sorted(set(domains))
    duplicate_count = int(result.get("metadata", {}).get("dedup_count", 0) or 0)

    short_snippets = 0
    for item in results:
        if len(_snippet_text(item)) < 40:
            short_snippets += 1

    extract_reasons: List[str] = []
    confidence_level = routing_info.get("confidence_level") or "unknown"
    confidence_score = routing_info.get("confidence")
    if confidence_level == "low" or (confidence_score is not None and float(confidence_score or 0) < 0.4):
        extract_reasons.append("low routing confidence")
    if len(results) < 3:
        extract_reasons.append("few search results")
    if results and len(unique_domains) <= 1:
        extract_reasons.append("low domain diversity")
    if duplicate_count:
        extract_reasons.append("duplicate results detected")
    if results and short_snippets / max(len(results), 1) >= 0.5:
        extract_reasons.append("thin snippets")

    skipped = []
    for item in cooldown_skips:
        skipped.append({
            "provider": item.get("provider"),
            "reason": "cooldown",
            "cooldown_remaining_seconds": item.get("cooldown_remaining_seconds"),
        })
    for err in errors:
        skipped.append({
            "provider": err.get("provider"),
            "reason": "error",
            "error": err.get("error"),
        })

    return {
        "query": query,
        "selected_provider": routing_info.get("provider") or result.get("provider"),
        "routing_reason": routing_info.get("reason"),
        "routing_policy": routing_info.get("routing_policy", ROUTING_POLICY),
        "routing_class": routing_info.get("analysis_summary", {}).get("routing_class"),
        "language_hint": routing_info.get("analysis_summary", {}).get("language_hint"),

        "confidence": confidence_level,
        "confidence_score": routing_info.get("confidence"),
        "providers_considered": providers_considered,
        "eligible_providers": eligible_providers,
        "skipped_providers": skipped,
        "result_count": len(results),
        "domain_count": len(unique_domains),
        "domains": unique_domains,
        "domain_diversity": (len(unique_domains) / len(results)) if results else 0.0,
        "duplicate_count": duplicate_count,
        "thin_snippet_count": short_snippets,
        "extract_recommended": bool(extract_reasons),
        "extract_reasons": extract_reasons,
        "scores": routing_info.get("scores", {}),
    }


def select_research_providers(
    primary_provider: str,
    provider_priority: List[str],
    available_providers: set,
    max_providers: int = 3,
) -> List[str]:
    """Pick a compact provider set for research mode."""
    preferred = [primary_provider, "linkup", "tavily", "exa", "firecrawl", "brave", "serper", "you", "querit"]
    ordered: List[str] = []
    for provider in preferred + provider_priority:
        if provider and provider in available_providers and provider not in ordered:
            ordered.append(provider)
        if len(ordered) >= max_providers:
            break
    return ordered


def run_research_mode(
    query: str,
    research_providers: List[str],
    execute_search,
    extract_urls,
    max_results: int,
    max_extract_urls: int = 3,
    time_budget_seconds: float | None = None,
    now_fn=None,
) -> Dict[str, Any]:
    """Run broad search, deduplicate, then extract top sources for grounding.

    Research mode is intentionally best-effort: provider/extraction failures should
    produce diagnostics and partial search results instead of throwing away the
    whole response. The optional time budget is checked between expensive calls so
    the mode can degrade safely before starting more provider work or extraction.
    """
    provider_results: List[Tuple[str, Dict[str, Any]]] = []
    provider_errors: List[Dict[str, Any]] = []
    now = now_fn or time.monotonic
    start = now()

    def budget_exhausted() -> bool:
        return time_budget_seconds is not None and (now() - start) >= time_budget_seconds

    for provider in research_providers:
        if budget_exhausted():
            provider_errors.append({"provider": provider, "error": "skipped: research time budget exhausted"})
            continue
        try:
            payload = execute_search(provider)
            provider_results.append((provider, payload))
        except Exception as e:
            provider_errors.append({"provider": provider, "error": str(e)})

    deduped, dedup_count = deduplicate_results_across_providers(provider_results, max_results)
    urls = [r.get("url") for r in deduped if r.get("url")][:max(0, max_extract_urls)]
    extracted = {"provider": None, "results": []}
    extraction_error = None
    if urls:
        if budget_exhausted():
            extraction_error = "skipped: research time budget exhausted"
        else:
            try:
                extracted = extract_urls(urls) or {"provider": None, "results": []}
            except Exception as e:
                extraction_error = str(e)
                extracted = {"provider": None, "results": []}

    routing = {
        "providers_queried": [p for p, _ in provider_results],
        "provider_errors": provider_errors,
        "extraction_provider": extracted.get("provider"),
    }
    if extraction_error:
        routing["extraction_error"] = extraction_error

    source_summaries = extracted.get("results", []) or []

    return {
        "mode": "research",
        "provider": "research",
        "query": query,
        "results": deduped,
        "source_summaries": source_summaries,
        "routing": routing,
        "metadata": {
            "dedup_count": dedup_count,
            "providers_merged": [p for p, _ in provider_results],
            "extracted_url_count": len(source_summaries),
        },
    }


# =============================================================================
# HTTP Client
# =============================================================================

def execute_provider_with_retry(provider: str, operation, max_attempts: int = 3) -> Dict[str, Any]:
    """Execute a provider operation with shared transient-error retry semantics."""
    last_error = None
    for attempt in range(0, max_attempts):
        try:
            return operation()
        except ProviderRequestError as e:
            last_error = e
            if e.status_code in {401, 403}:
                break
            if not e.transient:
                break
            if attempt < max_attempts - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
                continue
            break
        except Exception as e:
            last_error = e
            break
    raise last_error if last_error else Exception(f"Unknown {provider} provider execution error")


def _response_header(response, name: str) -> str:
    """Return an HTTP response header from urllib response/error objects."""
    if hasattr(response, "getheader"):
        value = response.getheader(name)
        if value is not None:
            return str(value)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get(name)
        except AttributeError:
            value = None
        if value is not None:
            return str(value)
    return ""


def _read_response_body(response) -> bytes:
    """Read an urllib response body and decode supported Content-Encoding values."""
    raw = response.read()
    encoding = _response_header(response, "Content-Encoding").strip().lower()

    if encoding in {"gzip", "x-gzip"} or raw.startswith(b"\x1f\x8b"):
        return gzip.decompress(raw)
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            # Some servers send raw deflate without the zlib wrapper.
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    if encoding == "br":
        raise ProviderRequestError(
            "Brotli-compressed response received, but web-search-plus does not bundle brotli support. "
            "Disable brotli for this provider or install a brotli-capable transport.",
            transient=False,
        )
    return raw


def _read_json_response(response) -> dict:
    """Read an urllib response as UTF-8 JSON with Content-Encoding handling."""
    return json.loads(_read_response_body(response).decode("utf-8"))


def make_request(url: str, headers: dict, body: dict, timeout: int = 30) -> dict:
    """Make HTTP POST request and return JSON response."""
    # Ensure User-Agent is set (required by some APIs like Exa/Cloudflare)
    if "User-Agent" not in headers:
        headers["User-Agent"] = "ClawdBot-WebSearchPlus/2.1"
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    
    try:
        with urlopen(req, timeout=timeout) as response:
            return _read_json_response(response)
    except HTTPError as e:
        error_body = _read_response_body(e).decode("utf-8") if e.fp else str(e)
        try:
            error_json = json.loads(error_body)
            error_detail = error_json.get("error") or error_json.get("message") or error_body
        except json.JSONDecodeError:
            error_detail = error_body[:500]
        
        error_messages = {
            401: "Invalid or expired API key. Please check your credentials.",
            403: "Access forbidden. Your API key may not have permission for this operation.",
            429: "Rate limit exceeded. Please wait a moment and try again.",
            500: "Server error. The search provider is experiencing issues.",
            503: "Service unavailable. The search provider may be down."
        }
        
        friendly_msg = error_messages.get(e.code, f"API error: {error_detail}")
        raise ProviderRequestError(f"{friendly_msg} (HTTP {e.code})", status_code=e.code, transient=e.code in TRANSIENT_HTTP_CODES)
    except URLError as e:
        reason = str(getattr(e, "reason", e))
        is_timeout = "timed out" in reason.lower()
        raise ProviderRequestError(f"Network error: {reason}. Check your internet connection.", transient=is_timeout)
    except IncompleteRead as e:
        partial_len = len(getattr(e, "partial", b"") or b"")
        raise ProviderRequestError(
            f"Connection interrupted while reading response ({partial_len} bytes received). Please retry.",
            transient=True,
        )
    except TimeoutError:
        raise ProviderRequestError(f"Request timed out after {timeout}s. Try again or reduce max_results.", transient=True)


def make_get_request(url: str, headers: dict, timeout: int = 30) -> dict:
    """Make HTTP GET request and return JSON response."""
    if "User-Agent" not in headers:
        headers["User-Agent"] = "ClawdBot-WebSearchPlus/2.1"
    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=timeout) as response:
            return _read_json_response(response)
    except HTTPError as e:
        error_body = _read_response_body(e).decode("utf-8") if e.fp else str(e)
        try:
            error_json = json.loads(error_body)
            error_detail = error_json.get("error") or error_json.get("message") or error_body
        except json.JSONDecodeError:
            error_detail = error_body[:500]

        error_messages = {
            401: "Invalid or expired API key. Please check your credentials.",
            403: "Access forbidden. Your API key may not have permission for this operation.",
            429: "Rate limit exceeded. Please wait a moment and try again.",
            500: "Server error. The search provider is experiencing issues.",
            503: "Service unavailable. The search provider may be down."
        }

        friendly_msg = error_messages.get(e.code, f"API error: {error_detail}")
        raise ProviderRequestError(f"{friendly_msg} (HTTP {e.code})", status_code=e.code, transient=e.code in TRANSIENT_HTTP_CODES)
    except URLError as e:
        reason = str(getattr(e, "reason", e))
        is_timeout = "timed out" in reason.lower()
        raise ProviderRequestError(f"Network error: {reason}. Check your internet connection.", transient=is_timeout)
    except IncompleteRead as e:
        partial_len = len(getattr(e, "partial", b"") or b"")
        raise ProviderRequestError(
            f"Connection interrupted while reading response ({partial_len} bytes received). Please retry.",
            transient=True,
        )
    except TimeoutError:
        raise ProviderRequestError(f"Request timed out after {timeout}s. Try again or reduce max_results.", transient=True)


# =============================================================================
# Serper (Google Search API)
# =============================================================================

def search_serper(
    query: str,
    api_key: str,
    max_results: int = 5,
    country: str = "us",
    language: str = "en",
    search_type: str = "search",
    time_range: Optional[str] = None,
    include_images: bool = False,
) -> dict:
    """Search using Serper (Google Search API)."""
    endpoint = f"https://google.serper.dev/{search_type}"
    
    body = {
        "q": query,
        "gl": country,
        "hl": language,
        "num": max_results,
        "autocorrect": True,
    }
    
    if time_range and time_range != "none":
        tbs_map = {
            "hour": "qdr:h",
            "day": "qdr:d",
            "week": "qdr:w",
            "month": "qdr:m",
            "year": "qdr:y",
        }
        if time_range in tbs_map:
            body["tbs"] = tbs_map[time_range]
    
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    
    data = make_request(endpoint, headers, body)
    
    results = []
    for i, item in enumerate(data.get("organic", [])[:max_results]):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "score": round(1.0 - i * 0.1, 2),
            "date": item.get("date"),
        })
    
    answer = ""
    if data.get("answerBox", {}).get("answer"):
        answer = data["answerBox"]["answer"]
    elif data.get("answerBox", {}).get("snippet"):
        answer = data["answerBox"]["snippet"]
    elif data.get("knowledgeGraph", {}).get("description"):
        answer = data["knowledgeGraph"]["description"]
    elif results:
        answer = results[0]["snippet"]
    
    images = []
    if include_images:
        try:
            img_data = make_request(
                "https://google.serper.dev/images",
                headers,
                {"q": query, "gl": country, "hl": language, "num": 5},
            )
            images = [img.get("imageUrl", "") for img in img_data.get("images", [])[:5] if img.get("imageUrl")]
        except Exception:
            pass
    
    return {
        "provider": "serper",
        "query": query,
        "results": results,
        "images": images,
        "answer": answer,
        "knowledge_graph": data.get("knowledgeGraph"),
        "related_searches": [r.get("query") for r in data.get("relatedSearches", [])]
    }


# =============================================================================
# SerpBase (Google SERP API)
# =============================================================================

def _strip_tracking_params(url: str) -> str:
    """Remove common SERP tracking params while preserving the canonical target URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    tracking_prefixes = ("utm_",)
    tracking_names = {"srsltid", "gclid", "fbclid", "mc_cid", "mc_eid"}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in tracking_names and not key.lower().startswith(tracking_prefixes)
    ]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _serpbase_related_search_query(item: Any) -> Optional[str]:
    if isinstance(item, dict):
        return item.get("query") or item.get("title")
    if isinstance(item, str):
        return item
    return None


def search_serpbase(
    query: str,
    api_key: str,
    max_results: int = 5,
    country: str = "us",
    language: str = "en",
    page: int = 1,
    api_url: str = "https://api.serpbase.dev/google/search",
    timeout: int = 30,
) -> dict:
    """Search using SerpBase's Google Search endpoint.

    SerpBase returns HTTP 200 for some business failures, so `status == 0` is
    required before parsing results.
    """
    body = {
        "q": query,
        "hl": language,
        "gl": country,
        "page": page,
    }
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    data = make_request(api_url, headers, body, timeout=timeout)
    status = data.get("status", 0)
    if status != 0:
        message = data.get("message") or data.get("error") or data.get("msg") or "business status failure"
        transient = status in {1029, 1502, 1503, 1504}
        raise ProviderRequestError(f"SerpBase error {status}: {message}", transient=transient)

    results = []
    for i, item in enumerate(data.get("organic", [])[:max_results]):
        results.append({
            "title": item.get("title", ""),
            "url": _strip_tracking_params(item.get("link", "") or item.get("url", "")),
            "snippet": item.get("snippet", ""),
            "score": round(1.0 - i * 0.1, 2),
            "rank": item.get("rank") or item.get("position") or i + 1,
            "display_link": item.get("display_link") or item.get("displayed_link"),
        })

    related_searches = [
        value for value in (_serpbase_related_search_query(item) for item in data.get("related_searches", [])) if value
    ]
    answer = ""
    if data.get("answer_box"):
        answer_box = data.get("answer_box") or {}
        answer = answer_box.get("answer") or answer_box.get("snippet") or ""
    elif data.get("knowledge_graph"):
        kg = data.get("knowledge_graph") or {}
        answer = kg.get("description") or kg.get("subtitle") or ""
    elif results:
        answer = results[0]["snippet"]

    return {
        "provider": "serpbase",
        "query": query,
        "results": results,
        "answer": answer,
        "knowledge_graph": data.get("knowledge_graph"),
        "related_searches": related_searches,
        "session_id": data.get("session_id"),
    }


# =============================================================================
# Brave Search
# =============================================================================

def search_brave(
    query: str,
    api_key: str,
    max_results: int = 5,
    country: str = "US",
    language: str = "en",
    time_range: Optional[str] = None,
    safesearch: str = "moderate",
) -> dict:
    """Search using Brave Search API."""
    freshness_map = {
        "hour": "pd",
        "day": "pd",
        "week": "pw",
        "month": "pm",
        "year": "py",
    }
    params = {
        "q": query,
        "count": max_results,
        "country": country.upper(),
        "search_lang": language,
        "safesearch": safesearch,
        "spellcheck": 1,
    }
    if time_range and time_range in freshness_map:
        params["freshness"] = freshness_map[time_range]

    url = f"https://api.search.brave.com/res/v1/web/search?{urlencode(params)}"
    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }

    data = make_get_request(url, headers)

    web_results = (data.get("web") or {}).get("results", [])[:max_results]
    results = []
    for i, item in enumerate(web_results):
        snippet_parts = []
        description = item.get("description") or item.get("snippet") or ""
        if description:
            snippet_parts.append(description)
        extra_snippets = item.get("extra_snippets") or []
        if extra_snippets:
            snippet_parts.extend(extra_snippets[:2])
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": " ... ".join(part for part in snippet_parts if part),
            "score": round(1.0 - i * 0.1, 2),
            "age": item.get("age"),
        })

    answer = ""
    if data.get("summary"):
        answer = data.get("summary", "")
    elif data.get("infobox", {}).get("description"):
        answer = data["infobox"]["description"]
    elif results:
        answer = results[0]["snippet"]

    return {
        "provider": "brave",
        "query": query,
        "results": results,
        "images": [],
        "answer": answer,
        "mixed": data.get("mixed"),
    }


# =============================================================================
# Tavily (Research Search)
# =============================================================================

def search_tavily(
    query: str,
    api_key: str,
    max_results: int = 5,
    depth: str = "basic",
    topic: str = "general",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    include_images: bool = False,
    include_raw_content: bool = False,
) -> dict:
    """Search using Tavily (AI Research Search)."""
    endpoint = "https://api.tavily.com/search"
    
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": depth,
        "topic": topic,
        "include_images": include_images,
        "include_answer": True,
        "include_raw_content": include_raw_content,
    }
    
    if include_domains:
        body["include_domains"] = include_domains
    if exclude_domains:
        body["exclude_domains"] = exclude_domains
    
    headers = {"Content-Type": "application/json"}
    
    data = make_request(endpoint, headers, body)
    
    results = []
    for item in data.get("results", [])[:max_results]:
        result = {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "score": round(item.get("score", 0.0), 3),
        }
        if include_raw_content and item.get("raw_content"):
            result["raw_content"] = item["raw_content"]
        results.append(result)
    
    return {
        "provider": "tavily",
        "query": query,
        "results": results,
        "images": data.get("images", []),
        "answer": data.get("answer", ""),
    }


# =============================================================================
# Querit (Multi-lingual search API for AI, with rich metadata and real-time information)
# =============================================================================

def _map_querit_time_range(time_range: Optional[str]) -> Optional[str]:
    """Map generic time ranges to Querit's compact date filter format."""
    if not time_range:
        return None
    return {
        "day": "d1",
        "week": "w1",
        "month": "m1",
        "year": "y1",
    }.get(time_range, time_range)


def search_querit(
    query: str,
    api_key: str,
    max_results: int = 5,
    language: str = "en",
    country: str = "us",
    time_range: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    base_url: str = "https://api.querit.ai",
    base_path: str = "/v1/search",
    timeout: int = 30,
) -> dict:
    """Search using Querit.

    Mirrors the Querit Python SDK payload shape:
      - query
      - count
      - optional filters: languages, geo, sites, timeRange
    """
    endpoint = base_url.rstrip("/") + base_path

    filters: Dict[str, Any] = {}
    if language:
        filters["languages"] = {"include": [language.lower()]}
    if country:
        filters["geo"] = {"countries": {"include": [country.upper()]}}
    if include_domains or exclude_domains:
        sites: Dict[str, List[str]] = {}
        if include_domains:
            sites["include"] = include_domains
        if exclude_domains:
            sites["exclude"] = exclude_domains
        filters["sites"] = sites

    querit_time_range = _map_querit_time_range(time_range)
    if querit_time_range:
        filters["timeRange"] = {"date": querit_time_range}

    body: Dict[str, Any] = {
        "query": query,
        "count": max_results,
    }
    if filters:
        body["filters"] = filters

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    data = make_request(endpoint, headers, body, timeout=timeout)

    error_code = data.get("error_code")
    error_msg = data.get("error_msg")
    if error_msg or (error_code not in (None, 0, 200)):
        message = error_msg or f"Querit request failed with error_code={error_code}"
        raise ProviderRequestError(message)

    raw_results = ((data.get("results") or {}).get("result")) or []
    results = []
    for i, item in enumerate(raw_results[:max_results]):
        snippet = item.get("snippet") or item.get("page_age") or ""
        result = {
            "title": item.get("title") or _title_from_url(item.get("url", "")),
            "url": item.get("url", ""),
            "snippet": snippet,
            "score": round(1.0 - i * 0.05, 3),
        }
        if item.get("page_time") is not None:
            result["page_time"] = item["page_time"]
        if item.get("page_age"):
            result["date"] = item["page_age"]
        if item.get("language") is not None:
            result["language"] = item["language"]
        results.append(result)

    answer = results[0]["snippet"] if results else ""

    return {
        "provider": "querit",
        "query": query,
        "results": results,
        "images": [],
        "answer": answer,
        "metadata": {
            "search_id": data.get("search_id"),
            "time_range": querit_time_range,
        }
    }


# =============================================================================
# Linkup Search
# =============================================================================

def search_linkup(
    query: str,
    api_key: str,
    max_results: int = 5,
    depth: str = "standard",
    output_type: str = "searchResults",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    api_url: str = "https://api.linkup.so/v1/search",
    timeout: int = 30,
) -> dict:
    """Search using Linkup's source-grounded web search API."""
    body: Dict[str, Any] = {
        "q": query,
        "depth": depth,
        "outputType": output_type,
    }
    if include_domains:
        body["includeDomains"] = include_domains[:50]
    if exclude_domains:
        body["excludeDomains"] = exclude_domains[:50]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    data = make_request(api_url, headers, body, timeout=timeout)
    if data.get("error"):
        raise ProviderRequestError(str(data.get("error")))

    raw_results = data.get("results") or data.get("sources") or []
    results = []
    for i, item in enumerate(raw_results[:max_results]):
        snippet = item.get("content") or item.get("snippet") or item.get("description") or ""
        result = {
            "title": item.get("name") or item.get("title") or _title_from_url(item.get("url", "")),
            "url": item.get("url", ""),
            "snippet": snippet,
            "score": round(1.0 - i * 0.05, 3),
        }
        if item.get("type") is not None:
            result["type"] = item["type"]
        if item.get("favicon") is not None:
            result["favicon"] = item["favicon"]
        results.append(result)

    return {
        "provider": "linkup",
        "query": query,
        "results": results,
        "images": data.get("images", []),
        "answer": data.get("answer", ""),
        "metadata": {
            "depth": depth,
            "output_type": output_type,
        },
    }


# =============================================================================
# Firecrawl Search
# =============================================================================

def _map_firecrawl_time_range(time_range: Optional[str]) -> Optional[str]:
    """Map generic time ranges to Firecrawl/Google tbs values."""
    if not time_range:
        return None
    return {
        "hour": "qdr:h",
        "day": "qdr:d",
        "week": "qdr:w",
        "month": "qdr:m",
        "year": "qdr:y",
    }.get(time_range, time_range)


def search_firecrawl(
    query: str,
    api_key: str,
    max_results: int = 5,
    country: str = "US",
    time_range: Optional[str] = None,
    sources: Optional[List[str]] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    scrape_markdown: bool = False,
    ignore_invalid_urls: bool = False,
    api_url: str = "https://api.firecrawl.dev/v2/search",
    timeout_ms: int = 30000,
) -> dict:
    """Search using Firecrawl's v2 search endpoint."""
    selected_sources = sources or ["web"]
    body: Dict[str, Any] = {
        "query": query,
        "limit": max_results,
        "sources": selected_sources,
        "timeout": timeout_ms,
        "ignoreInvalidURLs": ignore_invalid_urls,
    }

    if country:
        body["country"] = country.upper()

    tbs = _map_firecrawl_time_range(time_range)
    if tbs:
        body["tbs"] = tbs

    if include_domains:
        body["query"] += " " + " ".join(f"site:{domain}" for domain in include_domains)
    if exclude_domains:
        body["query"] += " " + " ".join(f"-site:{domain}" for domain in exclude_domains)

    if scrape_markdown:
        body["scrapeOptions"] = {"formats": ["markdown"]}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    data = make_request(api_url, headers, body, timeout=max(1, int(timeout_ms / 1000)))
    if data.get("success") is False:
        raise ProviderRequestError(data.get("error") or data.get("warning") or "Firecrawl request failed")

    response_data = data.get("data") or {}
    raw_web = response_data.get("web") or []
    results = []
    for i, item in enumerate(raw_web[:max_results]):
        snippet = item.get("description") or item.get("snippet") or ""
        result = {
            "title": item.get("title") or _title_from_url(item.get("url", "")),
            "url": item.get("url", ""),
            "snippet": snippet,
            "score": round(1.0 - i * 0.05, 3),
        }
        if item.get("position") is not None:
            result["position"] = item.get("position")
        if item.get("category") is not None:
            result["category"] = item.get("category")
        if item.get("markdown"):
            result["raw_content"] = item["markdown"]
            if not result["snippet"]:
                result["snippet"] = item["markdown"][:500]
        metadata = item.get("metadata") or {}
        if metadata.get("statusCode") is not None:
            result["status_code"] = metadata.get("statusCode")
        if metadata.get("error"):
            result["error"] = metadata.get("error")
        results.append(result)

    images = []
    for image in response_data.get("images") or []:
        image_url = image.get("imageUrl")
        if image_url:
            images.append(image_url)

    answer = results[0]["snippet"] if results else ""
    return {
        "provider": "firecrawl",
        "query": query,
        "results": results,
        "images": images,
        "answer": answer,
        "warning": data.get("warning"),
        "credits_used": data.get("creditsUsed"),
        "metadata": {
            "id": data.get("id"),
            "sources": selected_sources,
            "tbs": tbs,
        },
    }


# =============================================================================
# Extract Plus (URL Content Extraction)
# =============================================================================

def _normalize_extract_result(
    provider: str,
    url: str,
    title: str = "",
    content: str = "",
    raw_content: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    result = {
        "url": url,
        "title": title or _title_from_url(url),
        "content": content or "",
        "raw_content": raw_content if raw_content is not None else (content or ""),
        "provider": provider,
    }
    for key, value in extra.items():
        if value is not None:
            result[key] = value
    return result


def extract_firecrawl(
    urls: List[str],
    api_key: str,
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    api_url: str = "https://api.firecrawl.dev/v2/scrape",
    timeout: int = 60,
) -> dict:
    """Extract URL content using Firecrawl scrape."""
    formats = ["markdown"] if output_format != "html" else ["html"]
    if include_raw_html and "html" not in formats:
        formats.append("html")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    results: List[Dict[str, Any]] = []
    for url in urls:
        body: Dict[str, Any] = {"url": url, "formats": formats}
        if render_js:
            body["waitFor"] = 1000
        data = make_request(api_url, headers, body, timeout=timeout)
        if data.get("success") is False:
            results.append(_normalize_extract_result("firecrawl", url, error=data.get("error") or data.get("warning") or "Firecrawl scrape failed"))
            continue
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        metadata = payload.get("metadata") or {}
        final_url = metadata.get("sourceURL") or metadata.get("url") or url
        title = metadata.get("title") or ""
        markdown = payload.get("markdown") or ""
        html = payload.get("html") or payload.get("rawHtml") or ""
        content = html if output_format == "html" else markdown or html
        images = None
        if include_images:
            md_images = []
            seen_image_urls = set()
            for alt, image_url in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", markdown):
                if image_url not in seen_image_urls:
                    md_images.append({"alt": alt, "url": image_url})
                    seen_image_urls.add(image_url)
            og_image = metadata.get("ogImage") or metadata.get("og:image")
            if og_image and og_image not in seen_image_urls:
                md_images.insert(0, {"alt": "og:image", "url": og_image})
            images = md_images or None
        results.append(_normalize_extract_result(
            "firecrawl",
            final_url,
            title=title,
            content=content,
            raw_content=content,
            raw_html=html if html else None,
            images=images,
            metadata=metadata,
        ))
    return {"provider": "firecrawl", "results": results}


def extract_linkup(
    urls: List[str],
    api_key: str,
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    api_url: str = "https://api.linkup.so/v1/fetch",
    timeout: int = 30,
) -> dict:
    """Extract URL content using Linkup fetch."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def fetch_one(url: str) -> Dict[str, Any]:
        body = {
            "url": url,
            "extractImages": include_images,
            "includeRawHtml": include_raw_html or output_format == "html",
            "renderJs": render_js,
        }
        data = make_request(api_url, headers, body, timeout=timeout)
        if data.get("error"):
            return _normalize_extract_result("linkup", url, error=str(data.get("error")))
        markdown = data.get("markdown") or ""
        raw_html = data.get("rawHtml") or data.get("raw_html") or ""
        content = raw_html if output_format == "html" else markdown or raw_html
        return _normalize_extract_result(
            "linkup",
            url,
            content=content,
            raw_content=content,
            raw_html=raw_html if raw_html else None,
            images=data.get("images") if include_images else None,
        )

    if len(urls) <= 1:
        return {"provider": "linkup", "results": [fetch_one(url) for url in urls]}

    indexed_results: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(len(urls), 5)) as executor:
        futures = {executor.submit(fetch_one, url): idx for idx, url in enumerate(urls)}
        for future in as_completed(futures):
            indexed_results[futures[future]] = future.result()
    results = [indexed_results[idx] for idx in range(len(urls)) if idx in indexed_results]
    return {"provider": "linkup", "results": results}


def extract_tavily(
    urls: List[str],
    api_key: str,
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    api_url: str = "https://api.tavily.com/extract",
    timeout: int = 30,
) -> dict:
    """Extract URL content using Tavily extract."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": urls, "include_images": include_images}
    data = make_request(api_url, headers, body, timeout=timeout)
    results: List[Dict[str, Any]] = []
    for item in data.get("results", []):
        url = item.get("url", "")
        content = item.get("raw_content") or item.get("content") or ""
        results.append(_normalize_extract_result(
            "tavily",
            url,
            title=item.get("title", ""),
            content=content,
            raw_content=content,
            images=item.get("images") if include_images else None,
        ))
    for failed in data.get("failed_results", []) or []:
        failed_url = failed.get("url", "")
        results.append(_normalize_extract_result("tavily", failed_url, error=failed.get("error") or "Tavily extract failed"))
    return {"provider": "tavily", "results": results}


def extract_exa(
    urls: List[str],
    api_key: str,
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    api_url: str = "https://api.exa.ai/contents",
    timeout: int = 30,
) -> dict:
    """Extract URL content using Exa Contents API."""
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    body: Dict[str, Any] = {"urls": urls, "text": True}
    data = make_request(api_url, headers, body, timeout=timeout)
    results: List[Dict[str, Any]] = []
    for item in data.get("results", []):
        url = item.get("url") or item.get("id") or ""
        content = item.get("text") or item.get("summary") or ""
        results.append(_normalize_extract_result(
            "exa",
            url,
            title=item.get("title", ""),
            content=content,
            raw_content=content,
            summary=item.get("summary"),
            highlights=item.get("highlights"),
            published_date=item.get("publishedDate"),
            author=item.get("author"),
            image=item.get("image") if include_images else None,
            favicon=item.get("favicon"),
        ))
    return {
        "provider": "exa",
        "results": results,
        "request_id": data.get("requestId"),
        "cost_dollars": data.get("costDollars"),
        "statuses": data.get("statuses"),
    }


def extract_you(
    urls: List[str],
    api_key: str,
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    api_url: str = "https://ydc-index.io/v1/contents",
    timeout: int = 30,
) -> dict:
    """Extract URL content using You.com Contents API."""
    formats = ["html" if output_format == "html" else "markdown"]
    if include_raw_html and "html" not in formats:
        formats.append("html")
    if "metadata" not in formats:
        formats.append("metadata")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {"urls": urls, "formats": formats, "crawl_timeout": max(1, min(timeout, 60))}
    data = make_request(api_url, headers, body, timeout=timeout)
    raw_items = data if isinstance(data, list) else data.get("results", []) or data.get("data", [])
    results: List[Dict[str, Any]] = []
    for item in raw_items:
        url = item.get("url", "")
        markdown = item.get("markdown") or ""
        html = item.get("html") or ""
        content = html if output_format == "html" else markdown or html
        results.append(_normalize_extract_result(
            "you",
            url,
            title=item.get("title", ""),
            content=content,
            raw_content=content,
            raw_html=html if html else None,
            metadata=item.get("metadata"),
        ))
    return {"provider": "you", "results": results}


EXTRACT_PROVIDER_PRIORITY = ["tavily", "exa", "linkup", "firecrawl", "you"]


def extract_plus(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> dict:
    """Extract URL content with provider fallback."""
    config = config or load_config()
    selected = provider or "auto"
    if not urls:
        return {"provider": selected, "results": [], "error": "No URLs provided", "requested_provider": selected}
    invalid = [u for u in urls if not (isinstance(u, str) and u.startswith(("http://", "https://")))]
    if invalid:
        return {
            "provider": selected,
            "results": [],
            "error": f"Invalid URL(s) — must start with http:// or https://: {invalid}",
            "requested_provider": selected,
        }
    providers = EXTRACT_PROVIDER_PRIORITY if selected == "auto" else [selected] + [p for p in EXTRACT_PROVIDER_PRIORITY if p != selected]
    errors = []
    cooldown_skips = []
    for prov in providers:
        if prov not in EXTRACT_PROVIDER_PRIORITY:
            errors.append({"provider": prov, "error": f"Provider {prov} does not support extraction"})
            continue
        key = get_api_key(prov, config)
        if not key:
            errors.append({"provider": prov, "error": "missing_api_key"})
            continue
        in_cooldown, remaining = provider_in_cooldown(prov)
        if in_cooldown and not (selected != "auto" and prov == selected):
            cooldown_skips.append({"provider": prov, "cooldown_remaining_seconds": remaining})
            continue
        try:
            def execute_extract() -> Dict[str, Any]:
                if prov == "firecrawl":
                    fc = config.get("firecrawl", {})
                    return extract_firecrawl(urls, key, output_format, include_images, include_raw_html, render_js, api_url=fc.get("scrape_url", "https://api.firecrawl.dev/v2/scrape"), timeout=int(fc.get("extract_timeout", 60)))
                if prov == "linkup":
                    lu = config.get("linkup", {})
                    return extract_linkup(urls, key, output_format, include_images, include_raw_html, render_js, api_url=lu.get("fetch_url", "https://api.linkup.so/v1/fetch"), timeout=int(lu.get("timeout", 30)))
                if prov == "tavily":
                    tv = config.get("tavily", {})
                    return extract_tavily(urls, key, output_format, include_images, include_raw_html, render_js, api_url=tv.get("extract_url", "https://api.tavily.com/extract"), timeout=int(tv.get("timeout", 30)))
                if prov == "exa":
                    exa = config.get("exa", {})
                    return extract_exa(urls, key, output_format, include_images, include_raw_html, render_js, api_url=exa.get("contents_url", "https://api.exa.ai/contents"), timeout=int(exa.get("timeout", 30)))
                you = config.get("you", {})
                return extract_you(urls, key, output_format, include_images, include_raw_html, render_js, api_url=you.get("contents_url", "https://ydc-index.io/v1/contents"), timeout=int(you.get("timeout", 30)))

            result = execute_provider_with_retry(prov, execute_extract)
            res_list = result.get("results") or []
            all_failed = bool(res_list) and all(r.get("error") for r in res_list)
            if all_failed:
                errors.append({
                    "provider": prov,
                    "error": "all_urls_failed",
                    "details": [r.get("error") for r in res_list],
                })
                continue
            reset_provider_health(prov)
            result["routing"] = {"provider": prov, "requested_provider": selected, "fallback_used": bool(errors) or bool(cooldown_skips), "fallback_errors": errors}
            if cooldown_skips:
                result["routing"]["cooldown_skips"] = cooldown_skips
            return result
        except Exception as e:
            error_msg = str(e)
            cooldown_info = mark_provider_failure(prov, error_msg)
            errors.append({"provider": prov, "error": error_msg, "cooldown_seconds": cooldown_info.get("cooldown_seconds")})
            continue
    error_result = {"provider": selected, "results": [], "error": "All extraction providers failed", "fallback_errors": errors}
    if cooldown_skips:
        error_result["cooldown_skips"] = cooldown_skips
    return error_result


# =============================================================================
# Exa (Neural/Semantic/Deep Search)
# =============================================================================

def search_exa(
    query: str,
    api_key: str,
    max_results: int = 5,
    search_type: str = "neural",
    exa_depth: str = "normal",
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    similar_url: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    text_verbosity: str = "standard",
) -> dict:
    """Search using Exa (Neural/Semantic/Deep Search).

    exa_depth controls synthesis level:
      - "normal": standard search (neural/fast/auto/keyword/instant)
      - "deep": multi-source synthesis with grounding (4-12s, $12/1k)
      - "deep-reasoning": cross-reference reasoning with grounding (12-50s, $15/1k)
    """
    is_deep = exa_depth in ("deep", "deep-reasoning")

    if similar_url:
        # findSimilar does not support deep search types
        endpoint = "https://api.exa.ai/findSimilar"
        body: Dict[str, Any] = {
            "url": similar_url,
            "numResults": max_results,
            "contents": {
                "text": {"maxCharacters": 2000, "verbosity": text_verbosity},
                "highlights": {"numSentences": 3, "highlightsPerUrl": 2},
            },
        }
    elif is_deep:
        endpoint = "https://api.exa.ai/search"
        body = {
            "query": query,
            "numResults": max_results,
            "type": exa_depth,
            "contents": {
                "text": {"maxCharacters": 5000, "verbosity": "full"},
            },
        }
    else:
        endpoint = "https://api.exa.ai/search"
        body = {
            "query": query,
            "numResults": max_results,
            "type": search_type,
            "contents": {
                "text": {"maxCharacters": 2000, "verbosity": text_verbosity},
                "highlights": {"numSentences": 3, "highlightsPerUrl": 2},
            },
        }

    if category:
        body["category"] = category
    if start_date:
        body["startPublishedDate"] = start_date
    if end_date:
        body["endPublishedDate"] = end_date
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    timeout = 55 if is_deep else 30
    data = make_request(endpoint, headers, body, timeout=timeout)

    results = []

    # Deep search: primary content in output field with grounding citations
    if is_deep:
        deep_output = data.get("output", {})
        synthesized_text = ""
        grounding_citations: List[Dict[str, Any]] = []

        if isinstance(deep_output.get("content"), str):
            synthesized_text = deep_output["content"]
        elif isinstance(deep_output.get("content"), dict):
            synthesized_text = json.dumps(deep_output["content"], ensure_ascii=False)

        for field_citation in deep_output.get("grounding", []):
            for cite in field_citation.get("citations", []):
                grounding_citations.append({
                    "url": cite.get("url", ""),
                    "title": cite.get("title", ""),
                    "confidence": field_citation.get("confidence", ""),
                    "field": field_citation.get("field", ""),
                })

        # Primary synthesized result
        if synthesized_text:
            results.append({
                "title": f"Exa {exa_depth.replace('-', ' ').title()} Synthesis",
                "url": "",
                "snippet": synthesized_text,
                "full_synthesis": synthesized_text,
                "score": 1.0,
                "grounding": grounding_citations[:10],
                "type": "synthesis",
            })

        # Supporting source documents
        for item in data.get("results", [])[:max_results]:
            text_content = item.get("text", "") or ""
            highlights = item.get("highlights", [])
            snippet = text_content[:800] if text_content else (highlights[0] if highlights else "")
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": snippet,
                "score": round(item.get("score", 0.0), 3),
                "published_date": item.get("publishedDate"),
                "author": item.get("author"),
                "type": "source",
            })

        answer = synthesized_text if synthesized_text else (results[1]["snippet"] if len(results) > 1 else "")

        return {
            "provider": "exa",
            "query": query,
            "exa_depth": exa_depth,
            "results": results,
            "images": [],
            "answer": answer,
            "grounding": grounding_citations,
            "metadata": {
                "synthesis_length": len(synthesized_text),
                "source_count": len(data.get("results", [])),
            },
        }

    # Standard search result parsing
    for item in data.get("results", [])[:max_results]:
        text_content = item.get("text", "") or ""
        highlights = item.get("highlights", [])
        if text_content:
            snippet = text_content[:800]
        elif highlights:
            snippet = " ... ".join(highlights[:2])
        else:
            snippet = ""

        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": snippet,
            "score": round(item.get("score", 0.0), 3),
            "published_date": item.get("publishedDate"),
            "author": item.get("author"),
        })

    answer = results[0]["snippet"] if results else ""

    return {
        "provider": "exa",
        "query": query if not similar_url else f"Similar to: {similar_url}",
        "results": results,
        "images": [],
        "answer": answer,
    }


# =============================================================================
# Perplexity-compatible Direct Answers
# =============================================================================

def search_perplexity(
    query: str,
    api_key: str,
    max_results: int = 5,
    model: str = "sonar-pro",
    api_url: str = "https://api.perplexity.ai/chat/completions",
    freshness: Optional[str] = None,
    provider_name: str = "perplexity",
) -> dict:
    """Search/answer using the native Perplexity API or a compatible gateway.

    Args:
        query: Search query
        api_key: Provider API key
        max_results: Maximum results to return
        model: Perplexity-compatible model to use
        api_url: Chat completions endpoint
        freshness: Filter by recency — 'day', 'week', 'month', 'year' (maps to
                   Perplexity's search_recency_filter parameter)
        provider_name: Result provider label (perplexity or kilo-perplexity)
    """
    # Map generic freshness values to Perplexity's search_recency_filter
    recency_map = {"day": "day", "pd": "day", "week": "week", "pw": "week", "month": "month", "pm": "month", "year": "year", "py": "year"}
    recency_filter = recency_map.get(freshness or "", None)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Answer with concise factual summary and include source URLs."},
            {"role": "user", "content": query},
        ],
        "temperature": 0.2,
    }
    if recency_filter:
        body["search_recency_filter"] = recency_filter

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    data = make_request(api_url, headers, body)
    choices = data.get("choices", [])
    message = choices[0].get("message", {}) if choices else {}
    answer = (message.get("content") or "").strip()

    # Prefer the structured citations array from Perplexity API response
    api_citations = data.get("citations", [])

    # Fallback: extract URLs from answer text if API doesn't provide citations
    if not api_citations:
        api_citations = []
        seen = set()
        for u in re.findall(r"https?://[^\s)\]}>\"']+", answer):
            if u not in seen:
                seen.add(u)
                api_citations.append(u)

    results = []

    # Primary result: the synthesized answer itself
    if answer:
        # Clean citation markers [1][2] for the snippet
        clean_answer = re.sub(r'\[\d+\]', '', answer).strip()
        results.append({
            "title": f"Perplexity Answer: {query[:80]}",
            "url": "https://www.perplexity.ai",
            "snippet": clean_answer[:500],
            "score": 1.0,
        })

    # Source results from citations
    for i, citation in enumerate(api_citations[:max_results - 1]):
        # citations can be plain URL strings or dicts with url/title
        if isinstance(citation, str):
            url = citation
            title = _title_from_url(url)
        else:
            url = citation.get("url", "")
            title = citation.get("title") or _title_from_url(url)
        results.append({
            "title": title,
            "url": url,
            "snippet": f"Source cited in Perplexity answer [citation {i+1}]",
            "score": round(0.9 - i * 0.1, 3),
        })

    return {
        "provider": provider_name,
        "query": query,
        "results": results,
        "images": [],
        "answer": answer,
        "metadata": {
            "model": model,
            "usage": data.get("usage", {}),
        }
    }



# =============================================================================
# You.com (LLM-Ready Web & News Search)
# =============================================================================

def search_you(
    query: str,
    api_key: str,
    max_results: int = 5,
    country: str = "US",
    language: str = "en",
    freshness: Optional[str] = None,
    safesearch: str = "moderate",
    include_news: bool = True,
    livecrawl: Optional[str] = None,
) -> dict:
    """Search using You.com (LLM-Ready Web & News Search).
    
    You.com excels at:
    - RAG applications with pre-extracted snippets
    - Combined web + news results in one call
    - Real-time information with automatic news classification
    - Clean, structured JSON optimized for AI consumption
    
    Args:
        query: Search query
        api_key: You.com API key
        max_results: Maximum results to return (default 5, max 100)
        country: ISO 3166-2 country code (e.g., US, GB, DE)
        language: BCP 47 language code (e.g., en, de, fr)
        freshness: Filter by recency: day, week, month, year, or YYYY-MM-DDtoYYYY-MM-DD
        safesearch: Content filter: off, moderate (default), strict
        include_news: Include news results when relevant (default True)
        livecrawl: Fetch full page content: "web", "news", or "all"
    """
    endpoint = "https://ydc-index.io/v1/search"
    
    # Build query parameters
    params = {
        "query": query,
        "count": max_results,
        "safesearch": safesearch,
    }
    
    if country:
        params["country"] = country.upper()
    if language:
        params["language"] = language.upper()
    if freshness:
        params["freshness"] = freshness
    if livecrawl:
        params["livecrawl"] = livecrawl
        params["livecrawl_formats"] = "markdown"
    
    # Build URL with query params (URL-encode values)
    query_string = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    url = f"{endpoint}?{query_string}"
    
    headers = {
        "X-API-KEY": api_key,
        "Accept": "application/json",
        "User-Agent": "ClawdBot-WebSearchPlus/2.4",
    }
    
    # Make GET request (You.com uses GET, not POST)
    from urllib.request import Request, urlopen
    req = Request(url, headers=headers, method="GET")
    
    try:
        with urlopen(req, timeout=30) as response:
            data = _read_json_response(response)
    except HTTPError as e:
        error_body = _read_response_body(e).decode("utf-8") if e.fp else str(e)
        try:
            error_json = json.loads(error_body)
            error_detail = error_json.get("error") or error_json.get("message") or error_body
        except json.JSONDecodeError:
            error_detail = error_body[:500]
        
        error_messages = {
            401: "Invalid or expired API key. Get one at https://api.you.com",
            403: "Access forbidden. Check your API key permissions.",
            429: "Rate limit exceeded. Please wait and try again.",
            500: "You.com server error. Try again later.",
            503: "You.com service unavailable."
        }
        friendly_msg = error_messages.get(e.code, f"API error: {error_detail}")
        raise ProviderRequestError(f"{friendly_msg} (HTTP {e.code})", status_code=e.code, transient=e.code in TRANSIENT_HTTP_CODES)
    except URLError as e:
        reason = str(getattr(e, "reason", e))
        is_timeout = "timed out" in reason.lower()
        raise ProviderRequestError(f"Network error: {reason}. Check your internet connection.", transient=is_timeout)
    except TimeoutError:
        raise ProviderRequestError("You.com request timed out after 30s.", transient=True)
    
    # Parse results
    results_data = data.get("results", {})
    web_results = results_data.get("web", [])
    news_results = results_data.get("news", []) if include_news else []
    metadata = data.get("metadata", {})
    
    # Normalize web results
    results = []
    for i, item in enumerate(web_results[:max_results]):
        snippets = item.get("snippets", [])
        snippet = snippets[0] if snippets else item.get("description", "")
        
        result = {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": snippet,
            "score": round(1.0 - i * 0.05, 3),  # Assign descending score
            "date": item.get("page_age"),
            "source": "web",
        }
        
        # Include additional snippets if available (great for RAG)
        if len(snippets) > 1:
            result["additional_snippets"] = snippets[1:3]
        
        # Include thumbnail and favicon for UI display
        if item.get("thumbnail_url"):
            result["thumbnail"] = item["thumbnail_url"]
        if item.get("favicon_url"):
            result["favicon"] = item["favicon_url"]
        
        # Include live-crawled content if available
        if item.get("contents"):
            result["raw_content"] = item["contents"].get("markdown") or item["contents"].get("html", "")
        
        results.append(result)
    
    # Add news results (if any)
    news = []
    for item in news_results[:5]:
        news.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
            "date": item.get("page_age"),
            "thumbnail": item.get("thumbnail_url"),
            "source": "news",
        })
    
    # Build answer from best snippets
    answer = ""
    if results:
        # Combine top snippets for LLM context
        top_snippets = []
        for r in results[:3]:
            if r.get("snippet"):
                top_snippets.append(r["snippet"])
        answer = " ".join(top_snippets)[:1000]
    
    return {
        "provider": "you",
        "query": query,
        "results": results,
        "news": news,
        "images": [],
        "answer": answer,
        "metadata": {
            "search_uuid": metadata.get("search_uuid"),
            "latency": metadata.get("latency"),
        }
    }


# =============================================================================
# SearXNG (Privacy-First Meta-Search)
# =============================================================================

def search_searxng(
    query: str,
    instance_url: str,
    max_results: int = 5,
    categories: Optional[List[str]] = None,
    engines: Optional[List[str]] = None,
    language: str = "en",
    time_range: Optional[str] = None,
    safesearch: int = 0,
) -> dict:
    """Search using SearXNG (self-hosted privacy-first meta-search).
    
    SearXNG excels at:
    - Privacy-preserving search (no tracking, no profiling)
    - Multi-source aggregation (70+ upstream engines)
    - $0 API cost (self-hosted)
    - Diverse perspectives from multiple search engines
    
    Args:
        query: Search query
        instance_url: URL of your SearXNG instance (required)
        max_results: Maximum results to return (default 5)
        categories: Search categories (general, images, news, videos, etc.)
        engines: Specific engines to use (google, bing, duckduckgo, etc.)
        language: Language code (e.g., en, de, fr)
        time_range: Filter by recency: day, week, month, year
        safesearch: Content filter: 0=off, 1=moderate, 2=strict
    
    Note:
        Requires a self-hosted SearXNG instance with JSON format enabled.
        See: https://docs.searxng.org/admin/installation.html
    """
    # Build URL with query parameters
    params = {
        "q": query,
        "format": "json",
        "language": language,
        "safesearch": str(safesearch),
    }
    
    if categories:
        params["categories"] = ",".join(categories)
    if engines:
        params["engines"] = ",".join(engines)
    if time_range:
        params["time_range"] = time_range
    
    # Build URL — instance_url comes from operator-controlled config/env only
    # (validated by _validate_searxng_url), not from agent/LLM input
    base_url = instance_url.rstrip("/")
    query_string = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    url = f"{base_url}/search?{query_string}"
    
    headers = {
        "User-Agent": "ClawdBot-WebSearchPlus/2.5",
        "Accept": "application/json",
    }
    
    # Make GET request
    req = Request(url, headers=headers, method="GET")
    
    try:
        with urlopen(req, timeout=30) as response:
            data = _read_json_response(response)
    except HTTPError as e:
        error_body = _read_response_body(e).decode("utf-8") if e.fp else str(e)
        try:
            error_json = json.loads(error_body)
            error_detail = error_json.get("error") or error_json.get("message") or error_body
        except json.JSONDecodeError:
            error_detail = error_body[:500]
        
        error_messages = {
            403: "JSON API disabled on this SearXNG instance. Enable 'json' in search.formats in settings.yml",
            404: "SearXNG instance not found. Check your instance URL.",
            500: "SearXNG server error. Check instance health.",
            503: "SearXNG service unavailable."
        }
        friendly_msg = error_messages.get(e.code, f"SearXNG error: {error_detail}")
        raise ProviderRequestError(f"{friendly_msg} (HTTP {e.code})", status_code=e.code, transient=e.code in TRANSIENT_HTTP_CODES)
    except URLError as e:
        reason = str(getattr(e, "reason", e))
        is_timeout = "timed out" in reason.lower()
        raise ProviderRequestError(f"Cannot reach SearXNG instance at {instance_url}. Error: {reason}", transient=is_timeout)
    except TimeoutError:
        raise ProviderRequestError("SearXNG request timed out after 30s. Check instance health.", transient=True)
    
    # Parse results
    raw_results = data.get("results", [])
    
    # Normalize results to unified format
    results = []
    engines_used = set()
    for i, item in enumerate(raw_results[:max_results]):
        engine = item.get("engine", "unknown")
        engines_used.add(engine)
        
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "score": round(item.get("score", 1.0 - i * 0.05), 3),
            "engine": engine,
            "category": item.get("category", "general"),
            "date": item.get("publishedDate"),
        })
    
    # Build answer from answers, infoboxes, or first result
    answer = ""
    if data.get("answers"):
        answer = data["answers"][0] if isinstance(data["answers"][0], str) else str(data["answers"][0])
    elif data.get("infoboxes"):
        infobox = data["infoboxes"][0]
        answer = infobox.get("content", "") or infobox.get("infobox", "")
    elif results:
        answer = results[0]["snippet"]
    
    return {
        "provider": "searxng",
        "query": query,
        "results": results,
        "images": [],
        "answer": answer,
        "suggestions": data.get("suggestions", []),
        "corrections": data.get("corrections", []),
        "metadata": {
            "number_of_results": data.get("number_of_results"),
            "engines_used": list(engines_used),
            "instance_url": instance_url,
        }
    }


# =============================================================================
# CLI
# =============================================================================

def main():
    config = load_config()
    
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

  Direct Answer Intent → Perplexity (via Kilo Gateway)
    "what is", "current status", local events, synthesized up-to-date answers

Examples:
  python3 search.py -q "iPhone 16 Pro Max price"          # → Serper (shopping)
  python3 search.py -q "how does HTTPS encryption work"   # → Tavily (research)
  python3 search.py -q "startups similar to Notion"       # → Exa (discovery)
  python3 search.py --explain-routing -q "your query"     # Debug routing

Full docs: See README.md and SKILL.md
        """,
    )
    
    # Common arguments
    parser.add_argument(
        "--provider", "-p", 
        choices=["serper", "serpbase", "brave", "tavily", "linkup", "querit", "exa", "firecrawl", "perplexity", "kilo-perplexity", "you", "searxng", "auto"],
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
    
    # Serper-specific
    serper_config = config.get("serper", {})
    parser.add_argument("--country", default=serper_config.get("country", "us"))
    parser.add_argument("--language", default=serper_config.get("language", "en"))
    parser.add_argument(
        "--type", 
        dest="search_type", 
        default=serper_config.get("type", "search"),
        choices=["search", "news", "images", "videos", "places", "shopping"]
    )
    parser.add_argument(
        "--time-range", 
        choices=["hour", "day", "week", "month", "year"]
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
        choices=["searchResults", "sourcedAnswer"],
        help="Linkup output type"
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
        choices=["normal", "deep", "deep-reasoning"],
        help="Exa search depth: deep (synthesized, 4-12s), deep-reasoning (cross-reference, 12-50s)"
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
        "--freshness",
        choices=["day", "week", "month", "year"],
        help="Filter results by recency (You.com/Serper)"
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
    
    args = parser.parse_args()
    
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
    provider_priority = auto_config.get("provider_priority", ["tavily", "linkup", "exa", "firecrawl", "perplexity", "kilo-perplexity", "brave", "serper", "you", "searxng", "serpbase", "querit"])
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
            if p not in providers_to_try and p not in disabled_providers and _provider_auto_allowed(p, auto_config) and get_api_key(p, config):
                providers_to_try.append(p)

    # Skip providers currently in cooldown
    eligible_providers = []
    cooldown_skips = []
    for p in providers_to_try:
        in_cd, remaining = provider_in_cooldown(p)
        if in_cd:
            cooldown_skips.append({"provider": p, "cooldown_remaining_seconds": remaining})
        else:
            eligible_providers.append(p)

    if not eligible_providers:
        eligible_providers = providers_to_try[:1]

    # Helper function to execute search for a provider
    def execute_search(prov: str) -> Dict[str, Any]:
        key = validate_api_key(prov, config)
        if prov == "serper":
            return search_serper(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                country=args.country,
                language=args.language,
                search_type=args.search_type,
                time_range=args.time_range,
                include_images=args.images,
            )
        elif prov == "serpbase":
            serpbase_config = config.get("serpbase", {})
            return search_serpbase(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                country=serpbase_config.get("country", args.country),
                language=serpbase_config.get("language", args.language),
                page=int(serpbase_config.get("page", 1)),
                api_url=serpbase_config.get("api_url", "https://api.serpbase.dev/google/search"),
                timeout=int(serpbase_config.get("timeout", 30)),
            )
        elif prov == "brave":
            brave_config = config.get("brave", {})
            return search_brave(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                country=brave_config.get("country", args.country),
                language=brave_config.get("search_lang", args.language),
                time_range=args.time_range or args.freshness,
                safesearch=brave_config.get("safesearch", "moderate"),
            )
        elif prov == "tavily":
            return search_tavily(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                depth=args.depth,
                topic=args.topic,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                include_images=args.images,
                include_raw_content=args.raw_content,
            )
        elif prov == "linkup":
            linkup_config = config.get("linkup", {})
            return search_linkup(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                depth=args.linkup_depth,
                output_type=args.linkup_output_type,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                api_url=linkup_config.get("api_url", "https://api.linkup.so/v1/search"),
                timeout=int(linkup_config.get("timeout", 30)),
            )
        elif prov == "querit":
            return search_querit(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                language=args.language,
                country=args.country,
                time_range=args.time_range or args.freshness,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                base_url=args.querit_base_url,
                base_path=args.querit_base_path,
                timeout=int(querit_config.get("timeout", 30)),
            )
        elif prov == "exa":
            # CLI --exa-depth overrides; fallback to auto-routing suggestion
            exa_depth = args.exa_depth
            if exa_depth == "normal" and routing_info.get("exa_depth") in ("deep", "deep-reasoning"):
                exa_depth = routing_info["exa_depth"]
            return search_exa(
                query=args.query or "",
                api_key=key,
                max_results=args.max_results,
                search_type=args.exa_type,
                exa_depth=exa_depth,
                category=args.category,
                start_date=args.start_date,
                end_date=args.end_date,
                similar_url=args.similar_url,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                text_verbosity=args.exa_verbosity,
            )
        elif prov == "firecrawl":
            firecrawl_config = config.get("firecrawl", {})
            return search_firecrawl(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                country=firecrawl_config.get("country", args.country),
                time_range=args.time_range or args.freshness,
                sources=args.firecrawl_sources,
                include_domains=args.include_domains,
                exclude_domains=args.exclude_domains,
                scrape_markdown=args.firecrawl_scrape or args.raw_content,
                ignore_invalid_urls=firecrawl_config.get("ignore_invalid_urls", False),
                api_url=firecrawl_config.get("api_url", "https://api.firecrawl.dev/v2/search"),
                timeout_ms=int(firecrawl_config.get("timeout", 30000)),
            )
        elif prov in {"perplexity", "kilo-perplexity"}:
            perplexity_config = config.get(prov, {})
            defaults = DEFAULT_CONFIG.get(prov, {})
            return search_perplexity(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                model=perplexity_config.get("model", defaults.get("model", "sonar-pro")),
                api_url=perplexity_config.get("api_url", defaults.get("api_url", "https://api.perplexity.ai/chat/completions")),
                freshness=getattr(args, "freshness", None),
                provider_name=prov,
            )
        elif prov == "you":
            return search_you(
                query=args.query,
                api_key=key,
                max_results=args.max_results,
                country=args.country,
                language=args.language,
                freshness=args.freshness,
                safesearch=args.you_safesearch,
                include_news=not args.no_news,
                livecrawl=args.livecrawl,
            )
        elif prov == "searxng":
            # For SearXNG, 'key' is actually the instance URL
            instance_url = args.searxng_url or key
            if instance_url:
                instance_url = _validate_searxng_url(instance_url)
            return search_searxng(
                query=args.query,
                instance_url=instance_url,
                max_results=args.max_results,
                categories=args.categories,
                engines=args.engines,
                language=args.language,
                time_range=args.time_range,
                safesearch=args.searxng_safesearch,
            )
        else:
            raise ValueError(f"Unknown provider: {prov}")

    def execute_with_retry(prov: str) -> Dict[str, Any]:
        return execute_provider_with_retry(prov, lambda: execute_search(prov))

    cache_context = {
        "locale": f"{args.country}:{args.language}",
        "freshness": args.freshness,
        "time_range": args.time_range,
        "include_domains": sorted(args.include_domains) if args.include_domains else None,
        "exclude_domains": sorted(args.exclude_domains) if args.exclude_domains else None,
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

    providers_considered = providers_to_try.copy()

    if args.mode == "research":
        available_research_providers = {
            p for p in providers_to_try
            if p not in disabled_providers and _provider_auto_allowed(p, auto_config) and get_api_key(p, config) and not provider_in_cooldown(p)[0]
        }
        if provider and get_api_key(provider, config) and not provider_in_cooldown(provider)[0]:
            available_research_providers.add(provider)
        if args.research_providers:
            research_providers = [
                p for p in args.research_providers
                if p not in disabled_providers and _provider_auto_allowed(p, auto_config) and get_api_key(p, config) and not provider_in_cooldown(p)[0]
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
            print(json.dumps(error_result, indent=2), file=sys.stderr)
            sys.exit(1)

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
        result["quality_report"] = build_quality_report(
            query=args.query,
            result=result,
            routing_info=routing_info,
            providers_considered=providers_considered,
            eligible_providers=research_providers,
            cooldown_skips=cooldown_skips,
            errors=result.get("routing", {}).get("provider_errors", []),
        )
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False))
        return

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
            reset_provider_health(current_provider)
            successful_results.append((current_provider, provider_result))
            successful_provider = current_provider

            # If we have enough results, stop.
            if len(provider_result.get("results", [])) >= args.max_results:
                break

            # Only continue collecting from lower-priority providers when fallback was needed.
            if not errors:
                break
        except Exception as e:
            error_msg = str(e)
            cooldown_info = mark_provider_failure(current_provider, error_msg)
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

        result["routing"] = routing_info

        if not cache_hit and not args.no_cache and args.query:
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

        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, ensure_ascii=False))
    else:
        error_result = {
            "error": "All providers failed",
            "provider": provider,
            "query": args.query,
            "routing": routing_info,
            "provider_errors": errors,
            "cooldown_skips": cooldown_skips,
        }
        print(json.dumps(error_result, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
