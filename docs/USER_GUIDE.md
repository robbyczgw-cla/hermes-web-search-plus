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
- `self-hosted`: SearXNG + keyless Keenable for automatic routing without a commercial API key. A separately installed DonSeTch sidecar can be layered on for explicit local search and extraction.
- `all`: prompt for every supported provider.

Search-capable providers include You.com, Serper, Exa, Firecrawl, Tavily, Linkup, Parallel, Brave, SearXNG, SerpBase, Querit, Keenable, and the optional local DonSeTch MCP sidecar. Extraction-capable providers are Linkup, Firecrawl, Tavily, Exa, Parallel, You.com, Keenable, Serper, and DonSeTch. Native Perplexity and Kilo Perplexity are not registered because their legacy answer endpoints do not expose a verified source-only mode.

Keenable is keyless: set `KEENABLE_API_KEY` for the authenticated endpoints, or opt into its public tier (off by default). In the wizard, skip the Keenable key prompt and answer yes, or run `setup.py setup keenable --keyless-public`; it writes `keenable.allow_public: true` to `config.json` (equivalently `KEENABLE_ALLOW_PUBLIC=1`).

DonSeTch is a different kind of keyless provider: it is a separately installed local MCP service rather than a public WSP endpoint. Set `DONSETCH_BIN=/absolute/path/to/donsetch` in the Hermes runtime environment, then call `provider="donsetch"` explicitly. DonSeTch stays outside automatic routing and fallback unless `setup.py config set-auto-allow donsetch on` is set. See [DonSeTch local MCP provider](DONSETCH.md) before enabling it; keyless removes commercial credentials and request billing, not local resource use, outbound traffic, rate limits, or website policy obligations.

With a `KEENABLE_API_KEY` set, requests always use the authenticated endpoints. Without a key and without the opt-in, Keenable is treated as unconfigured: it won't auto-route, fall back, or enable `web_extract_plus`. When the public tier is enabled, queries and fetched URLs are sent to an **unauthenticated** public service with per-IP limits and no SLA — roughly 1,000 requests/hour and 10 requests/second — so treat it as a best-effort last resort, not a dependable provider. The first request that uses the public endpoint logs a one-time warning so the egress is visible, and `web-search-plus doctor` reports keyless providers as `key=no` with a separate `keyless=on/off` badge so key status stays truthful.

### Local and keyless paths

`Local`, `self-hosted`, and `keyless` describe different boundaries in WSP:

- **SearXNG** is an operator-configured metasearch endpoint and supports search only. The `self_hosted` profile can select it automatically.
- **DonSeTch** is a separately installed local stdio MCP provider and supports both search and extraction. It is explicit-only by default and is not installed or silently enabled by the `self-hosted` preset.
- **Keenable's public tier** needs no API key, but it is a remote public service rather than a local provider.

Neither SearXNG nor DonSeTch makes web access offline: searches still reach upstream engines, and extracted URLs still reach destination websites. "Local" means that you operate the control-plane service and its compute, not that requests stay on the machine.

### Self-hosted automatic-routing profile

Use the `self_hosted` profile when automatic traffic must avoid commercial API keys. The quickest setup is:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py setup --preset self-hosted
python ~/.hermes/plugins/web-search-plus/setup.py status
```

The preset records the profile, enables Keenable's existing keyless public tier, and prompts only for the optional SearXNG endpoint. To configure it directly, use the canonical `searxng.base_url` name (legacy `searxng.instance_url` remains supported):

```json
{
  "profile": "self_hosted",
  "searxng": {
    "base_url": "https://search.example.net"
  }
}
```

At load time, rather than by saving duplicate routing settings, the profile derives this effective automatic policy:

- Search auto pool: SearXNG and Keenable only.
- Fallback: keyless-capable Keenable.
- Extraction auto pool: keyless-capable Keenable only.
- Budget preflight limits continue to apply unchanged when configured.

`setup.py status` is an offline diagnostic: it displays the active profile and effective auto pool, then checks that the SearXNG URL is present and well formed and that Keenable is configured or its keyless public tier is enabled. It never contacts either provider. If neither SearXNG nor Keenable is usable, an automatic request returns the typed `self_hosted_profile_unavailable` error with the local remediation.

#### Add DonSeTch for local Search + Extract

The `self-hosted` preset deliberately does not install DonSeTch or add it to the automatic pool. Install DonSeTch separately as described in [DonSeTch local MCP provider](DONSETCH.md), then set its executable path for the Hermes process:

```bash
export DONSETCH_BIN=/absolute/path/to/donsetch
```

Use DonSeTch explicitly for both capabilities:

```python
web_search_plus(query="Hermes Agent documentation", provider="donsetch", count=5)
web_extract_plus(urls=["https://example.com"], provider="donsetch")
```

This gives a local-first control plane with SearXNG available for automatic search and DonSeTch available for deliberate search or extraction. DonSeTch remains outside automatic routing and fallback unless an operator explicitly opts it in after measuring local reliability and latency.

The profile governs only automatic routing. An explicit `provider="serper"` (or another keyed provider) still works when its key exists; its result metadata includes `"profile_deviation": true` so the paid-key deviation is visible. Explicit provider requests remain useful for diagnostics and controlled operator overrides.

### Migration note for v2.0.0

Routing v2 changes the default `provider="auto"` behavior. Existing configs keep explicit user choices, but missing `auto_allow` entries inherit the guarded defaults: SerpBase, Querit, and Parallel stay explicit-only until you opt them into automatic routing; Brave joins the default auto-pool at priority 7. Perplexity provider IDs from older configs are ignored because those endpoints are no longer registered.

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

Tune automatic search routing, extraction routing, and fallback:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-priority you,serper,exa,firecrawl,tavily,linkup
python ~/.hermes/plugins/web-search-plus/setup.py config set-extract-priority serper,parallel,tavily,exa,linkup,firecrawl,you,keenable
python ~/.hermes/plugins/web-search-plus/setup.py config set-fallback serper
python ~/.hermes/plugins/web-search-plus/setup.py config disable brave
python ~/.hermes/plugins/web-search-plus/setup.py config enable brave
python ~/.hermes/plugins/web-search-plus/setup.py config set-threshold 0.45
```

Preview config changes without writing:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default you --dry-run
```

Semantics worth knowing:

- `set-default <provider>` disables auto-routing and makes `--provider auto` resolve to that provider; `set-routing on` restores query-based routing while keeping the saved default for later.
- `set-priority` changes search auto-routing and fallback priority only.
- `set-extract-priority` changes `provider="auto"` extraction order only. It accepts extract-capable providers, removes duplicates, and appends omitted extract providers in registry order.
- `set-auto-allow <provider> off` keeps a configured provider available for explicit calls while preventing auto-routing/fallback from selecting it.
- `config reset --yes` backs up the existing file before writing fresh defaults.

## V3 budget preflight

Budget preflight is opt-in. Before a native v3 request starts any provider attempt, it can cap the provider-call fan-out, request wall-time budget, extraction context, and the current UTC day's provider-call ledger. The default is disabled and all limits are unbounded, so existing requests are unchanged.

```json
{
  "budget_preflight": {
    "enabled": true,
    "max_provider_calls_per_request": 2,
    "max_daily_provider_calls": 100,
    "max_timeout_seconds": 30,
    "max_context_chars": 30000,
    "on_exceed": "degrade"
  }
}
```

Set a limit to `null` to leave that dimension unbounded. With `on_exceed: "degrade"`, WSP deterministically applies the smallest compatible cap; with `"abort"`, it returns a typed budget failure before a provider call. Daily usage is checked and recorded in the existing local v3 `budget_ledger`; an unavailable ledger fails closed when a daily quota is enabled. The receipt endpoint includes the typed checks and any reduction or abort reason. Set `WSP_BUDGET_PREFLIGHT_OFF=1` for an emergency process-level override; `0`, `false`, `no`, and `off` leave configured preflight enabled.

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

Read that as: guarded providers can have keys but remain explicit-only for `provider="auto"`, and the router selected the best eligible provider. If you want SerpBase, Brave, Querit, or Parallel to participate in automatic routing, opt in with `set-auto-allow <provider> on`; if a provider is cooled down, wait or inspect local provider health state.

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

Backward compatibility: without `defaults.locale` and without flags, everything still resolves to `us`/`en` exactly as before. Providers without locale parameters (Tavily, Exa, Linkup, Parallel, Keenable) are unaffected.

## Explicit opt-in providers: guarded providers

Some providers can be configured for explicit use without being selected automatically. That is what `auto_allow` controls.

SerpBase, Querit, and Parallel default to `auto_allow=false`. Setting their keys makes explicit calls work; configured Brave keys participate in automatic routing by default:

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
| `provider` | string | `"auto"` | `auto`, `serper`, `brave`, `tavily`, `exa`, `linkup`, `firecrawl`, `parallel`, `you`, `searxng`, `serpbase`, `querit`, `keenable` |
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

- `provider`: `auto`, or a concrete provider such as `you`, `serper`, `exa`, `firecrawl`, `tavily`, `linkup`, `brave`, `parallel`, `searxng`, `serpbase`, `querit`, or `donsetch`. Brave joins the default auto-pool at priority 7; Parallel, SerpBase, Querit, and DonSeTch remain available for explicit calls but default to `auto_allow=false`.
- `count`: result count, from 1 to 20.
- `time_range`: `day`, `week`, `month`, or `year` where supported.
- `freshness`: unified recency filter with the values `day`, `week`, `month`, or `year` (case-insensitive; invalid values return a clear error). It is applied natively by Serper, Brave, Querit, Firecrawl, Keenable, You.com, SearXNG, Exa, and TinyFish, each translated into that provider's own format (for example Brave `pw`, Serper `tbs=qdr:w`, Exa absolute UTC `startPublishedDate`/`endPublishedDate` bounds, or TinyFish publication-time filters). Providers without recency support (Tavily, Linkup, Parallel, SerpBase) still run the search normally; result metadata reports `freshness.applied=false` instead of silently dropping the filter. In `mode="research"` the applied status is reported per provider.
- `search_type`: result vertical, `search` (default) or `news` (case-insensitive; invalid values return a clear error). Serper serves news natively via `google.serper.dev/news`; TinyFish serves it through its native news-domain mode. Providers without a native news vertical still run the normal search; result metadata reports `search_type.applied=false` instead of silently ignoring the request. In `mode="research"` the applied status is reported per provider.
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

Auto extraction defaults to Tavily, Exa, Linkup, Parallel, Firecrawl, You.com, Keenable, then Serper when those providers are configured. DonSeTch is extraction-capable but excluded from automatic extraction and fallback unless explicitly opted in. Change only this order with `setup.py config set-extract-priority ...`; the setting is stored as `auto_routing.extract_provider_priority` and does not inherit search `provider_priority`. Partial lists are completed with missing extract providers in registry order. Serper's webpage scraper (`https://scrape.serper.dev`, overridable via config `serper.scrape_url`, timeout via `serper.extract_timeout`) remains the public default's last-resort fallback. Each URL is returned independently; one failed URL does not discard successful results from the same call.

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

### Diversity Score

When quality reporting is enabled, `quality_report.diversity` explains result-set variety without changing the returned order. Its 0–1 score is a documented weighted blend: 40% registrable-domain diversity, 30% canonical-URL uniqueness, 20% snippet-content diversity, and 10% provider mix. Tracking parameters and fragments do not make URLs distinct; snippets are compared with casefolded word trigrams, and pairs at or above the configured threshold are reported as near duplicates. A single-provider set receives `provider_mix: 1.0`, because provider mix is only meaningful after a research merge.

The default is diagnostic-only. To let Research Mode stably move URL/content duplicate candidates behind diverse results (never as a one-off removal), opt in with:

```json
{
  "quality": {
    "diversity": {
      "rerank": true,
      "near_duplicate_threshold": 0.6
    }
  }
}
```

`near_duplicate_threshold` must be between `0.0` and `1.0`; higher values require more overlap. With `rerank: false` (the default), research merge ordering and deduplication behavior are unchanged.
- **Explicit intent wins:** queries with `site:` operators or `include_domains` are exempt — constrained domains bypass the spam filter and the diversity rerank is skipped entirely.

## Reliability and cost controls

The plugin is designed to fail visibly rather than invent confidence.

- Search result cache TTL is 1 hour by default.
- Cache files and provider health state live under `WSP_CACHE_DIR`, or the plugin cache directory if unset.
- Use `--no-cache` in CLI tests when you need a fresh provider call.
- Transient provider errors are retried with short backoff.
- Repeated provider failures put that provider on cooldown, stepping from 1 minute to 5 minutes to 25 minutes to 1 hour.
- Research mode harvests providers in completion order but keeps the public result order deterministic. By default it may stop waiting after at least two providers contribute a sufficiently diverse result head; every provider skipped by this optimization remains visible as `preempted_after_quorum` in routing diagnostics. Tune or disable this under `quality.research_quorum` (`enabled`, `min_contributing_providers`, `result_target_cap`, `min_unique_domains`). This behavior is implemented for WSP's own provider, budget, provenance, and receipt contracts; it does not make heterogeneous providers equivalent.
- `research_time_budget` remains a best-effort wall-clock bound, not a provider-side billing limit.
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
