# web-search-plus Architecture

This document explains the runtime shape of `web-search-plus`: what runs locally, what leaves the machine, how routing works, and where the safety/cost boundaries are. It is deliberately plain. Fancy diagrams are nice; lying less is nicer.

## System boundary

`web-search-plus` is a Hermes Agent plugin. Hermes calls the plugin tools, and the plugin calls configured provider APIs.

```text
Hermes Agent
  → plugin tool schema and handler in __init__.py
  → provider/routing engine in search.py
  → configured external provider APIs
  → normalized result returned to Hermes
```

The plugin does not run a separate hosted backend. It does not add an analytics service. It does not make searches private from the provider you configured. Queries and URLs sent to external providers leave the machine by definition.

## Main files

- `plugin.yaml`: plugin manifest, optional environment variables, onboarding commands, and tool declarations.
- `__init__.py`: Hermes plugin entrypoint, tool schemas, setup/onboarding helpers, and wrapper functions exposed to Hermes.
- `search.py`: provider adapters, routing, caching, cooldowns, extraction, and CLI.
- `setup.py`: thin standalone CLI entrypoint that loads setup helpers from `__init__.py`.
- `tests/`: unit and regression coverage for providers, onboarding, routing, extraction, and docs-sensitive configuration.

## Tool surface

The plugin exposes two tools:

- `web_search_plus`: routed or forced-provider search.
- `web_extract_plus`: URL extraction through extraction-capable providers.

## Provider abstraction

Each provider adapter normalizes provider-specific request and response details into a common result shape. Providers declare capabilities in docs and onboarding metadata, but provider APIs are still heterogeneous.

Provider capability classes:

- Search-only: Brave, Serper, Perplexity, Kilo Perplexity, SearXNG, SerpBase, Querit. Brave, Perplexity/Kilo Perplexity, SerpBase, and Querit default to `auto_allow=false` and are explicit/guarded unless users opt in.
- Search and extraction: You.com, Firecrawl, Tavily, Exa, Linkup.
- Answer-style search: Perplexity and Kilo Perplexity return direct-answer style search results, but default auto-routing treats them as guarded providers rather than fast search providers.

Provider pricing, freshness, ranking, localization, and vertical support are controlled by the providers. The plugin normalizes responses; it does not make providers equivalent.

## Configuration model

There are two config layers:

- Secrets: provider keys in `.env`.
- Behavior: routing config in `config.json`.

Default routing config includes:

```json
{
  "auto_routing": {
    "enabled": true,
    "fallback_provider": "serper",
    "provider_priority": ["you", "serper", "exa", "firecrawl", "tavily", "linkup", "parallel", "brave", "serpbase", "querit", "kilo-perplexity", "perplexity", "searxng"],
    "disabled_providers": [],
    "auto_allow": {
      "serpbase": false,
      "querit": false,
      "brave": false,
      "parallel": false,
      "kilo-perplexity": false,
      "perplexity": false
    },
    "confidence_threshold": 0.3
  }
}
```

Secrets and routing are separate so users can configure a provider key without automatically letting that provider receive automatic traffic.

## Routing engine

Routing is rule-based. It is not ML and it is not magic.

High-level flow:

1. Analyze query text for signals: current-info intent, product/local intent, research language, direct-answer intent, semantic-discovery intent, privacy intent, complexity, recency, language/script hints, and benchmark-derived query classes.
2. Score known providers for those signals.
3. Apply conservative Routing v2 boosts and penalties for classes such as multilingual current queries, AT/local shopping, GitHub/docs, package/API docs, arXiv/academic, Reddit/community, CVE/security, official/regulatory, finance/IR, weather/local factual, and briefing/synthesis.
4. Remove providers that do not have a key or required local config.
5. Remove providers listed in `disabled_providers`.
6. Remove providers with `auto_allow=false` from automatic routing.
7. Choose the highest-scoring remaining provider.
8. Break ties deterministically using query text and `provider_priority`.
9. Execute the provider call with retry/cooldown handling.
10. Return quality diagnostics if requested.

When no provider is eligible, the router reports `no_available_providers` and falls back to the configured fallback provider path. If that provider has no key, the call fails visibly instead of inventing results.

## Auto-allow gate

`auto_allow` controls automatic routing and fallback eligibility. It does not control explicit provider calls.

Example:

```json
"auto_allow": {
  "serpbase": false,
  "parallel": false,
  "querit": false,
  "brave": false,
  "kilo-perplexity": false,
  "perplexity": false
}
```

With this config:

- `provider="serpbase"` can work when `SERPBASE_API_KEY` is present.
- `provider="parallel"` can work when `PARALLEL_API_KEY` is present.
- `provider="auto"` will not select guarded providers such as SerpBase or Parallel unless opted in.
- fallback lists will not silently choose guarded providers.
- `quality_report` can surface guarded providers under `auto_allow_excluded`.

Opt in:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
```

Opt out:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
```

The gate exists for providers where silent automatic use could surprise users because of pricing, maturity, reliability, coverage, terms, latency, or result style. Guarded providers are documented as explicit-opt-in providers, not as broken or disabled providers.

## Fallback and failure semantics

The plugin favors partial truth over fake certainty.

- Provider calls use shared transient-error retry behavior.
- Transient HTTP codes include `429` and `503`.
- Retry backoff is short and bounded.
- Repeated provider failures update provider health state and cooldown.
- Cooldowns step through 1 minute, 5 minutes, 25 minutes, and 1 hour.
- Cooldown state is local and stored in `provider_health.json` under the cache directory.
- Research mode keeps partial provider results when later extraction or provider calls fail.


A provider outage should produce skipped-provider metadata or a structured error, not a confident hallucination.

## Caching

Search results are cached locally by query, provider, result count, and relevant parameters.

- Default TTL: 1 hour.
- Cache key: SHA-256 hash prefix over normalized query/provider/max-results/params payload.
- Cache directory: `WSP_CACHE_DIR` if set; otherwise the plugin cache directory.
- CLI bypass: `--no-cache`.
- Cache write failures are non-fatal and reported to stderr.
- Corrupted or expired cache entries are removed on read.

The cache stores result payloads. Do not put the cache directory in version control.

## Cost and budget model

The plugin has cost guards, not provider billing control.

- `count` limits requested search result count.
- Research mode has a best-effort `research_time_budget` checked between provider and extraction steps.
- Extraction provider order is bounded and explicit.
- `disabled_providers`, `set-default`, and `auto_allow` let users control which providers can receive traffic.

The plugin does not know every provider account's exact current price, quota, or billing state. Provider dashboards remain the source of truth.

## Observability and debugging

Use quality reports for routing diagnostics:

```python
web_search_plus(query="best bookshelf speakers under 1000", quality_report=True)
```

CLI equivalent:

```bash
python3 search.py --query "best bookshelf speakers under 1000" --provider auto --quality-report --compact
```

Useful fields include:

- selected provider
- provider scores
- confidence and reason
- skipped providers
- cooldown remaining seconds
- provider errors
- `auto_allow_excluded`
- extraction recommendation

Setup/status diagnostics:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py status --json
python ~/.hermes/plugins/web-search-plus/setup.py config show --json
```

## Data flow and trust boundary

Data sent to providers depends on the tool:

- `web_search_plus`: sends the query and provider-specific search parameters.
- `web_extract_plus`: sends URLs and extraction options.


The plugin does not promise “no data leaves your machine.” A more accurate statement is: data only goes to providers you configure or explicitly select, plus local cache/state files written by the plugin.

## Extending with a new provider

A provider addition should include:

- provider adapter function in `search.py`
- API key mapping in runtime and onboarding code
- provider metadata in setup/list/status output
- routing score/match behavior if it participates in auto-routing
- explicit default `auto_allow` decision
- docs in README, User Guide, FAQ, and Architecture when behavior is user-visible
- tests for response normalization, missing-key behavior, routing eligibility, onboarding metadata, and CLI/provider choices

Default stance: new or surprising providers should start explicit-only until their cost and quality characteristics are boring enough for automatic fallback.

## Non-goals

`web-search-plus` is not:

- a paywall bypass tool
- an auth-walled content extractor
- a guarantee of Google/Bing parity
- an unlimited scraping service
- an SLA-backed hosted search backend
- a privacy shield from external providers
- a legal/medical/financial citation verifier
- a replacement for live provider billing dashboards

## Safe claims to make

Use precise wording:

- “multi-provider search and extraction”
- “capability-based provider setup”
- “best-effort routing diagnostics”
- “explicit opt-in for automatic use of gated providers”
- “bounded research and extraction fanout”
- “local cache and cooldown state”

Avoid claims like “always best,” “private,” “real-time guaranteed,” “unlimited,” or “verified citations.” Those are marketing confetti. Pretty, useless, and gets everywhere.
