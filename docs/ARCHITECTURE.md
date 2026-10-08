# web-search-plus Architecture

This document explains the runtime shape of `web-search-plus`: what runs locally, what leaves the machine, how routing works, and where the safety/cost boundaries are. It is deliberately plain. Fancy diagrams are nice; lying less is nicer.

## System boundary

`web-search-plus` is a Hermes Agent plugin. Hermes calls the plugin tools, and the plugin calls configured provider APIs.

```text
Hermes Agent
  → plugin tool schema and handler in __init__.py
  → engine package wsp_core/ (routing, providers, cache, extraction)
  → configured external provider APIs
  → normalized result returned to Hermes
```

The plugin does not run a separate hosted backend. It does not add an analytics service. It does not make searches private from the provider you configured. Queries and URLs sent to external providers leave the machine by definition.

## Main files

- `plugin.yaml`: plugin manifest, optional environment variables, onboarding commands, and tool declarations.
- `__init__.py`: Hermes plugin entrypoint, tool schemas, setup/onboarding helpers, and wrapper functions exposed to Hermes.
- `wsp_core/`: the engine, host-neutral and imported relatively, so it works as `hermes_plugins.<slug>.wsp_core` and can be shared with other hosts unchanged.
  - `wsp_core/search.py`: search pipeline entry points and the CLI.
  - `wsp_core/providers.py`: provider adapters; `wsp_core/routing.py`: auto-routing.
  - `wsp_core/provider_registry.py`: data-only provider metadata registry (single source of truth).
  - `wsp_core/provider_dispatch.py`: registry-driven `SEARCH_DISPATCH`/`EXTRACT_DISPATCH` adapter tables.
  - `wsp_core/sdk/`: the Provider SDK implementation behind the public `wsp_sdk` name.
- `search.py`: the documented command line (`python search.py --query ...`); it only calls `wsp_core.search.main`.
- `wsp_sdk/`: public Provider SDK import name. `providers.d` modules import `wsp_sdk`; the engine binds that name to its own `wsp_core.sdk` during discovery.
- `setup.py`: thin standalone CLI entrypoint that loads the plugin package and its setup helpers.
- `tests/`: unit and regression coverage for providers, onboarding, routing, extraction, and docs-sensitive configuration.

Default file locations are relative to the plugin directory and did not change when the engine moved into `wsp_core/`: cache and runtime state in `../.cache`, behaviour config in `../config.json`, keys in the plugin `.env`, the parent `.env` and the Hermes profile `.env`.

## Tool surface

The plugin exposes two tools:

- `web_search_plus`: routed or forced-provider search.
- `web_extract_plus`: URL extraction through extraction-capable providers.

## Provider abstraction

Each provider adapter normalizes provider-specific request and response details into a common result shape. Providers declare capabilities in docs and onboarding metadata, but provider APIs are still heterogeneous.

Provider capability classes:

- Search-only: Brave, SearXNG, SerpBase, and Querit. Brave participates in the default auto-pool at priority 7; SerpBase and Querit default to `auto_allow=false` and are explicit/guarded unless users opt in.
- Search and extraction: You.com, Serper, Firecrawl, Tavily, Exa, Linkup, Parallel, Keenable, and the optional local DonSeTch MCP sidecar. Serper extraction uses its webpage scraper (`scrape.serper.dev`) and sits last in the default auto-extraction fallback chain. DonSeTch defaults to `auto_allow=false` for both capabilities and is explicit-only unless deliberately enabled.

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
    "provider_priority": ["you", "serper", "exa", "firecrawl", "tavily", "linkup", "parallel", "brave", "serpbase", "querit", "searxng", "keenable"],
    "extract_provider_priority": ["tavily", "exa", "linkup", "parallel", "firecrawl", "you", "keenable", "serper"],
    "disabled_providers": [],
    "auto_allow": {
      "serpbase": false,
      "querit": false,
      "octen": false,
      "tinyfish": false,
      "donsetch": false
    },
    "confidence_threshold": 0.3
  }
}
```

`confidence_threshold` is accepted for compatibility but has no effect since 5.0.

Secrets and routing are separate so users can configure a provider key without automatically letting that provider receive automatic traffic. Search `provider_priority` and `extract_provider_priority` are independent: search ranking does not silently reorder URL extraction. A partial extraction list is normalized and completed with missing extract-capable providers in registry order.

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
  "querit": false,
  "octen": false,
  "tinyfish": false,
  "donsetch": false
}
```

With this config:

- `provider="serpbase"` can work when `SERPBASE_API_KEY` is present.
- `provider="donsetch"` can work when `DONSETCH_BIN` is present.
- `provider="auto"` will not select SerpBase, Querit, Octen, TinyFish, or DonSeTch unless opted in.
- Brave and Parallel join automatic routing when their keys are configured.
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

- Automatic search tries the routed provider first and keeps the other eligible providers as fallbacks. The next one starts as soon as the current one fails or answers with no results, or when it is slower than its usual latency (the 75th percentile of its recent calls, at least `v3.hedge_min_delay_seconds`, default 2.5 s). At most two run at once; the first non-empty answer wins. If every provider answers empty, the empty answer of the routed provider is returned.
- While a fallback exists, each provider gets one try with a socket timeout of `v3.attempt_timeout_seconds` (default 10 s). An explicitly requested provider without fallback keeps one retry for transient errors (`429`, `503`, timeouts) and its own timeout.
- Repeated provider failures open a circuit for that provider and update provider health state and cooldown.
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

Provider wiring is registry-driven. `wsp_core/provider_registry.py` is the data-only
single source of truth (id, env var, capabilities, onboarding metadata,
`auto_allow` default), and `wsp_core/provider_dispatch.py` maps each provider id to a
search/extract adapter in `SEARCH_DISPATCH` / `EXTRACT_DISPATCH`. CLI choices,
tool-schema enums, doctor output, onboarding, and extraction priority all
derive from the registry, and completeness tests
(`tests/test_provider_dispatch.py`) fail if a registry entry has no dispatch
adapter or vice versa — a new provider can no longer be silently forgotten on
one surface.

A provider addition should include:

- a `ProviderSpec` entry in `wsp_core/provider_registry.py` (id, env var, capabilities, `auto_allow` default)
- provider function(s) in `wsp_core/providers.py` (`search_<provider>`, optionally `extract_<provider>`)
- a dispatch adapter per capability in `wsp_core/provider_dispatch.py`, registered in `SEARCH_DISPATCH`/`EXTRACT_DISPATCH`; dispatch resolves functions from `wsp_core/providers.py`
- routing score/match behavior in `wsp_core/routing.py` if it participates in auto-routing
- docs in README, User Guide, FAQ, and Architecture when behavior is user-visible (`docs/PROVIDERS.md` regenerates from the registry)
- tests for response normalization and missing-key behavior (dispatch/enum/onboarding completeness is enforced by existing registry-driven tests)

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
