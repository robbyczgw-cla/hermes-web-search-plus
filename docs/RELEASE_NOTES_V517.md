# Web Search Plus 5.1.7

Honest page extraction. When a page cannot be read, the model now learns why and can tell the user, and Web Search Plus stops paying for providers that cannot help. Found in a code review and checked with real keys through the registered Hermes tool. No configuration changes are needed.

## Fixed

- **Every failed URL names its reason.** `page not found (HTTP 404)`, `page removed (HTTP 410)`, `the site refused automated access`, `the site could not be reached or timed out`, or `<provider> does not support this site`, with the provider that reported it. Before, a request where no page could be read returned only "All extraction providers failed".
- **A missing page costs one call.** When a provider reports that the site answered 404 or 410, no other provider is tried for that URL. Measured on 2026-10-30: one Tavily call in 0.7 s, instead of Tavily, Exa and Linkup in 2.7 s.
- **The fallback chain reaches three providers again.** A transient error at the first provider used to spend two of the three attempts per request on a retry.
- **Firecrawl.** A 404 page came back as page content and was cached. A site Firecrawl does not support (HTTP 403) blocked Firecrawl for five minutes for all URLs, with the hint to check the API key.
- **Page titles stay on one line.** A line break in a title could look like an extra result to the model.
- **Research** shows unread sources with the reason and counts only pages it read.
- **Native backend** (`web.extract_backend: wsp`) keeps the reason per URL; search snippets are capped at 1,200 characters like in `web_search_plus`.

## Before and after (live, 2026-10-30)

| Case | 5.1.6 | 5.1.7 |
|---|---|---|
| Page that does not exist | 3 paid calls, "All extraction providers failed" | 1 call, "page not found (HTTP 404)" |
| Batch: page + missing page | 3 calls, "Tavily extract failed" | 1 call, page text + "page not found (HTTP 404)" |
| `provider="firecrawl"`, site it refuses, then a normal page | Firecrawl blocked, second page served by Tavily | Firecrawl serves the second page |
| `provider="firecrawl"`, missing page | Firecrawl skipped (blocked), 3 other paid calls | 1 call, "page not found (HTTP 404) (reported by firecrawl)" |
| Site that refuses bots | 3 calls, "All extraction providers failed" | 3 calls, reason plus "tried tavily, exa, linkup; 5 more providers not tried" |

Full list: [CHANGELOG](../CHANGELOG.md).
