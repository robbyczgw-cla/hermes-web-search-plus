# web-search-plus FAQ

## Why did SerpBase not get used after I set `SERPBASE_API_KEY`?

Because SerpBase is explicit-opt-in for automatic routing. The key enables direct calls:

```python
web_search_plus(query="best DAC reviews", provider="serpbase")
```

To let `provider="auto"` or fallback select SerpBase, opt in:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase on
```

Turn it back off with:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-auto-allow serpbase off
```

## Which provider gets picked when multiple providers are configured?

If routing is enabled, the query analyzer scores providers by query signals such as current-info intent, product/local intent, research language, direct-answer intent, semantic-discovery intent, and privacy intent. The router then filters out unavailable, disabled, or `auto_allow=false` providers and chooses the best eligible provider. Ties are deterministic per query.

For the exact flow, see [Architecture](ARCHITECTURE.md#routing-engine).

## How do I force one provider?

Per call:

```python
web_search_plus(query="Hermes Agent docs", provider="brave")
```

Persistently:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default brave
```

Re-enable auto-routing later:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-routing on
```

## What happens if all providers fail?

The tool returns a structured error or warning metadata instead of fabricating results. In fallback paths, the plugin keeps partial results when it has them. Providers with repeated failures enter cooldown and are skipped temporarily.

Typical causes:

- missing key
- quota or rate limit
- provider outage
- network timeout
- bad or unsupported query parameters

## Why is my new key not detected?

After editing `.env`, restart or reset Hermes so the process reloads environment variables:

```text
CLI: restart hermes, or use /reset
Gateway/Telegram: /restart, then /reset
```

Then check:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py status
```

Also verify that the key was written to the environment file Hermes actually uses. The setup helper supports explicit targeting:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py setup brave --env-path ~/.hermes/.env
```

## How do I cap spend?

Use fewer providers, pin cheaper providers, keep extraction capped, and avoid research mode unless needed.

Useful controls:

- `count` limits search result count.
- `mode="normal"` avoids multi-provider research fanout.
- `research_time_budget` limits research-mode wall-clock work between steps.
- `web_answer_plus(max_extracts=...)` limits extraction count and is hard-capped at 5.
- `setup.py config set-default <provider>` pins search to one provider.
- `setup.py config disable <provider>` removes a provider from auto-routing.
- `setup.py config set-auto-allow <provider> off` keeps a provider explicit-only.

These are plugin-side controls, not a billing guarantee. Providers own their own pricing and rate limits.

## Is `web_answer_plus` production-ready?

It is beta. It is useful for concise cited summaries, but it is not the default path for live facts. Prefer `web_search_plus` for current events, sports, prices, weather, schedules, standings, and source discovery.

Citations are best-effort source references from selected search/extraction results. They are not legal, medical, financial, or academic verification.

## Why do SerpBase results differ from Google?

Different SERP APIs can have different freshness, ranking, localization, personalization, vertical support, and parsing behavior. Treat SerpBase as a Google-like SERP provider, not as a promise of exact Google parity.

## Can I run with only one provider?

Yes. Configure one search-capable provider and pin it if you want predictable behavior:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py setup brave
python ~/.hermes/plugins/web-search-plus/setup.py config set-default brave
```

Extraction requires an extraction-capable provider such as Linkup, Firecrawl, Tavily, Exa, or You.com.

## Can I run fully offline?

No. The plugin calls external provider APIs for search and extraction. You can use a self-hosted SearXNG instance for the search provider, but the plugin still sends the query to that configured instance.

## Does the plugin log or send results anywhere else?

The normal data flow is:

```text
Hermes tool call → web-search-plus plugin → configured provider API → plugin response → Hermes agent
```

The plugin writes local cache files and provider health state under the cache directory. It does not add a separate analytics service. Provider APIs still receive the queries or URLs you ask them to process.

## Where is the cache?

By default the cache directory is near the plugin install under the Hermes plugin cache area. Override it with:

```bash
export WSP_CACHE_DIR=/path/to/cache
```

Search cache TTL is 1 hour. CLI tests can bypass cache with:

```bash
python3 search.py --query "Hermes Agent" --provider auto --no-cache
```

## What do I do about repeated 429 or quota errors?

- Wait for the provider quota window to reset.
- Disable that provider temporarily:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config disable brave
```

- Pin a different provider:

```bash
python ~/.hermes/plugins/web-search-plus/setup.py config set-default tavily
```

- Reduce fanout by avoiding `mode="research"`.

Repeated provider failures are cooled down automatically, but provider-side quota exhaustion still needs provider-side quota management.

## How do I debug routing decisions?

Use `quality_report=True` or CLI `--quality-report`:

```python
web_search_plus(query="best bookshelf speakers under 1000", quality_report=True)
```

```bash
python3 search.py --query "best bookshelf speakers under 1000" --provider auto --quality-report --compact
```

Look for selected provider, provider scores, skipped providers, cooldown skips, and `auto_allow_excluded`.
