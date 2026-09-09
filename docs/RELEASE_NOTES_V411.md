# Web Search Plus 4.1.1

## Adapter fidelity

Search adapters now keep more of the evidence the upstream APIs already return.

- Exa search prefers the requested `highlights` over the first 800 characters of page text when both are present. If Exa returns no highlights, the snippet still uses the leading text.
- Parallel Search sends `max_results` in `advanced_settings` and domain include/exclude in `advanced_settings.source_policy`. It no longer rewrites the query with `site:` operators. Results are still trimmed locally as a safety cap.
- Tavily Search applies the unified `freshness` filter as native `time_range`. `--time-range` wins if both are set. Result metadata reports the value that was actually sent, including time-range-only requests. Linkup, Parallel, and SerpBase still run unfiltered and report `applied=false`.

Existing tool names, defaults, and Plus/native routing stay the same.

Exa publication bounds are computed once for the HTTP body and carried into
freshness receipts unchanged, including Research Mode. `--time-range hour`
selects a one-hour window and takes precedence over `--freshness week`.
Unknown Exa recency tokens apply no relative date filter. Canonical v3 output
preserves the receipt in `warnings[].details.freshness` under
`wsp.freshness.applied`; the `applied` field records whether a filter was sent.

## Compatibility

- No Hermes tool is renamed or removed.
- Native `wsp` backend selection is unchanged.
- Providers without a native recency filter still search and report `freshness.applied=false`.

## Upgrade

```bash
hermes plugins update web-search-plus
```

Reload Hermes after updating so the registered plugin tools use the new code.
