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

## Latency and fallback (hedged fallback)

Replayed time-scaled (0.05) with `scenarios.py`; a fault makes one provider
hang until the client timeout, answer with no results, or answer HTTP 503 on
every call. Latencies include WSP's own per-call work scaled up with the
replay, so compare rows, not absolute p50 values.

| scenario | version | p95 | slowest | empty answers | provider calls / query | nDCG@5 |
|---|---|---:|---:|---:|---:|---:|
| no fault | v4.3.5 | 3165 ms | 6496 ms | 0 | 1.00 | 0.672 |
| no fault | hedged | 3294 ms | 3931 ms | 0 | 1.10 | 0.668 |
| serper hangs | v4.3.5 | 31845 ms | 32147 ms | 1 | 1.45 | 0.673 |
| serper hangs | hedged | 3551 ms | 4544 ms | 0 | 1.12 | 0.688 |
| serper answers empty | v4.3.5 | 3370 ms | 6704 ms | 27 | 1.00 | 0.373 |
| serper answers empty | hedged | 3530 ms | 6686 ms | 0 | 1.55 | 0.688 |
| serper answers 503 | v4.3.5 | 3626 ms | 7424 ms | 1 | 1.45 | 0.673 |
| serper answers 503 | hedged | 3206 ms | 4323 ms | 0 | 1.10 | 0.688 |
| brave hangs | v4.3.5 | 32551 ms | 34257 ms | 0 | 1.23 | 0.662 |
| brave hangs | hedged | 3701 ms | 4674 ms | 0 | 1.17 | 0.658 |

## Research mode (rank fusion and passages)

The 26 research queries of the recorded set, replayed (scale 0.2) in the
core-4 configuration. Before, research mode listed the first provider's
results and appended the others, so the top 5 came from one provider. Now
the provider lists are fused with reciprocal rank fusion (k = 60, pages
found by several providers rank higher).

| version | nDCG@5 | authority hit@5 | providers in top 5 | p50 | p95 | approx. tokens |
|---|---:|---:|---:|---:|---:|---:|
| before | 0.678 | 1.000 | 1.00 | 3480 ms | 9097 ms | 1907 |
| fused + passages | 0.694 | 1.000 | 2.00 | 3577 ms | 9167 ms | 2406 |

Tokens: the source summaries grow by about 200 characters per answer
(two 300-character passages instead of one 500-character excerpt). The
remaining growth comes from Exa results reaching the top 5 more often
(1.73 -> 2.23 per answer); their snippets are long multi-passage highlights.

Passage size was chosen with a judge (Grok 4.7, 0-3 "does this text help
answer the query") that saw the three variants unlabelled, in a fixed order
with the old excerpt first, for 53 extracted sources:

| summary shape | mean grade | useful (grade >= 2) |
|---|---:|---:|
| 1 x 500 characters (before) | 1.72 | 60% |
| 2 x 300 characters | 2.02 | 75% |
| 3 x 250 characters | 1.98 | 77% |
