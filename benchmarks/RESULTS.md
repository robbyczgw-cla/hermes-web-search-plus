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
answer the query") that saw the three variants unlabelled and in a random
order per source, for 53 extracted sources:

| summary shape | mean grade | useful (grade >= 2) |
|---|---:|---:|
| 1 x 500 characters (before) | 1.72 | 60% |
| 2 x 300 characters | 2.02 | 75% |
| 3 x 250 characters | 1.98 | 77% |

## Long result snippets

Exa highlights and Tavily content often run to 1,000-3,000 characters per
result, and the tool output printed them whole. 60 long snippets from the
recorded answers were judged as above (four unlabelled texts per result, random
order): the full snippet, its first 1,200 characters, and query-ranked
passages chosen from it.

| shown | mean grade | useful (grade >= 2) | characters |
|---|---:|---:|---:|
| full snippet | 2.92 | 100% | 126,413 |
| first 1,200 characters | 2.70 | 100% | 68,765 |
| 3 x 400 query-ranked passages | 2.33 | 95% | 39,533 |
| 2 x 300 query-ranked passages | 1.78 | 62% | 25,141 |

Provider snippets are already query-selected excerpts, so selecting passages
again loses context; the plain prefix keeps more. The tool output now shows
the first 1,200 characters of a longer snippet (the payload keeps it whole).
Output tokens on the recorded queries (core-4 replay):

| mode | before: mean / p95 / max | first 1,200 characters: mean / p95 / max |
|---|---:|---:|
| normal (60 queries) | 1056 / 3328 / 4108 | 800 / 1487 / 1680 |
| research (26 queries) | 2441 / 3126 / 3475 | 1405 / 1830 / 1980 |

## Routing 5.0 (intent table)

The 60 recorded normal-mode queries were scored for each provider on its own,
from the provider's raw recorded answer. The "Routing headroom" table above
ran each provider through WSP (dedup and quality pipeline), so its values
differ by up to 0.012; the order is the same.

| first provider | nDCG@5 | latency p50 (recorded) |
|---|---:|---:|
| always Brave | 0.696 | 958 ms |
| always Serper | 0.642 | 1401 ms |
| always Exa | 0.638 | 665 ms |
| always Tavily | 0.552 | 2214 ms |
| v4.3.5 router | 0.672 | |
| best provider per query (oracle) | 0.830 | |

A table "best provider per intent" looks better in-sample (0.738 with the
gold intents) but does not survive leave-one-out: choosing each query's
provider from the other queries of its intent gives 0.679, below always
Brave. Only three choices were the same in every fold: Exa for academic and
docs queries, Serper for shopping. 5.0 routes by exactly these exceptions and
Brave otherwise:

| table | nDCG@5 |
|---|---:|
| Brave + Exa (academic, docs) + Serper (shopping), gold intents | 0.716 |
| the same, intents from `wsp_core/intents.py` | 0.703 |

Against always Brave the difference is +0.007 (95% bootstrap interval -0.010
to +0.028); the gain is over the v4.3.5 router, not over Brave.

The intent detector was built on the 53 tuning queries only and evaluated on
the 241 evaluation queries afterwards. A wrong academic, docs or shopping
label moves a query away from Brave, so those labels need clear cues:

| intent | precision | recall | v4.3.5 router classes: precision / recall |
|---|---:|---:|---:|
| academic | 17/17 | 17/30 | 12/12 / 12/30 |
| docs | 15/15 | 15/31 | 5/9 / 5/31 |
| shopping | 10/10 | 10/30 | 2/2 / 2/30 |
| all eight intents (accuracy) | 153/241 | | 89/241 |

The first held-out run had one wrong docs label ("what are the differences
between Python and Node.js": "Node.js" counted as a file name); the cue was
fixed and the table shows the second run.

End to end (core-4 replay of the recorded answers; both columns run the 5.0
code and differ only in the router):

| | v4.3.5 router | 5.0 routing |
|---|---:|---:|
| nDCG@5, normal (60) | 0.672 | 0.703 |
| authority hit@5, normal (26 with authority domains) | 0.923 | 0.846 |
| latency p50 / p95, normal | 1519 / 3313 ms | 1186 / 1870 ms |
| output tokens, normal | 800 | 1020 |
| nDCG@5, research (26) | 0.680 | 0.681 |

Authority hit@5 changes on four queries, two each way; two of the losses
are docs queries the detector did not label (they stay on Brave). Tokens
rise because Brave returns longer snippets than Serper (description plus up
to two extra snippets, about 780 vs 145 characters). The 5.0 columns
include the snippet cap from the section above.
