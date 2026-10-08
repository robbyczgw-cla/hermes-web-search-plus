# Benchmarks

Query sets and method for measuring Web Search Plus changes. Every 5.0 change
that claims a gain reports a before/after number produced with these tools.

## Query sets

- `queries.jsonl` — 241 evaluation queries in eight intents (news, docs,
  academic, security, shopping, local, community, general), 54 of them not in
  English. Fields: `id`, `intent` (gold label), `lang`, `query`, optional
  `recency` (`day`/`week`/`month`: the query needs fresh results),
  `authority_domains` (domains a good answer usually contains) and `tags`
  (`known_misroute`, `finance`, `recipe`, ...).
- `tuning_queries.jsonl` — 53 queries from the May 2026 provider benchmarks
  that Routing v2 was tuned on. Reported separately: a router can look good
  on the queries it was fitted to.

The queries are written for this benchmark and contain no user data.

## Tools (`scripts/wsp_eval/`)

- `worker.py` loads one checkout the way Hermes does (`hermes_plugins.<slug>`)
  and calls the real tool handlers. Provider HTTP goes through a replaceable
  `http.client` connection: `synthetic` (deterministic fake answers),
  `replay` (recorded answers, with recorded latency) or `record` (real
  network, every exchange saved without credentials). Each checkout runs in
  its own process.
- `lock.py` freezes routing decisions and tool output.
  `tests/test_behaviour_lock.py` fails when a change alters either. Changes
  that alter behaviour on purpose regenerate the lock and show the diff.
- `overhead.py` measures WSP's own per-call cost with zero-latency providers.
- `judge.py` pools every URL the recorded providers returned per query, has a
  judge model grade each once (0-3), and scores any worker run against those
  grades (nDCG@5, authority hit@5). Results: [RESULTS.md](RESULTS.md).

## Metrics

- Result quality: nDCG@5 against pooled relevance judgments (every URL any
  recorded provider returned for a query is judged once, so variants are
  scored without new live calls); authority hit rate from
  `authority_domains`.
- Latency: p50/p95 per call, replayed with recorded provider latency, plus
  fault scenarios (provider hangs, returns nothing, returns 503).
- Fallback rate: share of calls answered by a provider other than the first
  choice, and share of calls that return no results.
- Tokens per answer: size of the text the model receives
  (`approx_tokens`: words plus punctuation runs, stable across versions).

Recorded provider answers stay out of the repository (`eval/` is ignored).
