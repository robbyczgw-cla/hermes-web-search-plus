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

## Provider setup

Keys live in the active Hermes environment file, normally `~/.hermes/.env`. The setup helper preserves existing entries and does not print secret values.

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

Search-capable providers include You.com, Serper, Exa, Firecrawl, Tavily, Linkup, Parallel, Brave, Perplexity, Kilo Perplexity, SearXNG, SerpBase, and Querit. Extraction-capable providers are Linkup, Firecrawl, Tavily, Exa, Parallel, and You.com.

### Migration note for v2.0.0

Routing v2 changes the default `provider="auto"` behavior. Existing configs keep explicit user choices, but missing `auto_allow` entries inherit the new guarded defaults: Brave, SerpBase, Querit, native Perplexity, and Kilo Perplexity stay explicit-only until you opt them into automatic routing.

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config show --json
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
```

## Routing preferences

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

Important parameters:

- `provider`: `auto`, or a concrete provider such as `you`, `serper`, `exa`, `firecrawl`, `tavily`, `linkup`, `brave`, `perplexity`, `kilo-perplexity`, `searxng`, `serpbase`, or `querit`. Brave, Parallel, Perplexity/Kilo Perplexity, SerpBase, and Querit are available for explicit calls but default to `auto_allow=false`.
- `count`: result count, from 1 to 20.
- `time_range`: `day`, `week`, `month`, or `year` where supported.
- `include_domains` / `exclude_domains`: provider-dependent domain filters.
- `quality_report`: include routing diagnostics, skipped providers, result quality hints, and extraction recommendation.
- `mode="research"`: query multiple providers and optionally extract selected URLs within a best-effort wall-clock budget.

## Using `web_extract_plus`

Use `web_extract_plus` when you already have URLs and want page content, not just search snippets.

```python
web_extract_plus(urls=["https://example.com"], provider="firecrawl")
web_extract_plus(urls=["https://docs.linkup.so"], provider="linkup", render_js=False)
```

Auto extraction currently tries Tavily, Exa, Linkup, Firecrawl, Parallel, and You.com when keys are available. Tavily is the fast reliable default; Exa is the fast docs/academic backup; Linkup stays the clean long-form fallback; Firecrawl remains the robust scraper safety net.

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
