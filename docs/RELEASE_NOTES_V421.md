# Web Search Plus 4.2.1

Point release on the 4.2 line. Plugin APIs stay the same.

## Cache freshness

`web_search_plus` now exposes `no_cache` and `cache_ttl`, matching the CLI. Recency queries and freshness filters cap the search-cache TTL (`live`/`hour` 60s, `latest`/`day` 300s, `week` 1800s). Cached hits show age. Research source summaries stay 500 characters and prefer a query-ranked span over the page prefix.

## DonSeTch 4.2.9

The optional local adapter now treats **4.2.9** as the tested DonSeTch version. Other parsed versions, including 3.x, are `compatible_unverified`. A different major is not treated as broken. DonSeTch remains separately installed, AGPL-3.0-only, and is not bundled.

```bash
npm install -g donsetch@4.2.9
export DONSETCH_BIN=$(command -v donsetch)
```

Attribution: [dondai44423/donsetch](https://github.com/dondai44423/donsetch).

## Also in 4.2.1

`setup.py status` reports optional Jev from the on-disk `jev` block instead of always printing `off`. Reported by [@prismatic7](https://github.com/prismatic7) in [#133](https://github.com/robbyczgw-cla/hermes-web-search-plus/issues/133).
