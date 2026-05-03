# web-search-plus — Hermes Plugin

Multi-provider web search, URL extraction, quality reports, and opt-in research mode for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> Ported from [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) (OpenClaw) to the Hermes Plugin API.

---

## Quick Start

```bash
git clone https://github.com/robbyczgw-cla/hermes-web-search-plus.git ~/.hermes/plugins/web-search-plus
cd ~/.hermes/hermes-agent
source venv/bin/activate
pip install requests
cd ~/.hermes/plugins/web-search-plus
cp .env.template .env          # fill in at least one provider key
# Optional: pip install httpx  # only needed for Exa deep/deep-reasoning
```

Important:
- Use the Hermes virtualenv, not your system Python.
- Run `pip` only after `source ~/.hermes/hermes-agent/venv/bin/activate` (or from `~/.hermes/hermes-agent` via `source venv/bin/activate`).
- If you test the plugin from the CLI, prefer `~/.hermes/hermes-agent/venv/bin/python` or activate the Hermes venv first.

Then enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - web-search-plus
```

Also enable the plugin toolset alongside the built-in `web` toolset so both are available:

```yaml
tools:
  enabled:
    - web
    - web-search-plus
```

Finally restart Hermes (or `/restart` + `/reset` in gateway chats) and use the plugin tools:

- `web_search_plus` — multi-provider web search and auto-routing
- `web_extract_plus` — provider-specific URL extraction via Firecrawl, Linkup, Tavily, Exa, or You.com

Both tools are exposed by the `web-search-plus` toolset; enabling `web-search-plus` enables both.

---

## Features

- **Intelligent auto-routing** — picks the best provider based on query intent
- **10 providers** — Serper, Brave, Tavily, Exa, Querit, Linkup, Firecrawl, Perplexity, You.com, SearXNG
- **Exa Deep Research** — `depth=deep` for multi-source synthesis, `depth=deep-reasoning` for cross-document analysis
- **Adaptive fallback** — automatically skips providers on cooldown (1h after failure)
- **Routing transparency** — every response includes a `routing` object explaining provider choice
- **Quality reports** — optional provider diagnostics, result-count summaries, and execution metadata
- **Research mode** — opt-in multi-provider search + extraction with a best-effort time budget
- **Time & domain filtering** — `time_range`, `include_domains`, `exclude_domains`
- **URL extraction** — `web_extract_plus` extracts clean content via Firecrawl, Linkup, Tavily, Exa, or You.com
- **Local caching** — avoids duplicate API calls (1h TTL)

---

## Provider Routing

| Provider | Best for | Free tier |
|----------|----------|-----------|
| Brave | General-purpose web search, independent index, broad factual queries | $5.00/mo in free credits |
| Serper (Google) | News, shopping, facts, local queries | 2,500/mo |
| Tavily | Research, deep content, academic | 1,000/mo |
| Exa | Semantic discovery, "alternatives to X", arxiv | 1,000/mo |
| Querit | Multilingual, real-time queries | 1,000/mo |
| Linkup | Source-backed grounding, citations, RAG-ready retrieval | €5 free monthly credits |
| Firecrawl | Web search with optional scrape-ready result content | 500 credits/free plan |
| Perplexity | Direct AI-synthesized answers | API key |
| You.com | LLM-ready real-time snippets | Limited |
| SearXNG | Privacy-focused, self-hosted, no API cost | Free |

Auto-routing scores providers based on query signals (keywords, intent, linguistic patterns). Brave and Serper share generic web-search intents; when they tie, the router uses deterministic per-query tie-breaking so the same query stays reproducible while ties are distributed across both providers. Override anytime with `provider="serper"`, `provider="brave"`, etc.

---

## Installation

### API Keys

```bash
# Required (at least one)
SERPER_API_KEY=***        # https://serper.dev — 2,500 free/mo
BRAVE_API_KEY=***         # https://brave.com/search/api/ — $5.00/mo in free credits; you won't be charged
TAVILY_API_KEY=***        # https://tavily.com — 1,000 free/mo
EXA_API_KEY=***           # https://exa.ai — 1,000 free/mo

# Optional
QUERIT_API_KEY=***        # https://querit.ai
LINKUP_API_KEY=***        # https://linkup.so — source-backed search + fetch
FIRECRAWL_API_KEY=***     # https://firecrawl.dev — search + scrape/extract
PERPLEXITY_API_KEY=***    # https://perplexity.ai/settings/api
KILOCODE_API_KEY=your-key      # Perplexity via Kilo Gateway fallback
YOU_API_KEY=your-key           # https://api.you.com
SEARXNG_INSTANCE_URL=https://your-instance.example.com
```

> Python 3.8+ required. For Exa deep research: `pip install httpx` (optional).

---

## Usage

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | **required** | The search query |
| `provider` | string | `"auto"` | Force: `serper`, `brave`, `tavily`, `exa`, `querit`, `linkup`, `firecrawl`, `perplexity`, `you`, `searxng` |
| `depth` | string | `"normal"` | Exa only: `normal`, `deep`, `deep-reasoning` |
| `count` | integer | `5` | Results (1–20) |
| `time_range` | string | — | `day`, `week`, `month`, `year` |
| `include_domains` | string[] | — | Restrict search to domains |
| `exclude_domains` | string[] | — | Exclude domains |
| `quality_report` | boolean | `false` | Include provider diagnostics and result-quality metadata |
| `mode` | string | `"normal"` | `normal` or opt-in `research` |
| `research_time_budget` | number | `55.0` | Best-effort seconds budget for research mode provider/extraction work |

### Quality report and research mode

`quality_report=True` adds diagnostic metadata without changing provider selection. Use it when tuning routing, comparing providers, or debugging weak result sets.

`mode="research"` is intentionally opt-in: it can query multiple providers and extract selected result URLs, so it is slower and may spend more API credits than normal search. The default `research_time_budget=55.0` keeps the run bounded; when the budget is exhausted, remaining providers or extraction steps are skipped and reported in routing metadata instead of hanging or failing the whole response. Search results already collected are preserved even if extraction fails.

### `web_extract_plus`

Extract content from specific URLs using provider-specific extraction backends.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `urls` | string[] | **required** | URLs to extract |
| `provider` | string | `"auto"` | Force: `firecrawl`, `linkup`, `tavily`, `exa`, `you` |
| `format` | string | `"markdown"` | `markdown` or `html` |
| `include_images` | boolean | `false` | Include image metadata when supported |
| `include_raw_html` | boolean | `false` | Include raw HTML when supported |
| `render_js` | boolean | `false` | Render JavaScript before extraction when supported |

Auto extraction currently tries Firecrawl, then Linkup, Tavily, Exa, and You.com when keys are available.

Examples:

```python
web_search_plus(query="Graz weather today")
# → auto-routed to Serper or Brave (generic weather/current-info intent)

web_search_plus(query="Singapore CPI latest", provider="brave")
# → Brave Search (independent general web index)

web_search_plus(query="alternatives to Notion", provider="exa")
# → Exa (discovery/similarity)

web_search_plus(query="LLM scaling laws research", provider="exa", depth="deep")
# → Exa deep synthesis (4–12s)

web_search_plus(query="OpenAI news", time_range="day")
# → Serper/Brave/Firecrawl, last 24h

web_search_plus(query="YC startups web scraping", provider="firecrawl")
# → Firecrawl search

web_search_plus(query="find credible sources and citations for AI tutoring outcomes", provider="linkup")
# → Linkup source-grounded retrieval

web_search_plus(query="best bookshelf speakers under 1000", quality_report=True)
# → Normal search plus diagnostic routing/result-quality metadata

web_search_plus(query="compare recent reviews of turntables under 1000", mode="research", research_time_budget=45)
# → Opt-in multi-provider research; keeps partial results if extraction hits errors/budget

web_extract_plus(urls=["https://example.com"], provider="firecrawl")
# → Extract clean markdown from a URL

web_extract_plus(urls=["https://docs.linkup.so"], provider="linkup", render_js=False)
# → Linkup fetch endpoint

web_search_plus(query="LoRA fine-tuning", include_domains=["arxiv.org"])
# → arxiv only
```

### CLI testing

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
python ~/.hermes/plugins/web-search-plus/search.py \
  --query "test query" --provider auto --max-results 5 --compact

python ~/.hermes/plugins/web-search-plus/search.py \
  --query "compare recent reviews of turntables under 1000" \
  --mode research --quality-report --research-time-budget 45 --compact
```

---

## Architecture

```
__init__.py      — Hermes plugin entry, tool schema, handler
search.py        — Core engine: providers, routing, caching, fallback
scripts/         — Golden query evaluator and support scripts
plugin.yaml      — Plugin manifest
.env.template    — API key reference
CHANGELOG.md     — Version history
```

The plugin runs `search.py` as a subprocess with a 75s timeout (for Exa deep-reasoning queries).

---

## Related

- [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) — TypeScript version for OpenClaw
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the agent this plugin runs on
