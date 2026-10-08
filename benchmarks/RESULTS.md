# Results

Measured with the tools in `scripts/wsp_eval/` (method: [README](README.md)).
Recorded provider answers stay out of the repository; this file holds only
aggregate numbers.

## Setup

- **Recording:** 2026-10-08, one host with real provider keys, 300 live
  calls in total:
  - 273 searches for 60 queries from `queries.jsonl` (stratified: 7-8 per intent, 18 not in English, 15 that need recent results). Each query was sent to serper, brave, exa and tavily, plus the v4.3.5 router's first choice when it was another provider, plus a recency-filtered serper call for the recency queries.
  - 27 extraction batches for 26 of these queries in research mode.
- **Evaluation configuration "core-4":** serper, brave, exa and tavily
  configured. Every version is measured by replaying the same recording,
  including each call's recorded latency.
- **Relevance:** 933 pooled URLs judged 0-3 from title, URL and snippet by
  grok-4.7 (reasoning: low). Agreement check on a 12-query sample (192 URLs)
  re-judged by gpt-6-luna: exact 0.65, within one grade 0.96, quadratic-weighted
  kappa 0.70; both judges rank the providers in the same order.
- **Limits:** 60 queries, one judge, snippet-level judgments, one region and
  one day of provider answers. Differences below ~0.02 nDCG are noise.

## Baseline: v4.3.5

| mode | nDCG@5 | authority hit@5 | latency p50 | latency p95 | empty answers | fallback used | ≈ tokens per answer |
|---|---:|---:|---:|---:|---:|---:|---:|
| normal (60 queries) | 0.672 | 0.923 | 1275 ms | 2851 ms | 0 | 0 | 1039 |
| research (26 queries) | 0.678 | 1.000 | 3731 ms | 8950 ms | 0 | 0 | 1907 |

Per intent (normal): academic 0.726, community 0.670, docs 0.791,
general 0.650, local 0.748, news 0.562, security 0.504, shopping 0.751.

WSP's own work per search with zero-latency providers (`overhead.py`): p50
16.5-17.8 ms, p95 ~25 ms; engine import 62-72 ms.

## Routing headroom (normal mode, same recording)

| strategy | nDCG@5 |
|---|---:|
| v4.3.5 router | 0.672 |
| always brave | 0.696 |
| always serper | 0.654 |
| always exa | 0.635 |
| always tavily | 0.549 |
| oracle: best of the four per query | 0.831 |

The v4.3.5 router (about 389 patterns, 21 first-match class rules, 107
boosts) scores below always choosing brave. The best provider depends on the
intent: exa leads for academic, docs and general queries and is weakest for
community queries (0.27); brave leads for community and local; serper for
shopping.
