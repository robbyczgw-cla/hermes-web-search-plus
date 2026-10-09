# Web Search Plus 5.0.0

5.0 rebuilds automatic routing, speeds up fallback and changes which provider most searches go to. Read the breaking changes before you update.

## What's new

- **Faster.** On 294 live queries the median search takes 565 ms instead of 930 ms, the slowest one 2.1 s instead of 22 s. When a provider fails, answers empty or runs past its p75 latency, the next one starts after at most 2.5 s.
- **Better results.** Two blind judges compared 4.3.5 and 5.0 answer by answer and preferred 5.0 in about two thirds of the queries, in all eight query types.
- **Shorter answers.** Results are about 35 % shorter for the agent; the source a query asks for is found as often as before.
- **Searches in the query's language.** A clearly German or French query is searched in German or French, unless you set a language yourself.
- **Simpler setup.** `setup.py setup` asks for Brave, Serper, Exa and Linkup. Without a key it asks in a terminal (default no) whether to start on Keenable's free public tier.
- **Your own order.** `setup.py config set-order exa,serper,brave` or the Hermes Desktop field "Provider order" uses that order for every query. `setup.py status` shows the order in effect.
- **Clear provider errors.** An empty account reads "Out of credits", a blocked provider says why in words, and the error lists each failed provider with its reason.
- **Domain filters you can trust.** `include_domains` accepts a plain string, ports, IDN domains and suffixes such as `.gov`. A filter without a usable entry is an error instead of an unfiltered search. Tavily cannot filter by suffix and says so; automatic routing skips it for such searches.

## Breaking changes

- **Automatic routing picks providers by query type.** Brave for general, news, local and community queries, Exa for docs and academic, Serper for security and shopping. In the 294-query mix Brave answered 62 % of automatic searches (4.3.5: 14 %). With a Brave key, expect more Brave usage.
- **Engine modules moved into `wsp_core/`.** Code that did `import search` or `import routing` imports from `wsp_core`. The tools, `setup.py`, config.json, `.env` and `python search.py` are unchanged. The Operator Console starts with `python3 -m wsp_core.ui`.
- **Query language reaches the provider** when no language is configured. Set `defaults.locale.language` to keep one language.
- **Removed:** the heuristic router (`adaptive_routing` and `confidence_threshold` are accepted and ignored), shadow routing, the subprocess fallback, reading the pre-v3 JSON search cache from the tools, and Perplexity.

Full list: [CHANGELOG.md](../CHANGELOG.md#v500--2026-10-09).

## Upgrade

```bash
hermes plugins update web-search-plus
python3 setup.py status
```

Configs from 4.x load unchanged. A config that still holds the 4.x default `provider_priority` gets the new order; an order you chose yourself stays.
