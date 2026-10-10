# Web Search Plus 5.0.1

Bug fixes for 5.0.0. No breaking changes; routing, providers and config format stay the same.

## Fixed

- **Normal queries no longer trip the source-only filter.** Searches with words such as "synthesizer" or "verify the claim" failed at Exa, Tavily and Linkup, and docs queries meant for Exa fell through to another provider. The filter now checks only what Web Search Plus itself sends, not your query, URLs or domain filters.
- **One bad URL no longer sinks an extraction batch.** Each blocked, unresolvable or failing URL gets its own error line; the other URLs are still extracted, and failed ones are retried on the next extraction provider. Error messages no longer reveal internal IP addresses.
- **Empty pages are no longer a success.** Blank extractions fall back to the next provider and are not cached.
- **Honest extract footer.** Long pages report their real length and point `read_file` at the file that holds the full cleaned text, with correct offsets.
- **Zero results say so.** A search without hits returns "No results found for this query" instead of only the provider line.
- **Bounded rate-limit waits.** A long `Retry-After` is not waited out inline (cap 30 s); retries without one back off briefly; malformed values no longer crash the attempt. The tool's time limit now reaches the engine as request deadline.
- **Long queries work at Brave.** Queries are shortened at a word boundary to Brave's 400 characters / 50 words, `site:` operators kept, and the output says so. A provider rejecting a too-long query reads "Query rejected … it may be too long".
- **Auto routing off without a default provider** uses the configured provider order instead of failing every search.
- **A private SearXNG URL** without `SEARXNG_ALLOW_PRIVATE=1` skips SearXNG instead of breaking every automatic search.
- **Keys only in config.json or Hermes Desktop** no longer hide the tools; `/web-search-plus-setup` reports them too.

## Changed

- Tool descriptions start with a short first sentence for the Hermes Tool Search catalog. The `web_extract_plus` description lists the real default extraction order.

Full list: [CHANGELOG](../CHANGELOG.md).
