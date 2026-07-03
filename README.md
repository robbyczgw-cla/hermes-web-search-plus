# web-search-plus — Hermes Plugin


<p align="center">
  <img src="docs/assets/web-search-plus-v261-hero.png" alt="Hermes web-search-plus hero: two-tool surface, adaptive Routing v2, research mode, quality diagnostics, and 14 search / 8 extraction providers" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-22d3ee.svg"></a>
  <img alt="Python 3.8+" src="https://img.shields.io/badge/python-3.8%2B-34d399.svg">
  <img alt="Hermes Plugin" src="https://img.shields.io/badge/Hermes-plugin-a78bfa.svg">
</p>

**Web Search Plus is the operator-grade web layer for Hermes: one search tool, one extraction tool, many providers, conservative routing, safe large-page handling, freshness controls, and provider benchmarking without locking you into a single API.** Routing v2 spans 14 search providers and 8 extraction-capable providers; `web_extract_plus(provider="auto")` defaults to Tavily-first extraction for fast, reliable fetches, with Exa, Linkup, Firecrawl, Parallel, You.com, and Serper as fallback paths when available.

`web-search-plus` adds two Hermes tools:

- `web_search_plus` — routed multi-provider web search with quality diagnostics
- `web_extract_plus` — clean URL extraction via provider backends

> Ported from [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) for the [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin API.

---

## Why this exists

Most web-search tools fail in one of two boring ways: they hard-code a single provider, or they pretend every user has every API key. Web Search Plus is capability-based instead: it lets Hermes search, extract, compare, and recover across the providers you actually configured.

- **No global required key.** Configure one search-capable provider and search works; extraction providers are additive.
- **Routing v2 is conservative.** You.com, Serper, Exa, Firecrawl, Tavily, and Linkup form the default auto-search pool; everything else stays explicit/guarded until you opt in.
- **Large pages stay usable.** Long extracts return a compact head/tail preview plus a `read_file` footer for the full cleaned text, instead of dumping token bombs into the agent context.
- **Freshness, verticals, and locale are explicit.** `day`/`week`/`month`/`year` recency, `search_type="news"`, and `country`/`language` defaults with query-language auto-detection — providers that cannot apply a filter report that in metadata instead of silently dropping it.
- **Provider quality is measurable.** The built-in bench compares configured providers on success rate, latency, result volume, and snippet coverage, then suggests a provider-priority order without writing config automatically.
- **Costs and safety stay bounded.** Research mode caps provider work and keeps partial results, failed providers go on cooldown, and extraction rejects private/internal target URLs before provider dispatch.

---

## Quick Start

```bash
# 1) Install and enable the Hermes plugin
hermes plugins install robbyczgw-cla/hermes-web-search-plus --enable

# 2) Configure provider keys with the standalone setup wizard
python ~/.hermes/plugins/web-search-plus/setup.py status
python ~/.hermes/plugins/web-search-plus/setup.py setup

# Bare setup prompts every supported provider; press Enter to skip what you do not have.
# Fast starter preset if you want the short path:
# python ~/.hermes/plugins/web-search-plus/setup.py setup --preset starter
# YOU_API_KEY=...      # fast Routing v2 core provider
# SERPER_API_KEY=...   # reliable Google-like fallback
# LINKUP_API_KEY=...   # clean extraction

# 3) Restart/reload Hermes so plugin tools are registered
# CLI: exit and start `hermes` again, or use /reset in-session
# Gateway/Telegram: /restart, then /reset

# 4) Optional shell smoke test
cd ~/.hermes/plugins/web-search-plus
python3 search.py --query "Hermes Agent latest release" --provider auto --quality-report
```

Notes:

- Plugin install clones into `~/.hermes/plugins/web-search-plus`; update later with `hermes plugins update web-search-plus`, then restart/`/reset`.
- Keys are written to the active Hermes environment file by the setup helper; they should never be committed to the repo.
- Python 3.8+ is required; runtime code is stdlib-only.
- To make Web Search Plus the preferred web layer, disable the built-in web toolset (`agent.disabled_toolsets: [web]`) and verify with `setup.py fastpath` — details in the [User Guide](docs/USER_GUIDE.md#installation-and-first-run-checks).

---

## Documentation

- [User Guide](docs/USER_GUIDE.md) — detailed setup, provider tuning, routing preferences, tool parameters, extraction, reliability, and cost controls.
- [Provider Reference](docs/PROVIDERS.md) — generated per-provider matrix: capabilities, env vars, auto-routing defaults, free tiers, and signup links.
- [Routing v2 Reference](docs/ROUTING.md) — generated class-by-class view of what auto-routing prefers and demotes.
- [FAQ](docs/FAQ.md) — common setup, provider selection, cache, quota, and troubleshooting questions.
- [Architecture](docs/ARCHITECTURE.md) — plugin boundary, routing engine, auto-allow gate, cache/cooldown state, data flow, and provider-extension notes.

---

## Capability model

| Capability | Unlocks | Configure at least one of |
|---|---|---|
| Search | `web_search_plus` | Brave, Serper, Tavily, Exa, Linkup, Firecrawl, Parallel, Perplexity, Kilo Perplexity, You.com, SearXNG, SerpBase, Querit, or Keenable |
| Extraction | `web_extract_plus` | Linkup, Firecrawl, Tavily, Exa, Parallel, You.com, Serper, or Keenable |
| Best starter | Search + extraction + reliable fallback | You.com + Serper + Linkup |

---

## The two tools

### `web_search_plus`

Use this when the agent needs search results and routing metadata.

```python
web_search_plus(query="Graz weather today")
# → auto-routed current-info search

web_search_plus(query="alternatives to Notion", provider="exa")
# → semantic discovery via a forced provider

web_search_plus(query="compare recent reviews of turntables under 1000", mode="research", research_time_budget=45)
# → opt-in multi-provider research; keeps partial results if extraction hits errors/budget

web_search_plus(query="best bookshelf speakers under 1000", quality_report=True)
# → normal search plus routing/result-quality diagnostics
```

### `web_extract_plus`

Use this when you already have URLs and want clean content.

```python
web_extract_plus(urls=["https://example.com"], provider="firecrawl")
# → extract clean markdown from a URL

web_extract_plus(urls=["https://docs.linkup.so"], provider="linkup", render_js=False)
# → Linkup fetch endpoint
```

Auto extraction tries Tavily → Exa → Linkup → Parallel → Firecrawl → You.com for configured keys, then Keenable (only if configured), and finally Serper's webpage scraper as the last-resort safety net. Large pages use truncate-and-store output handling: a bounded head/tail preview inline, the full cleaned text stored under `cache/web/`, and a ready-to-run `read_file` call in the footer for paging into the omitted middle.

Full parameter tables for both tools, freshness/vertical/locale semantics, and cache management live in the [User Guide](docs/USER_GUIDE.md#using-web_search_plus).

---

## Providers

| Provider | Search | Extract | Best for |
|---|---:|---:|---|
| You.com | ✅ | ✅ | Fast Routing v2 core for current, multilingual, LLM-ready search |
| Serper | ✅ | ✅ | Reliable Google-like fallback for facts, shopping, local, and news; webpage scraper as last-resort extraction |
| Exa | ✅ | ✅ | Semantic discovery, docs, GitHub, academic/arXiv |
| Firecrawl | ✅ | ✅ | Source-first web search with scrape-ready result content |
| Tavily | ✅ | ✅ | Long-form research and content-heavy queries |
| Linkup | ✅ | ✅ | Source-backed grounding, citations, RAG-ready retrieval |
| Perplexity | ✅ | — | Native synthesized search; guarded by default (`auto_allow=false`) |
| Kilo Perplexity | ✅ | — | Perplexity through Kilo gateway; guarded by default (`auto_allow=false`) |
| Brave | ✅ | — | Independent web index; guarded by default (`auto_allow=false`) |
| SearXNG | ✅ | — | Privacy-focused self-hosted metasearch |
| Keenable | ✅ | ✅ | Independent web index; key or opt-in keyless public tier (off by default); lowest-priority fallback |
| SerpBase | ✅ | — | Cheap Google-like SERP fallback; guarded by default (`auto_allow=false`) |
| Parallel | ✅ | ✅ | LLM-ready search and fast extract with long source excerpts; guarded by default (`auto_allow=false`) |
| Querit | ✅ | — | Multilingual and real-time queries; guarded by default (`auto_allow=false`) |

Routing v2 is benchmarked and class-aware: it detects language/script hints and query classes (news, shopping/local, docs/API, GitHub, academic, community, CVE/security, finance, weather, and more — see the [Routing Reference](docs/ROUTING.md)). Guarded providers are available for explicit `provider=` calls once their key is set; opt them into automatic routing with `setup.py config set-auto-allow <provider> on`.

---

## API keys

All provider keys are optional at install time. Configure only what you use:

```bash
# Search-capable providers
SERPER_API_KEY=***        # https://serper.dev — search + extraction (webpage scraper)
BRAVE_API_KEY=***         # https://brave.com/search/api/
TAVILY_API_KEY=***        # https://tavily.com — search + extraction
EXA_API_KEY=***           # https://exa.ai — search + extraction
LINKUP_API_KEY=***        # https://linkup.so — search + cheap/citation-friendly extraction
FIRECRAWL_API_KEY=***     # https://firecrawl.dev — search + extraction
PERPLEXITY_API_KEY=***    # https://perplexity.ai/settings/api
YOU_API_KEY=***           # https://api.you.com — search + extraction
SEARXNG_INSTANCE_URL=https://your-instance.example.com
KEENABLE_API_KEY=***      # https://keenable.ai — search + extraction
SERPBASE_API_KEY=***      # https://www.serpbase.dev — explicit/fallback-only Google-like SERP search
PARALLEL_API_KEY=***      # https://platform.parallel.ai — explicit/guarded LLM-ready search + extraction
QUERIT_API_KEY=***        # https://querit.ai — explicit/fallback-only by default

# Kilo gateway alternate provider (`provider="kilo-perplexity"`)
KILOCODE_API_KEY=***
```

Keenable also has an opt-in keyless public tier (off by default, per-IP limits, no SLA) — see [User Guide → Provider setup](docs/USER_GUIDE.md#provider-setup).

---

## Quality, reliability, and safety

- **Adaptive routing:** recent provider latency/error/empty-result outcomes nudge close routing calls within a bounded adjustment; strong query-class signals are never overridden.
- **Result hygiene:** known SEO mirrors/scrapers are filtered, domain diversity is enforced, and explicit `site:`/`include_domains` intent bypasses both.
- **Safe extraction targets:** `web_extract_plus` rejects loopback, RFC1918, link-local, cloud-metadata, and other private/internal target URLs before provider dispatch.
- **Cooldowns and budgets:** failing providers go on escalating cooldown; research mode enforces a best-effort wall-clock budget and keeps partial results.
- **Truthful metadata:** missing keys, quota failures, empty results, unapplied filters, and budget exhaustion are reported in response metadata instead of being hidden.

Details and tuning knobs (spam-filter overrides, diversity limits, `allow_private_urls`, cache management) are in the [User Guide](docs/USER_GUIDE.md#result-quality).

---

## Development

```bash
cd ~/.hermes/plugins/web-search-plus
python3 -m pip install -r requirements.txt
python3 -m pytest -q
python3 -m compileall -q __init__.py search.py setup.py scripts tests

# Offline golden snapshot quality checks (CI-safe, no live provider calls)
python3 scripts/golden_eval.py --snapshot-fixtures tests/fixtures/golden_snapshots.json --out /tmp/golden-quality.jsonl
```

Module layout, routing engine internals, compatibility-shim policy, and provider-extension notes are documented in [Architecture](docs/ARCHITECTURE.md).

---

## License

MIT — see [LICENSE](LICENSE).

## Related

- [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) — TypeScript/OpenClaw version
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent runtime this plugin extends
