# Provider reference

<!-- Generated file. Do not edit by hand. -->

This reference is generated from `provider_registry.py`, discovered `providers.d` modules,
and the plugin provider catalog; regenerate it with `python scripts/gen_provider_docs.py` after changing provider metadata.

## Provider matrix

| Provider | Search | Extract | Env var | Keyless | Auto routing | Free tier | Signup |
|---|---:|---:|---|---|---|---|---|
| Serper | ✅ | ✅ | `SERPER_API_KEY` | — | yes (priority 2) | 2,500 one-time credits | https://serper.dev/api-key |
| SerpBase | ✅ | — | `SERPBASE_API_KEY` | — | explicit-only (`auto_allow=false`) | 100 free searches, paid packs available | https://www.serpbase.dev |
| Brave Search | ✅ | — | `BRAVE_API_KEY` | — | yes (priority 7) | $5 free monthly credits | https://api.search.brave.com/app/keys |
| Tavily | ✅ | ✅ | `TAVILY_API_KEY` | — | yes (priority 5) | 1,000 free searches/month | https://tavily.com |
| Querit | ✅ | — | `QUERIT_API_KEY` | — | explicit-only (`auto_allow=false`) | 1,000 free searches/month | https://www.querit.ai |
| Linkup | ✅ | ✅ | `LINKUP_API_KEY` | — | yes (priority 6) | €5 free monthly credits (~5,000 standard extracts) | https://www.linkup.so |
| Exa | ✅ | ✅ | `EXA_API_KEY` | — | yes (priority 3) | 1,000 free searches/month | https://dashboard.exa.ai/api-keys |
| Firecrawl | ✅ | ✅ | `FIRECRAWL_API_KEY` | — | yes (priority 4) | 500 one-time credits | https://www.firecrawl.dev/app/api-keys |
| Parallel | ✅ | ✅ | `PARALLEL_API_KEY` | — | yes (priority 8) | API key required | https://platform.parallel.ai |
| Perplexity | — | — | `PERPLEXITY_API_KEY` | — | — | API key required | https://www.perplexity.ai/settings/api |
| Kilo Code Perplexity bridge | — | — | `KILOCODE_API_KEY` | — | — | Depends on Kilo account | https://kilo.ai |
| You.com | ✅ | ✅ | `YOU_API_KEY` | — | yes (priority 1) | Limited/API key required | https://api.you.com |
| SearXNG | ✅ | — | `SEARXNG_INSTANCE_URL` | — | yes (priority 11) | Free if self-hosted | https://docs.searxng.org/admin/installation.html |
| Keenable | ✅ | ✅ | `KEENABLE_API_KEY` | yes (`KEENABLE_ALLOW_PUBLIC` opt-in) | yes (priority 12) | Keyless public tier; optional key for higher limits | https://keenable.ai |
| DonSeTch (local MCP) | ✅ | ✅ | `DONSETCH_BIN` | — | explicit-only (`auto_allow=false`) | Free local binary; no API key | https://github.com/dondai44423/donsetch |
| Octen via Monid | ✅ | — | `MONID_API_KEY` | — | explicit-only (`auto_allow=false`) | No free-tier claim; Monid API key and wallet balance required | https://app.monid.ai/access/api-keys |
| TinyFish Search | ✅ | — | `TINYFISH_API_KEY` | — | explicit-only (`auto_allow=false`) | Search does not consume credits; API access required (30 rpm Free/PAYG) | https://agent.tinyfish.ai/api-keys |

`priority N` is the provider's position in the default routing priority list.
Providers marked explicit-only stay out of `provider="auto"` routing and fallback
until opted in with `setup.py config set-auto-allow <provider> on`.

## Provider notes

### Serper

Google-like SERP results for facts, shopping, local and news queries, plus webpage scraping.

### SerpBase

Cheap Google-like SERP fallback; WSP exposes search only, explicit/fallback-only by default.

### Brave Search

Independent general web index in the Routing v2 default pool.

### Tavily

*Recommended starter provider.*

Research/tutorial provider in the Routing v2 default pool.

### Querit

Multilingual and real-time search candidate.

### Linkup

*Recommended starter provider.*

Best starter for cheap clean extraction and citation-grounded retrieval.

### Exa

Semantic discovery, alternatives, docs, academic and long-form discovery.

### Firecrawl

Robust scraping/extraction fallback, especially for JS-heavy pages.

### Parallel

LLM-ready web search and fast URL extraction with long source excerpts.

### Perplexity

Rejected legacy answer endpoint; no source-only mode is registered.

### Kilo Code Perplexity bridge

Rejected legacy answer bridge; no source-only mode is registered.

### You.com

*Recommended starter provider.*

Fast Routing v2 core provider for current, multilingual, and LLM-ready search.

### SearXNG

Self-hosted/privacy-preserving metasearch instance URL.

### Keenable

Independent web index for search and extraction; works keyless, optional key raises rate limits.

### DonSeTch (local MCP)

Local DonSeTch 3.x stdio MCP provider for metasearch, direct extraction, PDF/OCR-aware fetching, and optional browser escalation.

### Octen via Monid

Explicit-only Octen source-result web search through Monid, with native recency and domain filtering.

### TinyFish Search

Direct source-only TinyFish web/news search using your own account/API key. WSP does not provide, pool, proxy, or share TinyFish credentials. Domain filters and result hosts are accepted only as ASCII/Punycode hostnames. Privacy warning: TinyFish's standard Terms permit Customer Data to be used for model training and fine-tuning; review https://www.tinyfish.ai/terms and https://www.tinyfish.ai/privacy-policy before use. Explicit-only by default.
