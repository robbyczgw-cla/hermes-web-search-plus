"""Registry-driven provider dispatch tables for Web Search Plus.

This module replaces the historical if/elif provider chains in ``search.py``
(``execute_search``) and ``extract.py`` (``execute_extract``) with explicit
per-provider adapters registered in ``SEARCH_DISPATCH`` / ``EXTRACT_DISPATCH``.
Each adapter encapsulates exactly the provider-specific kwargs-building the
old chain branch did, so behaviour is unchanged.

Monkeypatch seam contract (do not early-bind provider functions here):
tests patch provider functions on the *calling module* (for example
``mock.patch.object(search, "search_you", ...)`` or
``mock.patch("search.extract_firecrawl", ...)``). Adapters therefore receive
the caller's namespace (a module object or its ``globals()`` dict) and resolve
``search_<provider>`` / ``extract_<provider>`` late on every call — the same
late-resolution pattern ``bench.py`` uses via its ``search_module`` seam.

``provider_registry.py`` stays data-only by design; the callable wiring lives
here. tests/test_provider_dispatch.py enforces that these tables and the
registry capability flags can never drift apart.
"""

from typing import Any, Callable, Dict

from config import DEFAULT_CONFIG, _validate_searxng_url, keyless_public_allowed
from search_locale import resolve_locale


def _resolve(namespace: Any, name: str) -> Callable[..., Dict[str, Any]]:
    """Late-resolve a provider function from the caller's namespace.

    Accepts either a module object (``getattr`` lookup, like bench.py's
    ``search_module`` seam) or a ``globals()`` dict, so callers loaded under
    non-standard module names (spec_from_file_location in tests) work too.
    """
    if isinstance(namespace, dict):
        return namespace[name]
    return getattr(namespace, name)


def _locale(prov: str, args: Any, config: Dict[str, Any]):
    """Resolve the effective (country, language) for a locale-aware provider.

    CLI flags arrive through ``args.country``/``args.language`` (None unless
    explicitly passed); everything else — explicit provider config, query
    location hints, ``defaults.locale``, us/en fallback — is resolved centrally
    in search_locale.resolve_locale.
    """
    country, language, _meta = resolve_locale(
        prov,
        config,
        getattr(args, "query", None),
        cli_country=getattr(args, "country", None),
        cli_language=getattr(args, "language", None),
    )
    return country, language


# =============================================================================
# Search adapters — one per provider, kwargs identical to the old
# search.py execute_search() if/elif branches.
# =============================================================================


def _call_serper_search(search_module, prov, args, key, config, routing_info):
    country, language = _locale(prov, args, config)
    return _resolve(search_module, "search_serper")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        country=country,
        language=language,
        search_type=args.search_type,
        time_range=args.time_range or args.freshness,
        include_images=args.images,
    )


def _call_serpbase_search(search_module, prov, args, key, config, routing_info):
    serpbase_config = config.get("serpbase", {})
    country, language = _locale(prov, args, config)
    return _resolve(search_module, "search_serpbase")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        country=country,
        language=language,
        page=int(serpbase_config.get("page", 1)),
        api_url=serpbase_config.get("api_url", "https://api.serpbase.dev/google/search"),
        timeout=int(serpbase_config.get("timeout", 30)),
    )


def _call_brave_search(search_module, prov, args, key, config, routing_info):
    brave_config = config.get("brave", {})
    country, language = _locale(prov, args, config)
    return _resolve(search_module, "search_brave")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        country=country,
        language=language,
        time_range=args.time_range or args.freshness,
        safesearch=brave_config.get("safesearch", "moderate"),
    )


def _call_tavily_search(search_module, prov, args, key, config, routing_info):
    return _resolve(search_module, "search_tavily")(
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


def _call_linkup_search(search_module, prov, args, key, config, routing_info):
    linkup_config = config.get("linkup", {})
    return _resolve(search_module, "search_linkup")(
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


def _call_querit_search(search_module, prov, args, key, config, routing_info):
    querit_config = config.get("querit", {})
    country, language = _locale(prov, args, config)
    return _resolve(search_module, "search_querit")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        language=language,
        country=country,
        time_range=args.time_range or args.freshness,
        include_domains=args.include_domains,
        exclude_domains=args.exclude_domains,
        base_url=args.querit_base_url,
        base_path=args.querit_base_path,
        timeout=int(querit_config.get("timeout", 30)),
    )


def _call_exa_search(search_module, prov, args, key, config, routing_info):
    # CLI --exa-depth overrides; fallback to auto-routing suggestion
    exa_depth = args.exa_depth
    if exa_depth == "normal" and routing_info.get("exa_depth") in ("deep", "deep-reasoning"):
        exa_depth = routing_info["exa_depth"]
    return _resolve(search_module, "search_exa")(
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


def _call_firecrawl_search(search_module, prov, args, key, config, routing_info):
    firecrawl_config = config.get("firecrawl", {})
    country, _language = _locale(prov, args, config)
    return _resolve(search_module, "search_firecrawl")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        country=country,
        time_range=args.time_range or args.freshness,
        sources=args.firecrawl_sources,
        include_domains=args.include_domains,
        exclude_domains=args.exclude_domains,
        scrape_markdown=args.firecrawl_scrape or args.raw_content,
        ignore_invalid_urls=firecrawl_config.get("ignore_invalid_urls", False),
        api_url=firecrawl_config.get("api_url", "https://api.firecrawl.dev/v2/search"),
        timeout_ms=int(firecrawl_config.get("timeout", 30000)),
    )


def _call_parallel_search(search_module, prov, args, key, config, routing_info):
    parallel_config = config.get("parallel", {})
    return _resolve(search_module, "search_parallel")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        include_domains=args.include_domains,
        exclude_domains=args.exclude_domains,
        api_url=parallel_config.get("api_url", "https://api.parallel.ai/v1/search"),
        timeout=int(parallel_config.get("timeout", 45)),
        client_model=parallel_config.get("client_model"),
    )


def _call_perplexity_search(search_module, prov, args, key, config, routing_info):
    """Shared adapter for both ``perplexity`` and ``kilo-perplexity``."""
    perplexity_config = config.get(prov, {})
    defaults = DEFAULT_CONFIG.get(prov, {})
    return _resolve(search_module, "search_perplexity")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        model=perplexity_config.get("model", defaults.get("model", "sonar-pro")),
        api_url=perplexity_config.get("api_url", defaults.get("api_url", "https://api.perplexity.ai/chat/completions")),
        freshness=getattr(args, "freshness", None),
        provider_name=prov,
    )


def _call_you_search(search_module, prov, args, key, config, routing_info):
    country, language = _locale(prov, args, config)
    return _resolve(search_module, "search_you")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        country=country,
        language=language,
        freshness=args.freshness,
        safesearch=args.you_safesearch,
        include_news=not args.no_news,
        livecrawl=args.livecrawl,
    )


def _call_searxng_search(search_module, prov, args, key, config, routing_info):
    # For SearXNG, 'key' is actually the instance URL
    instance_url = args.searxng_url or key
    if instance_url:
        instance_url = _validate_searxng_url(instance_url)
    _country, language = _locale(prov, args, config)
    return _resolve(search_module, "search_searxng")(
        query=args.query,
        instance_url=instance_url,
        max_results=args.max_results,
        categories=args.categories,
        engines=args.engines,
        language=language,
        time_range=args.time_range or args.freshness,
        safesearch=args.searxng_safesearch,
    )


def _call_keenable_search(search_module, prov, args, key, config, routing_info):
    keenable_config = config.get("keenable", {})
    return _resolve(search_module, "search_keenable")(
        query=args.query,
        api_key=key,
        max_results=args.max_results,
        time_range=args.time_range or args.freshness,
        include_domains=args.include_domains,
        public=keyless_public_allowed(prov, config),
        api_url=keenable_config.get("search_url", "https://api.keenable.ai/v1/search"),
        timeout=int(keenable_config.get("timeout", 30)),
    )


# Adapter signature: (search_module, provider, args, key, config, routing_info) -> result dict.
SEARCH_DISPATCH: Dict[str, Callable[..., Dict[str, Any]]] = {
    "serper": _call_serper_search,
    "serpbase": _call_serpbase_search,
    "brave": _call_brave_search,
    "tavily": _call_tavily_search,
    "querit": _call_querit_search,
    "linkup": _call_linkup_search,
    "exa": _call_exa_search,
    "firecrawl": _call_firecrawl_search,
    "parallel": _call_parallel_search,
    "perplexity": _call_perplexity_search,
    "kilo-perplexity": _call_perplexity_search,
    "you": _call_you_search,
    "searxng": _call_searxng_search,
    "keenable": _call_keenable_search,
}


# =============================================================================
# Extract adapters — one per provider, kwargs identical to the old
# extract.py execute_extract() if-chain branches.
# =============================================================================


def _call_firecrawl_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    fc = config.get("firecrawl", {})
    return _resolve(extract_module, "extract_firecrawl")(urls, key, output_format, include_images, include_raw_html, render_js, api_url=fc.get("scrape_url", "https://api.firecrawl.dev/v2/scrape"), timeout=int(fc.get("extract_timeout", 60)))


def _call_linkup_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    lu = config.get("linkup", {})
    return _resolve(extract_module, "extract_linkup")(urls, key, output_format, include_images, include_raw_html, render_js, api_url=lu.get("fetch_url", "https://api.linkup.so/v1/fetch"), timeout=int(lu.get("timeout", 30)))


def _call_tavily_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    tv = config.get("tavily", {})
    return _resolve(extract_module, "extract_tavily")(urls, key, output_format, include_images, include_raw_html, render_js, api_url=tv.get("extract_url", "https://api.tavily.com/extract"), timeout=int(tv.get("timeout", 30)))


def _call_exa_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    exa = config.get("exa", {})
    return _resolve(extract_module, "extract_exa")(urls, key, output_format, include_images, include_raw_html, render_js, api_url=exa.get("contents_url", "https://api.exa.ai/contents"), timeout=int(exa.get("timeout", 30)))


def _call_parallel_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    parallel = config.get("parallel", {})
    return _resolve(extract_module, "extract_parallel")(
        urls, key, output_format, include_images, include_raw_html, render_js,
        api_url=parallel.get("extract_url", "https://api.parallel.ai/v1/extract"),
        timeout=int(parallel.get("extract_timeout", parallel.get("timeout", 60))),
        client_model=parallel.get("client_model"),
        # None scales the batch budget with the URL count (len(urls) * per-result)
        # inside extract_parallel; explicit config values pin exact limits.
        max_chars_total=int(parallel["max_chars_total"]) if parallel.get("max_chars_total") else None,
        max_chars_per_result=int(parallel.get("max_chars_per_result") or 60000),
    )


def _call_keenable_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    kn = config.get("keenable", {})
    return _resolve(extract_module, "extract_keenable")(urls, key, output_format, include_images, include_raw_html, render_js, public=keyless_allowed, api_url=kn.get("fetch_url", "https://api.keenable.ai/v1/fetch"), timeout=int(kn.get("timeout", 30)))


def _call_serper_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    sp = config.get("serper", {})
    return _resolve(extract_module, "extract_serper")(urls, key, output_format, include_images, include_raw_html, render_js, api_url=sp.get("scrape_url", "https://scrape.serper.dev"), timeout=int(sp.get("extract_timeout", sp.get("timeout", 30))))


def _call_you_extract(extract_module, prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed):
    you = config.get("you", {})
    return _resolve(extract_module, "extract_you")(urls, key, output_format, include_images, include_raw_html, render_js, api_url=you.get("contents_url", "https://ydc-index.io/v1/contents"), timeout=int(you.get("timeout", 30)))


# Adapter signature: (extract_module, provider, urls, key, output_format,
# include_images, include_raw_html, render_js, config, keyless_allowed) -> result dict.
EXTRACT_DISPATCH: Dict[str, Callable[..., Dict[str, Any]]] = {
    "firecrawl": _call_firecrawl_extract,
    "linkup": _call_linkup_extract,
    "tavily": _call_tavily_extract,
    "exa": _call_exa_extract,
    "parallel": _call_parallel_extract,
    "keenable": _call_keenable_extract,
    "you": _call_you_extract,
    "serper": _call_serper_extract,
}
