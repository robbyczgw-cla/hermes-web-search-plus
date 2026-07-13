# WSP 3.0 Provider Benchmarks

Benchmarks make provider ordering measurable, but they spend real provider quota. They never apply a recommendation automatically.

## Search benchmark

```bash
python3 search.py bench
python3 search.py bench --json
```

The search benchmark runs a small fixed live query suite against configured search providers and reports success rate, latency, result volume, URL uniqueness, and snippet coverage. It recommends an `auto_routing.provider_priority` order.

Search benchmark traffic bypasses response cache, provider cooldown mutation, and adaptive routing statistics so measurement does not train or punish the live router.

Apply a reviewed recommendation explicitly:

```bash
python3 setup.py config set-priority you,serper,exa,tavily
```

## Extraction benchmark

Extraction benchmarking requires explicit public target URLs:

```bash
python3 search.py extract-bench \
  --extract-urls https://example.com https://example.org \
  --bench-providers tavily exa linkup \
  --bench-timeout-budget 120 \
  --json
```

If `--bench-providers` is omitted, WSP uses configured, enabled, extraction-capable providers in the effective extraction-priority order.

The report compares:

- per-URL success rate;
- median provider latency;
- returned character count;
- bounded error-code counts;
- a deterministic extraction-priority recommendation.

Apply a reviewed extraction recommendation explicitly:

```bash
python3 setup.py config set-extract-priority tavily,exa,linkup
```

## History and privacy

Completed extraction benchmark summaries are written to the marker-owned Operator Console history by default. Opt out with:

```bash
python3 search.py extract-bench --extract-urls https://example.com --no-history
```

Persisted summaries contain aggregate provider metrics only. Target URLs, extracted content, API keys, endpoint URLs, and upstream exception text are not retained. Failures are reduced to bounded codes such as `auth_error`, `rate_limited`, `timeout`, or `provider_error`.

The Console reports missing history as `not_collected`; it does not manufacture zero-result benchmark records.

## Fair-use guidance

- Start with one or two stable public URLs.
- Restrict providers when testing a new key or quota plan.
- Do not benchmark private/internal targets; the same extraction URL safety gate applies.
- Treat tiny timing differences as noise. Prefer repeated success, useful content volume, and stable error behavior.
- Keep recommendations separate for search and extraction; the two priority lists serve different jobs.
