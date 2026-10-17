# Parallel Search MCP

Use `parallel-mcp` for free web search with source URLs and excerpts through
[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp).
Anonymous searches use Fast mode and need no account or API key. The public tier
has rate limits and is intended for exploration and light use.

This provider is separate from `parallel`, which uses the Parallel API and
`PARALLEL_API_KEY`. It is search-only, explicit-only, and never joins automatic
routing by default. Installing it does not change your provider selections.
No extra Python packages or local MCP subprocess are needed.

## Enable anonymous search

In Hermes, enable public access in your active profile:

```bash
hermes config set PARALLEL_MCP_ALLOW_PUBLIC 1
```

Then ask Hermes to call `web_search_plus` with `provider="parallel-mcp"`.
The same selection works in the standalone CLI:

```bash
PARALLEL_MCP_ALLOW_PUBLIC=1 python search.py --provider parallel-mcp \
  --query "Python release documentation" --no-cache
```

Standalone users can instead merge this section into their existing `config.json`:

```json
{
  "parallel_mcp": {
    "allow_public": true,
    "timeout": 30
  }
}
```

Under Hermes, the active profile controls public access; `config.json` cannot
opt a profile into anonymous requests. `timeout` bounds the MCP initialization
and search together, in seconds (greater than zero, at most 120), and is further
limited by the request deadline and per-attempt timeout.

For higher limits, set a dedicated `PARALLEL_MCP_API_KEY` in your active profile
with `hermes config set PARALLEL_MCP_API_KEY <key>`, or in the environment for the
standalone CLI. That key is sent only to `https://search.parallel.ai/mcp` as a
Bearer token. The existing `PARALLEL_API_KEY` is never reused by this provider.

## Capabilities and privacy

The adapter returns source titles, URLs, publication dates when available, and
bounded excerpts. It does not return synthesized answers or implement page
extraction; use an extraction-capable provider for `web_extract_plus`.

Include/exclude domain filters are applied locally to the returned sources.
They do not restrict which websites Parallel searches, and filtering can leave
fewer results than requested. Freshness, news-only search, language and country
filters are not applied by this adapter. For API-level filters and configurable
search modes, use the existing `parallel` provider.

Queries leave your machine and go to Parallel. Requests are subject to Parallel's
[Customer Terms](https://parallel.ai/customer-terms) and
[Privacy Policy](https://parallel.ai/privacy-policy). Normal Web Search Plus
cache and privacy settings still apply. Transport errors and upstream tool errors
are reported without copying upstream diagnostic text into results.
