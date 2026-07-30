# Web Search Plus v3.4.1 Release Notes

Web Search Plus v3.4.1 adds native Exa freshness bounds, introduces TinyFish as an explicit-only source-search provider, and keeps the existing automatic-routing defaults unchanged.

## Exa freshness

Unified `day`, `week`, `month`, and `year` freshness values are translated into absolute UTC `startPublishedDate` and `endPublishedDate` bounds for Exa `/search` and `/findSimilar` requests.

Explicit `start_date` and `end_date` values take precedence. Result metadata now reports those effective overrides rather than generated bounds that were not sent upstream.

This capability originated in [#111](https://github.com/robbyczgw-cla/hermes-web-search-plus/pull/111) by [@kesku](https://github.com/kesku).

## TinyFish source search

TinyFish is bundled as a Search-only provider with native news, freshness, locale, and domain filters.

- explicit-only by default (`auto_allow=false`)
- BYOK credentials only; Web Search Plus does not provide, pool, proxy, or share TinyFish keys
- fixed official HTTPS Search API origin with redirects refused
- source-only projection with opaque upstream errors and bounded query, URL, domain, response, and aggregate sizes
- excluded from automatic routing and fallback unless an operator deliberately enables it

### High Risk data-use notice

TinyFish's published Terms grant broad rights over Customer Data, including queries, for analytics, training, fine-tuning, evaluation, and model improvement. Web Search Plus cannot offer a narrower no-training exclusion. Review the current [TinyFish Terms](https://www.tinyfish.ai/terms) before sending sensitive queries.

## Attribution

Hound remains visibly attributed to the independent MIT-licensed [Master-Fetch project](https://github.com/dondai1234/master-fetch) and its developer [Bishesh Bhandari](https://github.com/dondai1234). Web Search Plus bundles no Hound code or binary.

## Verification

- Ruff passed
- 1,021 tests plus 6 schema subtests passed
- generated provider documentation and contract-v3 schemas are current
- Node schema boundary passed after a clean `npm ci`
- `npm audit --audit-level=high` reports zero vulnerabilities
