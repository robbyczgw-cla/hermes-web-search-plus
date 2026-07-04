# web-search-plus User Guide

This guide is the long-form operating manual for `web-search-plus`. If you only need the first install, start with the [README Quick Start](../README.md#quick-start). Come back here when you want to tune providers, routing, fallback, or extraction without guessing.

## What this plugin does

`web-search-plus` adds two Hermes tools:

- `web_search_plus` for routed multi-provider web search.
- `web_extract_plus` for clean URL extraction.

The plugin is capability-based. You do not need every provider key. One search-capable key is enough for search; one extraction-capable key unlocks URL extraction.

## Installation and first-run checks

Install and enable the plugin:

```bash
hermes plugins install robbyczgw-cla/hermes-web-search-plus --enable
```

Check status and configure keys:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py status
python ~/.hermes/plugins/web-search-plus/setup.py setup
```

Restart or reset Hermes after changing keys so the tool schemas and environment are reloaded:

```text
CLI: exit and start hermes again, or use /reset in-session
Gateway/Telegram: /restart, then /reset
```

Smoke test from the plugin directory:

```bash
cd ~/.hermes/plugins/web-search-plus
python3 search.py --query "Hermes Agent latest release" --provider auto --quality-report --compact
```

Inspect whether the install is likely to hit the low-latency Hermes fast path:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py fastpath
python ~/.hermes/plugins/web-search-plus/setup.py fastpath --json
```

The fast-path checker is intentionally advisory and stdlib-only. It verifies that the plugin declares both tools for direct registration, that the standalone setup helper is available, and whether your Hermes `config.yaml` contains the current public-Hermes hint below. It does not require Hermes core patches.

```yaml
agent:
  disabled_toolsets: [web]
```

Use this when Web Search Plus should be the preferred web layer. Without it, Web Search Plus still works; Hermes may simply have more web-capable tools to choose from. Some forks/local builds may expose additional tool-pinning config, but this guide only documents options available in current public Hermes.

### Bench your providers

Once keys are configured, benchmark the providers against each other and get a data-backed `auto_routing.provider_priority` suggestion:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py bench
# or, from the plugin directory:
python3 search.py --bench
python3 search.py --bench --json   # structured report
```

The bench runs a small fixed query suite (docs, vendor release, community, non-English) against every configured search-capable provider and reports success rate, median latency, result volume, and simple quality signals (duplicate-free URLs, snippet coverage). Providers are ranked by a weighted score — reliability first, then speed, then quality — and the recommended priority is printed together with the exact `config set-priority` command to apply it.

Two guarantees worth knowing:

- Bench calls providers directly, so a bench run never triggers provider cooldowns and never feeds the adaptive routing statistics.
- Nothing is written to your config; applying the recommendation is always an explicit step.

Note that the bench makes a few real API calls per provider, so it spends a small amount of quota on every configured provider.

## Provider setup

Keys live in the active Hermes environment file, normally `~/.hermes/.env`. The setup helper preserves existing entries and does not print secret values. See the generated [provider reference](PROVIDERS.md) for every provider's capabilities, env var, auto-routing default, free tier, and signup link.

Useful commands:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py list
python ~/.hermes/plugins/web-search-plus/setup.py status --json
python ~/.hermes/plugins/web-search-plus/setup.py setup --preset starter
python ~/.hermes/plugins/web-search-plus/setup.py setup you linkup --env-path ~/.hermes/.env
```

Presets:

- `starter`: You.com + Serper + Linkup. Best Routing v2 first-run setup.
- `lean`: You.com + Linkup. Small fast search plus extraction.
- `search`: You.com + Serper + Exa + Firecrawl + Tavily + Linkup. Full default Routing v2 pool.
- `extract`: Firecrawl + Linkup + Exa + Tavily. Extraction-heavy setup.
- `all`: prompt for every supported provider.

Search-capable providers include You.com, Serper, Exa, Firecrawl, Tavily, Linkup, Parallel, Brave, Perplexity, Kilo Perplexity, SearXNG, SerpBase, Querit, and Keenable. Extraction-capable providers are Linkup, Firecrawl, Tavily, Exa, Parallel, You.com, Keenable, and Serper.

Keenable is keyless: set `KEENABLE_API_KEY` for the authenticated endpoints, or opt into its public tier (off by default). In the wizard, skip the Keenable key prompt and answer yes, or run `setup.py setup keenable --keyless-public`; it writes `keenable.allow_public: true` to `config.json` (equivalently `KEENABLE_ALLOW_PUBLIC=1`).

With a `KEENABLE_API_KEY` set, requests always use the authenticated endpoints. Without a key and without the opt-in, Keenable is treated as unconfigured: it won't auto-route, fall back, or enable `web_extract_plus`. When the public tier is enabled, queries and fetched URLs are sent to an **unauthenticated** public service with per-IP limits and no SLA — roughly 1,000 requests/hour and 10 requests/second — so treat it as a best-effort last resort, not a dependable provider. The first request that uses the public endpoint logs a one-time warning so the egress is visible, and `web-search-plus doctor` reports keyless providers as `key=no` with a separate `keyless=on/off` badge so key status stays truthful.

### Migration note for v2.0.0

Routing v2 changes the default `provider="auto"` behavior. Existing configs keep explicit user choices, but missing `auto_allow` entries inherit the new guarded defaults: Brave, SerpBase, Querit, native Perplexity, and Kilo Perplexity stay explicit-only until you opt them into automatic routing.

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config show --json
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
```

## Routing preferences

For a generated class-by-class reference of what auto-routing prefers and demotes, see [Routing v2 Reference](ROUTING.md).

Secrets and behavior are intentionally separate:

- Provider keys live in `.env`.
- Routing behavior lives in `config.json`.
- `WEB_SEARCH_PLUS_CONFIG=/path/to/config.json` can point runtime search at a custom config.
- `setup.py --config-path /path/to/config.json` points the setup helper at a custom config.

Inspect routing:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config show --json
```

Pin a fixed provider:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default you
```

Turn query-based auto-routing back on:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-routing on
```

Tune automatic routing and fallback:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-priority you,serper,exa,firecrawl,tavily,linkup
python ~/.hermes/plugins/web-search-plus/setup.py config set-fallback serper
python ~/.hermes/plugins/web-search-plus/setup.py config disable perplexity
python ~/.hermes/plugins/web-search-plus/setup.py config enable perplexity
python ~/.hermes/plugins/web-search-plus/setup.py config set-threshold 0.45
```

Preview config changes without writing:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default you --dry-run
```

Semantics worth knowing:

- `set-default <provider>` disables auto-routing and makes `--provider auto` resolve to that provider; `set-routing on` restores query-based routing while keeping the saved default for later.
- `set-priority` accepts comma-separated provider names, normalizes case/whitespace, and ignores duplicates with a warning.
- `set-auto-allow <provider> off` keeps a configured provider available for explicit calls while preventing auto-routing/fallback from selecting it.
- `config reset --yes` backs up the existing file before writing fresh defaults.

### GroktoCrawl / local Firecrawl-compatible backends

Firecrawl search and extraction use configurable endpoint URLs. If you run a local Firecrawl-v2-compatible service such as [GroktoCrawl](https://github.com/groktopus/groktocrawl), point the existing `firecrawl` provider at that service instead of adding a new provider name:

```json
{
  "firecrawl": {
    "api_url": "http://127.0.0.1:8080/v2/search",
    "scrape_url": "http://127.0.0.1:8080/v2/scrape"
  }
}
```

The backend still receives the same bearer header WSP sends for Firecrawl, so set `FIRECRAWL_API_KEY` when the local service requires authentication. This is an operator-controlled override: WSP keeps the default Firecrawl cloud URLs unless you set these config values. The GroktoCrawl path has been smoke-tested for search and scrape/extract response compatibility, but monitor your own timeout, pagination, and rate-limit behavior before relying on it for production crawls.

This local endpoint override is separate from the safety check on extraction **target** URLs. `web_extract_plus` rejects private/internal targets by default, including CGNAT/shared-address ranges, IPv6 ULA/link-local/mapped-private addresses, multicast, cloud metadata, and DNS answers that point inward. It still allows the operator-configured local `firecrawl.scrape_url` above. If you intentionally want to extract trusted intranet pages, opt in explicitly:

```json
{
  "extract": {
    "allow_private_urls": true
  }
}
```

The guard validates the initial extraction target before provider dispatch. If a local/self-hosted backend follows redirects itself, re-validating post-redirect targets is a provider-layer hardening follow-up.

### Routing debug walkthrough

When a query does not use the provider you expected, ask for routing diagnostics instead of guessing:

```bash
python3 search.py --query "best bookshelf speakers under 1000 EUR" --provider auto --quality-report --compact --no-cache
```

In the JSON output, check these fields first:

- `routing.provider`: the selected provider.
- `routing.reason`: why the router considered the match strong or weak.
- `scores`: provider scores before final selection.
- `quality_report.skipped_providers`: providers skipped because of cooldown or errors.
- `routing.auto_allow_excluded`: configured providers that were blocked from automatic routing by `auto_allow=false`.
- `quality_report.extraction_recommended`: whether snippets look thin enough that `web_extract_plus` may help.

Example pattern:

```json
{
  "routing": {
    "provider": "serper",
    "reason": "moderate_confidence_match",
    "routing_policy": "routing-v2",
    "routing_class": "shopping_at",
    "auto_allow_excluded": ["serpbase"]
  },
  "quality_report": {
    "skipped_providers": [
      {"provider": "brave", "reason": "cooldown", "cooldown_remaining_seconds": 42}
    ]
  }
}
```

Read that as: guarded providers can have keys but remain explicit-only for `provider="auto"`, and the router selected the best eligible provider. If you want SerpBase, Brave, Querit, Perplexity, or Kilo Perplexity to participate in automatic routing, opt in with `set-auto-allow <provider> on`; if a provider is cooled down, wait or clear local provider health state in your cache directory.

## Search locale defaults

Providers with region/language request parameters (Serper, Brave, You.com, SerpBase, Querit, Firecrawl, SearXNG) no longer hardcode us/en. Set your defaults once in `config.json`:

```json
{
  "defaults": {
    "locale": {
      "country": "at",
      "language": "auto"
    }
  }
}
```

- `country`: ISO 3166-1 alpha-2 code (for example `at`, `fr`, `es`) used as the default region for locale-aware providers.
- `language`: ISO 639-1 code (for example `de`), or `"auto"` to infer the language from each query.

`"auto"` mode uses a lightweight, local stopword/character heuristic (no LLM, no extra dependency, no IP geolocation) covering `de`, `es`, `fr`, `it`, `pt`, `nl`, and `en`. It is deliberately conservative: it needs at least two distinct language signals and a single unambiguous winner. A query like "Wiener Kaffeehaus Öffnungszeiten" infers German; a terse technical query like "DAC R2R NOS" or "PostgreSQL 17 release notes" infers nothing and keeps the default language.

Explicit location hints in the query move the country: a small curated table of well-known city and country names (Vienna/Wien, Berlin, Paris, Madrid, London, Rome, Amsterdam, ...) is checked, so "mejores restaurantes Madrid" searches with `country=es` and "boulangerie Paris horaires" with `country=fr` even when your configured default is `at`. Conflicting hints ("compare bakeries in Paris and Madrid") change nothing.

Resolution precedence:

1. CLI flags / tool parameters (`--country` / `--language`, or the `country` / `language` tool parameters)
2. Explicit provider-specific config in `config.json` (for example `serper.country` or `brave.search_lang`)
3. Explicit location hint in the query (country only)
4. `defaults.locale.country` / `defaults.locale.language` (with `"auto"` triggering language inference)
5. Fallback `us` / `en`

**Query language does not imply country.** A German query can come from Austria or Switzerland just as well as Germany, so inferred language never moves the region — only explicit location hints, configuration, or flags do.

Result metadata reports the resolved locale and where each value came from, following the freshness-metadata pattern:

```json
"locale": {
  "country": "at",
  "language": "de",
  "source": {"country": "config", "language": "inferred"}
}
```

Backward compatibility: without `defaults.locale` and without flags, everything still resolves to `us`/`en` exactly as before. Providers without locale parameters (Tavily, Exa, Linkup, Parallel, Perplexity, Keenable) are unaffected.

## Explicit opt-in providers: guarded providers

Some providers can be configured for explicit use without being selected automatically. That is what `auto_allow` controls.

Brave, SerpBase, Querit, native Perplexity, and Kilo Perplexity default to `auto_allow=false`. Setting their keys makes explicit calls work:

```python
web_search_plus(query="best DAC reviews", provider="serpbase")
web_search_plus(query="aktuelle KI-News Deutschland", provider="querit")
```

That does not make any guarded provider eligible for automatic routing or fallback until you opt in:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow querit on
```

Turn automatic use back off:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow querit off
```

This pattern avoids silent cost or coverage surprises. Use it for providers whose pricing, maturity, or result style you want to test before letting `provider="auto"` choose them.

## Using `web_search_plus`

Use `web_search_plus` when you need source discovery, current facts, prices, schedules, weather, sports, or provider diagnostics.

Examples:

```python
web_search_plus(query="Graz weather today")
web_search_plus(query="best bookshelf speakers under 1000 EUR", quality_report=True)
web_search_plus(query="alternatives to Notion", provider="exa")
web_search_plus(query="turntable reviews under 1000", mode="research", research_time_budget=45)
```

Parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | **required** | Search query |
| `provider` | string | `"auto"` | `auto`, `serper`, `brave`, `tavily`, `exa`, `linkup`, `firecrawl`, `parallel`, `perplexity`, `kilo-perplexity`, `you`, `searxng`, `serpbase`, `querit`, `keenable` |
| `depth` | string | `"normal"` | Exa only: `normal`, `deep`, `deep-reasoning` |
| `count` | integer | `5` | Results, 1–20 |
| `time_range` | string | — | `day`, `week`, `month`, `year` |
| `freshness` | string | — | Unified recency filter: `day`, `week`, `month`, `year` (case-insensitive) |
| `search_type` | string | `"search"` | Result vertical: `search` or `news` |
| `include_domains` | string[] | — | Restrict search to domains |
| `exclude_domains` | string[] | — | Exclude domains |
| `quality_report` | boolean | `false` | Include routing diagnostics, provider scores, result counts, authority signals, and extraction recommendation |
| `mode` | string | `"normal"` | `normal` or opt-in `research` |
| `research_time_budget` | number | `55.0` | Best-effort seconds budget for research mode |

Parameter semantics:

- `provider`: `auto`, or a concrete provider such as `you`, `serper`, `exa`, `firecrawl`, `tavily`, `linkup`, `brave`, `perplexity`, `kilo-perplexity`, `searxng`, `serpbase`, or `querit`. Brave, Parallel, Perplexity/Kilo Perplexity, SerpBase, and Querit are available for explicit calls but default to `auto_allow=false`.
- `count`: result count, from 1 to 20.
- `time_range`: `day`, `week`, `month`, or `year` where supported.
- `freshness`: unified recency filter with the values `day`, `week`, `month`, or `year` (case-insensitive; invalid values return a clear error). It is applied natively by Serper, Brave, Querit, Firecrawl, Keenable, You.com, Perplexity/Kilo Perplexity, and SearXNG, each translated into that provider's own format (for example Brave `pw` or Serper `tbs=qdr:w` for `week`). Providers without recency support (Tavily, Exa, Linkup, Parallel, SerpBase) still run the search normally; the result metadata then reports `"freshness": {"requested": "week", "applied": false, "reason": "provider tavily does not support freshness"}` instead of silently dropping the filter. In `mode="research"` the filter is passed to every participating provider and the applied status is reported per provider.
- `search_type`: result vertical, `search` (default) or `news` (case-insensitive; invalid values return a clear error). Serper serves the news vertical natively via `google.serper.dev/news` and returns article `date` and `source` fields; the unified `freshness` filter keeps working there. Providers without a native news vertical still run the normal search; the result metadata then reports `"search_type": {"requested": "news", "applied": false, "reason": "provider tavily does not support search_type news"}` instead of silently ignoring the request. In `mode="research"` the applied status is reported per provider.
- `include_domains` / `exclude_domains`: provider-dependent domain filters.
- `quality_report`: include routing diagnostics, skipped providers, result quality hints, and extraction recommendation.
- `mode="research"`: query multiple providers and optionally extract selected URLs within a best-effort wall-clock budget.

## Using `web_extract_plus`

Use `web_extract_plus` when you already have URLs and want page content, not just search snippets.

```python
web_extract_plus(urls=["https://example.com"], provider="firecrawl")
web_extract_plus(urls=["https://docs.linkup.so"], provider="linkup", render_js=False)
```

Parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `urls` | string[] | **required** | URLs to extract |
| `provider` | string | `"auto"` | `auto`, `tavily`, `exa`, `linkup`, `parallel`, `firecrawl`, `you`, `keenable`, `serper` |
| `format` | string | `"markdown"` | `markdown` or `html` |
| `include_images` | boolean | `false` | Include image metadata when supported |
| `include_raw_html` | boolean | `false` | Include raw HTML when supported |
| `render_js` | boolean | `false` | Render JavaScript before extraction when supported |

Auto extraction currently tries Tavily, Exa, Linkup, Parallel, Firecrawl, and You.com when keys are available, then Keenable (only if a key is set or its public tier is opted in), and finally Serper as the last resort. Tavily is the fast reliable default; Exa is the fast docs/academic backup; Linkup stays the clean long-form fallback; Firecrawl remains the robust scraper safety net; Serper's webpage scraper (`https://scrape.serper.dev`, overridable via config `serper.scrape_url`, timeout via `serper.extract_timeout`) closes the chain so a Serper-only setup still gets extraction.

Parallel extraction explicitly requests `full_content`. Its default budget is 60,000 characters per result and 120,000 characters total so long documents are not unfairly shortened compared with other extraction providers. Operators can override those request-side limits in `config.json` with `parallel.max_chars_per_result` and `parallel.max_chars_total`.

Large extracted pages are not returned as raw token bombs. `web_extract_plus` sanitizes inline base64 images, stores the full cleaned text under `cache/web`, and returns a bounded head/tail preview with a footer containing the stored file path plus an exact `read_file(path=..., offset=..., limit=500)` call for paging into the omitted middle. Configure the inline budget in `config.json`:

```json
{
  "web": {
    "extract_char_limit": 15000
  }
}
```

If the stored full text exceeds 2,000,000 characters, the stored file and footer both mark that cap explicitly.

The stored full text is local plaintext cache data. It may contain the complete cleaned contents of extracted pages, persists until cleared, and currently has no automatic TTL or total-size eviction. Use `python3 search.py --cache-stats` to inspect `web_text_entries`, `web_text_size_bytes`, and the combined cache size; use `python3 search.py --clear-cache` to remove both normal JSON cache entries and `cache/web/*.md` full-text files while preserving provider-health state. For privacy-sensitive or throwaway extraction runs, set `WSP_CACHE_DIR` to a disposable directory or clear the cache afterward.

## Result quality

Search results pass through a quality layer before they reach the agent:

- **Adaptive routing:** every real provider call records latency, error, and empty-result outcomes (rolling window, last 50 calls / 7 days). Routing blends a bounded adjustment (±1.0) into the scores, so providers that are currently fast and productive win close calls — strong query-class signals are never overridden. Disable with `auto_routing.adaptive_routing: false` in `config.json`; adjustments are visible in `quality_report.adaptive_adjustments`.
- **Spam/mirror filter:** results from known Stack Overflow/GitHub content mirrors and SEO scrapers are removed (reported in `metadata.spam_filtered`). Extend via `quality.blocked_domains`, rescue a domain via `quality.allowed_domains`, or disable with `quality.filter_spam: false`.
- **Domain diversity:** at most 2 results per domain keep their position; overflow is moved behind the diverse head (`quality.max_results_per_domain`, `0` disables).
- **Explicit intent wins:** queries with `site:` operators or `include_domains` are exempt — constrained domains bypass the spam filter and the diversity rerank is skipped entirely.

## Reliability and cost controls

The plugin is designed to fail visibly rather than invent confidence.

- Search result cache TTL is 1 hour by default.
- Cache files and provider health state live under `WSP_CACHE_DIR`, or the plugin cache directory if unset.
- Use `--no-cache` in CLI tests when you need a fresh provider call.
- Transient provider errors are retried with short backoff.
- Repeated provider failures put that provider on cooldown, stepping from 1 minute to 5 minutes to 25 minutes to 1 hour.
- Research mode checks `research_time_budget` between provider calls and extraction steps; it is best-effort, not a provider-side billing limit.
- Missing extraction keys, empty results, quota failures, and budget exhaustion are returned as warnings or metadata where possible.

The plugin cannot normalize or guarantee provider pricing. Provider APIs own their own billing, rate limits, index freshness, and terms.

## Updating

Update the plugin with Hermes’ plugin workflow or by pulling the installed clone, then restart/reset Hermes:

```bash
cd ~/.hermes/plugins/web-search-plus
git pull
python3 -m pytest -q
python3 -m compileall -q __init__.py search.py setup.py scripts tests
```

Check [CHANGELOG.md](../CHANGELOG.md) before upgrading across feature releases.

## More help

- [FAQ](FAQ.md) for common setup and routing problems.
- [Architecture](ARCHITECTURE.md) for routing, trust boundaries, caching, and provider extension notes.
