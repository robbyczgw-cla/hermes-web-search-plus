#!/usr/bin/env python3
"""Generate docs/ROUTING.md from the routing, intent, quality and cache constants.

The reference is rendered deterministically from the real module constants:
the first-provider table and measured order (routing.py), the intents and
their cue tables (intents.py), the authority domains (quality.py), the cache
caps (cache.py) and the default provider lists (provider_registry.py, config.py).
The document cannot drift from behavior without failing the --check mode used
in CI/tests.

Prose that the code does not carry (the one-line intent descriptions) lives in
this file. The renderer refuses to run when it disagrees with the code, so a
new, renamed or removed intent fails the check instead of silently missing.

Usage:
    python scripts/gen_routing_docs.py            # (re)write docs/ROUTING.md
    python scripts/gen_routing_docs.py --check    # exit 1 when the file drifts
"""

import argparse
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wsp_core import cache, intents, provider_registry, routing  # noqa: E402
from wsp_core.config import DEFAULT_CONFIG  # noqa: E402
from wsp_core.quality import CANONICAL_DOMAIN_RULES  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "docs" / "ROUTING.md"
LINE_WIDTH = 88

# Doc-only prose per intent. The renderer refuses to run when this map and
# intents.INTENTS disagree.
INTENT_DESCRIPTIONS: Dict[str, str] = {
    "academic": "Scholarly sources: papers, preprints, studies, DOIs, peer review, clinical trials, theses, patents.",
    "community": "Opinions and discussions: forums, Reddit, Hacker News, \"has anyone tried\", experience reports, \"is it worth it\".",
    "docs": "Programming and technical documentation: API references, error messages, shell and git commands, code-like tokens, changelogs.",
    "general": "Everything else. Chosen when no other intent reaches its threshold.",
    "local": "Places and weather: opening hours, \"near me\", restaurants and shops, addresses, forecasts, events.",
    "news": "Current events and announcements: news and breaking stories, earnings and market news, releases, sports standings.",
    "security": "Vulnerabilities and attacks: CVE ids, exploits, malware, security advisories, data breaches.",
    "shopping": "Buying and prices: where to buy, price and deals, \"best ... under 300 euros\", product categories, reviews, model numbers.",
}

# Cues that intents.py adds in code rather than in its cue table:
# (name, weight, condition).
EXTRA_CUES: Dict[str, List[Tuple[str, float, str]]] = {
    "local": [
        (
            "place_name",
            1.0,
            "added when another local cue fired and a capitalised word follows a place "
            "preposition such as \"in\", \"near\" or \"bei\" (\"restaurants in Vienna\")",
        ),
    ],
}

# Queries shown as examples. The intent and the first provider are computed
# when the page is generated, so they cannot be wrong.
EXAMPLE_QUERIES: Tuple[str, ...] = (
    "randomized controlled trial of intermittent fasting",
    "how to read a file in python",
    "best headphones under 300 euros",
    "Kopfhörer Preisvergleich",
    "CVE-2024-3094 remote code execution",
    "restaurants near me open now",
    "Wetter Wien morgen",
    "is it worth switching to Linux reddit",
    "what happened at the central bank today",
    "history of the Roman Empire",
)

# Names the generator reads from intents.py (a missing one fails loudly).
_INTENT_NAMES = (
    "INTENTS", "PRECISE_INTENTS", "_SPECS", "_THRESHOLD", "_TIE_ORDER", "_PRECISE_MARGIN", "_MIN_ANCHOR",
)


def anchor(title: str) -> str:
    """GitHub heading anchor for a plain-text heading."""
    return re.sub(r"[^a-z0-9_ -]", "", title.lower()).replace(" ", "-")


def _para(text: str, first: str = "", rest: str = "") -> List[str]:
    """Wrap one paragraph or list item to LINE_WIDTH."""
    return textwrap.wrap(
        text, width=LINE_WIDTH, initial_indent=first, subsequent_indent=rest,
        break_long_words=False, break_on_hyphens=False,
    )


def _bullet(text: str) -> List[str]:
    return _para(text, "- ", "  ")


def _code(items: Iterable[str]) -> str:
    return ", ".join(f"`{item}`" for item in items)


def _and(items: Iterable[str]) -> str:
    """Code-formatted list in prose: `a`, `b` and `c`."""
    names = [f"`{item}`" for item in items]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _num(value: float) -> str:
    return f"{value:.1f}"


def _validate_sources() -> None:
    """Fail loudly when the prose here and the code disagree."""
    missing = [name for name in _INTENT_NAMES if not hasattr(intents, name)]
    if missing:
        raise SystemExit(f"wsp_core/intents.py no longer defines {missing}; update scripts/gen_routing_docs.py")
    known = set(intents.INTENTS)
    described = set(INTENT_DESCRIPTIONS)
    if known != described:
        raise SystemExit(
            "INTENT_DESCRIPTIONS out of sync with wsp_core/intents.py "
            f"(missing: {sorted(known - described)}, stale: {sorted(described - known)}); "
            "update scripts/gen_routing_docs.py"
        )
    tables = {
        "routing.INTENT_FIRST_PROVIDER": routing.INTENT_FIRST_PROVIDER,
        "quality.CANONICAL_DOMAIN_RULES": CANONICAL_DOMAIN_RULES,
        "cache.CLASS_CACHE_TTL": cache.CLASS_CACHE_TTL,
        "intents._THRESHOLD": intents._THRESHOLD,
        "EXTRA_CUES": EXTRA_CUES,
    }
    for label, table in tables.items():
        unknown = sorted(set(table) - known)
        if unknown:
            raise SystemExit(f"{label} names intents that intents.INTENTS does not define: {unknown}")
    unknown_cue_intents = sorted({spec.intent for spec in intents._SPECS} - known)
    if unknown_cue_intents:
        raise SystemExit(f"intents._SPECS names unknown intents: {unknown_cue_intents}")
    no_threshold = sorted(i for i in known - {"general"} if i not in intents._THRESHOLD)
    if no_threshold:
        raise SystemExit(f"intents._THRESHOLD has no entry for: {no_threshold}")


def _first_provider(intent: str) -> str:
    return routing.INTENT_FIRST_PROVIDER.get(intent) or routing.MEASURED_PROVIDER_ORDER[0]


def _then_providers(intent: str) -> List[str]:
    first = _first_provider(intent)
    return [provider for provider in routing.MEASURED_PROVIDER_ORDER if provider != first]


def _exceptions_text() -> str:
    by_provider: Dict[str, List[str]] = {}
    for intent in intents.INTENTS:
        provider = routing.INTENT_FIRST_PROVIDER.get(intent)
        if provider:
            by_provider.setdefault(provider, []).append(intent)
    return "; ".join(
        f"`{provider}` for {_and(names)}" for provider, names in sorted(by_provider.items())
    )


def _cue_families(intent: str) -> List[Tuple[float, List[Any]]]:
    """Cue families of one intent, strongest first, in table order within a weight."""
    families: Dict[str, List[Any]] = {}
    for spec in intents._SPECS:
        if spec.intent == intent:
            families.setdefault(spec.group, []).append(spec)
    ranked = [(max(spec.weight for spec in specs), specs) for specs in families.values()]
    return sorted(ranked, key=lambda item: -item[0])


def _family_text(specs: List[Any]) -> str:
    top = max(spec.weight for spec in specs)
    parts = []
    for spec in sorted(specs, key=lambda s: -s.weight):
        text = f"`{spec.name}`"
        if spec.weight < top:
            text += f" ({_num(spec.weight)})"
        if spec.multi:
            text += " (grows to 2x with more matches)"
        parts.append(text)
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _first_provider_section() -> List[str]:
    measured = routing.MEASURED_PROVIDER_ORDER
    lines = ["## How the first provider is chosen", ""]
    lines += _para(
        "Automatic routing makes one decision: which provider to try first. It looks only at "
        "the query text and your configuration. It calls no model and no provider, and it "
        "does not score providers per query. A search with an explicit provider skips routing."
    )
    lines.append("")
    lines += _para("The query is assigned one of eight intents ([details](#the-eight-intents)).", "1. ", "   ")
    lines += _para(
        "The router builds an ordered list: the first-provider rule for that intent (if the "
        "intent has one), then the measured order, then `auto_routing.provider_priority`. "
        "Repeated providers are dropped.", "2. ", "   ")
    lines += _para(
        "The first provider in that list that is eligible is chosen. A provider is eligible "
        "when it is configured (an API key is set, or a keyless public endpoint is opted in), "
        "is not in `disabled_providers`, and is allowed for automatic use (`auto_allow`).",
        "3. ", "   ")
    lines += _para(
        "If no provider is eligible, `fallback_provider` is used and the reason is "
        "`no_available_providers`. If that provider has no key either, the search fails with "
        "an error.", "4. ", "   ")
    lines += ["", "With all four measured providers configured and allowed, the result is:", ""]
    lines += ["| Intent | First provider | Rule | Next in line |", "|---|---|---|---|"]
    for intent in intents.INTENTS:
        rule = "intent rule" if intent in routing.INTENT_FIRST_PROVIDER else "measured order"
        lines.append(
            f"| `{intent}` | `{_first_provider(intent)}` | {rule} | "
            f"{_code(_then_providers(intent))}, then `provider_priority` |"
        )
    lines.append("")
    lines += _para(
        f"The measured order is {_code(measured)}. If the first provider in a row is not "
        "eligible, the next one in the row is chosen."
    )
    lines += ["", "Two consequences:", ""]
    lines += _bullet(
        "Only the intent changes the choice. Confidence, query length, language and recency "
        "are reported with the decision but do not affect it."
    )
    lines += _bullet(
        f"`provider_priority` does not move a provider ahead of the measured order. A list "
        f"that starts with `{measured[-1]}` still leaves `{measured[0]}` first while "
        f"`{measured[0]}` is eligible. To keep a provider from being chosen first, put it in "
        "`disabled_providers` or set its `auto_allow` entry to `false`. To use one provider for "
        "every search, turn automatic routing off and set `default_provider` (see "
        "[Configuration](#configuration))."
    )
    lines.append("")
    return lines


def _measured_order_section() -> List[str]:
    measured = routing.MEASURED_PROVIDER_ORDER
    lines = ["## The measured order", ""]
    lines += _para(
        f"The measured order is {_code(measured)}, best first. The numbers behind it are in "
        "[benchmarks/RESULTS.md](../benchmarks/RESULTS.md), section \"Routing 5.0 (intent "
        "table)\". The query sets and tools are described in "
        "[benchmarks/README.md](../benchmarks/README.md). This page does not copy the numbers."
    )
    lines += ["", "How it was measured:", ""]
    lines += _bullet(
        "Each provider answered the same recorded set of queries. The set covers the eight "
        "intents and includes queries that are not in English."
    )
    lines += _bullet(
        "Every URL a provider returned was graded for relevance from 0 to 3 by a language "
        "model, from title, URL and snippet. Each provider's answers are scored with nDCG@5. "
        "The measured order is the order of these scores."
    )
    lines += _bullet(
        "A table that picks the best provider per intent scored better on the queries it was "
        "fitted to. Tested with leave-one-out (each query's provider chosen from the other "
        "queries of its intent), it fell below always using the first provider of the "
        f"measured order. Only these choices were the same in every fold: {_exceptions_text()}. "
        "They are the intent rules in the table above. Nothing finer is used."
    )
    lines += ["", "What the order does not show:", ""]
    lines += _bullet(
        "It comes from one recording: a fixed query set, one day of provider answers, one "
        "region, one judge, snippet-level grades. Small differences are noise."
    )
    lines += _bullet("It ranks provider answers for those queries, not the providers in general.")
    lines += _bullet(
        "The reported gain is over the 4.x router. Against always using the first provider of "
        "the measured order, the difference is within the noise (RESULTS.md gives the "
        "interval). RESULTS.md also lists measures where the new routing is not better, for "
        "example the share of queries whose top 5 contain an expected authority domain."
    )
    lines.append("")
    return lines


def _fallback_section() -> List[str]:
    default_priority = list(provider_registry._BUILTIN_DEFAULT_PROVIDER_PRIORITY)
    measured = list(routing.MEASURED_PROVIDER_ORDER)
    legacy = list(provider_registry.PRE_5_DEFAULT_PROVIDER_PRIORITY)
    fallback_provider = DEFAULT_CONFIG["auto_routing"]["fallback_provider"]

    lines = ["## The fallback chain", ""]
    lines += _para(
        "The chain after the first provider follows `auto_routing.provider_priority`, not the "
        "measured order. Providers that were already tried, are disabled, are not auto-allowed "
        "or are not configured are skipped."
    )
    lines.append("")
    lines += _bullet(
        "In the `web_search_plus` tool, the next provider starts when the running one fails, "
        "returns no results, or is slower than its usual latency. At most two searches run at "
        "once, and the first non-empty answer wins."
    )
    lines += _bullet(
        "In `python search.py`, providers are tried one after another, and the next one is "
        "tried when a provider fails."
    )
    lines += _bullet(
        "An explicit `--provider NAME` is strict: it does not fall back unless you pass "
        "`--allow-fallback`. With automatic routing off, the fixed provider is strict too."
    )
    lines += _bullet(
        "Research mode (`mode=\"research\"`) searches up to three providers. The first is "
        "chosen as above. The others come from `select_research_providers` in "
        "`wsp_core/quality.py`, which has its own preference list and does not use the "
        "measured order."
    )
    lines += _bullet(
        "A provider that keeps failing is skipped for a while at call time. That happens "
        "after routing and is not part of the routing decision."
    )
    lines += _bullet(
        f"`fallback_provider` (default `{fallback_provider}`) is not part of this chain. It is "
        "used only when no provider is eligible at all."
    )
    lines += ["", "### provider_priority", ""]
    lines += _para(
        "`auto_routing.provider_priority` is a list of provider ids. It sets the order of the "
        "fallback chain. It also decides the first provider when none of the measured "
        "providers is eligible."
    )
    lines.append("")
    default_text = f"The default is {_code(default_priority)}."
    if default_priority[: len(measured)] == measured:
        default_text += " It starts with the measured order."
        if default_priority[len(measured):] == [p for p in legacy if p not in measured]:
            default_text += " The remaining providers keep their 4.x relative order."
    default_text += (
        " Providers loaded from `providers.d` are appended only if their declaration opts in "
        "to automatic use."
    )
    lines += _para(default_text)
    lines.append("")
    lines += _para(
        "When automatic routing is enabled, providers that are missing from a custom list are "
        "appended in default order, so a short list does not remove providers from the chain."
    )
    lines += ["", "#### Migration from the 4.x default", ""]
    lines += _para(
        f"4.x setups wrote this list into `config.json`: {_code(legacy)}. A "
        "`provider_priority` that starts with exactly this list is treated as never customized. "
        "When the config is loaded it is replaced by the current default. Providers that come after the "
        "old list are kept, after the entries of the new default. Any other list is used as "
        "written."
    )
    lines.append("")
    return lines


def _intent_section(intent: str) -> List[str]:
    lines = [f"### `{intent}`", ""]
    lines += _para(INTENT_DESCRIPTIONS[intent])
    lines.append("")
    first = routing.INTENT_FIRST_PROVIDER.get(intent)
    lines.append(
        f"- **First provider:** `{first}` (intent rule)." if first
        else f"- **First provider:** `{routing.MEASURED_PROVIDER_ORDER[0]}` (measured order)."
    )
    if intent == "general":
        lines += ["- **Cues:** none.", ""]
        return lines
    lines.append(f"- **Threshold:** a score of at least {_num(intents._THRESHOLD[intent])}.")
    if intent in intents.PRECISE_INTENTS:
        lines += _bullet(
            f"**Precise intent:** also needs a lead of at least {_num(intents._PRECISE_MARGIN)} "
            f"over every other intent and one cue family of weight {_num(intents._MIN_ANCHOR)} or more."
        )
    lines += ["", "Cue families, strongest first:", ""]
    tiers: Dict[float, List[str]] = {}
    for weight, specs in _cue_families(intent):
        tiers.setdefault(weight, []).append(_family_text(specs))
    for weight, texts in tiers.items():
        lines += _bullet(f"**{_num(weight)}:** " + ", ".join(texts))
    for name, weight, condition in EXTRA_CUES.get(intent, []):
        lines += _bullet(f"**Also:** `{name}` ({_num(weight)}), {condition}")
    lines.append("")
    return lines


def _example_rows() -> List[str]:
    rows = ["| Query | Intent | First provider | Cues that fired |", "|---|---|---|---|"]
    for query in EXAMPLE_QUERIES:
        decision = intents.classify_intent(query)
        shown = list(decision.signals[:3])
        if decision.intent != "general":
            shown = [signal.split(":", 1)[1] for signal in shown]
        cue_text = (_code(shown) + (", ..." if len(decision.signals) > 3 else "")) if shown else "none"
        rows.append(f"| {query} | `{decision.intent}` | `{_first_provider(decision.intent)}` | {cue_text} |")
    return rows


def _intents_section() -> List[str]:
    precise = [intent for intent in intents.INTENTS if intent in intents.PRECISE_INTENTS]
    thresholds: Dict[float, List[str]] = {}
    for intent in intents.INTENTS:
        if intent in intents._THRESHOLD:
            thresholds.setdefault(intents._THRESHOLD[intent], []).append(intent)
    threshold_text = "; ".join(
        f"{_num(value)} for {_code(names)}" for value, names in sorted(thresholds.items(), reverse=True)
    )
    lines = ["## The eight intents", ""]
    lines += _para(
        "`classify_intent` in `wsp_core/intents.py` labels every query with one of these "
        f"intents: {_code(intents.INTENTS)}. It is a table of weighted cues, matched with "
        "regular expressions on the lower-cased, accent-stripped query. The cues are words, "
        "phrases and code-like tokens in English, German, French, Spanish and Italian. It is "
        "deterministic and local."
    )
    lines += ["", "How a label is chosen:", ""]
    lines += _bullet(
        "Every cue has a weight. Cues of one family (synonyms, or one concept in several "
        "languages) count once, at the highest weight in the family. An intent's score is the "
        "sum of its matched families."
    )
    lines += _bullet(f"An intent qualifies at its threshold: {threshold_text}.")
    lines += _bullet(
        f"{_and(precise)} are precise intents. A wrong label sends the query away from the "
        "default, so they need more evidence than the threshold alone: a lead over every other "
        "intent and one strong cue family. Weak cues cannot add up to one of them."
    )
    lines += _bullet(
        "The qualifying intent with the highest score wins. A tie goes to the first in this "
        f"order: {_code(intents._TIE_ORDER)}."
    )
    lines += _bullet("If no intent qualifies, the label is `general`.")
    lines += _bullet(
        "The confidence reported with the label is a heuristic between 0 and 1, not a "
        "probability. It is reported and does not change the routing."
    )
    lines += _bullet(
        "In the cue lists below, `/` joins the cues of one family, and a number in brackets is "
        "the weight of a cue that is weaker than the family's top weight. The cue names are the "
        "ones shown in `signals` by `--explain-routing`."
    )
    lines.append("")
    lines += _para(
        "Examples, computed when this page is generated. The first provider assumes all four "
        "measured providers are configured:"
    )
    lines.append("")
    lines += _example_rows()
    lines.append("")
    for intent in intents.INTENTS:
        lines += _intent_section(intent)
    return lines


def _domain_section() -> List[str]:
    lines = ["## Authority domains", ""]
    lines += _para(
        "After a provider answers, results are reranked for the intents below. Results from a "
        "boosted domain move up and results from a demoted domain move down. Within each group "
        "the provider's order is kept. If the query contains \"official\", results whose title "
        "or snippet contains it also move up slightly. `--quality-report` uses the same lists "
        "to report whether the top result is from an authority domain."
    )
    lines.append("")
    lines += _para(
        "The reranker uses the intent of the routing decision, so it applies to automatically "
        "routed searches only. A search with an explicit provider has no routing decision and "
        "is not reranked."
    )
    lines.append("")
    lines += _para(
        "A rule that ends in a dot (`docs.`) matches hosts that start with that label "
        "(`docs.python.org`). Any other rule matches the domain and its subdomains. A rule "
        "with a slash (`github.com/advisories`) matches a URL path prefix."
    )
    lines.append("")
    ruled = [intent for intent in intents.INTENTS if CANONICAL_DOMAIN_RULES.get(intent)]
    for intent in ruled:
        rules = CANONICAL_DOMAIN_RULES[intent]
        lines += [f"### `{intent}`", ""]
        if rules.get("boost"):
            lines += _bullet("**Boost:** " + _code(rules["boost"]))
        if rules.get("demote"):
            lines += _bullet("**Demote:** " + _code(rules["demote"]))
        lines.append("")
    unruled = [intent for intent in intents.INTENTS if intent not in ruled]
    lines += _para(f"No reranking for {_code(unruled)}.")
    lines.append("")
    return lines


def _cache_section() -> List[str]:
    lines = ["## Cache lifetime", ""]
    lines += _para(
        f"Search results are cached for {cache.DEFAULT_CACHE_TTL} seconds by default (the "
        "`--cache-ttl` option changes it). Some intents go stale faster, so their lifetime "
        "is capped:"
    )
    lines += ["", "| Intent | Cache cap |", "|---|---|"]
    for intent in intents.INTENTS:
        ttl = cache.CLASS_CACHE_TTL.get(intent)
        lines.append(f"| `{intent}` | {f'{ttl} seconds' if ttl else 'no intent cap'} |")
    freshness = ", ".join(f"`{name}` {seconds}s" for name, seconds in cache.FRESHNESS_CACHE_TTL.items())
    lines.append("")
    lines += _para(
        "An intent cap only lowers the lifetime. Other caps apply as well, and the shortest "
        f"wins: a freshness filter ({freshness}), and recency words in the query such as "
        "\"latest\" or \"today\". The intent used is the one from the routing decision or, for "
        "an explicit provider, the one computed from the query."
    )
    lines.append("")
    return lines


def _config_section() -> List[str]:
    auto = DEFAULT_CONFIG["auto_routing"]
    default_priority = list(provider_registry._BUILTIN_DEFAULT_PROVIDER_PRIORITY)
    off_by_default = [
        spec.provider
        for spec in provider_registry._BUILTIN_PROVIDER_SPECS
        if spec.supports_search and not spec.auto_allowed_by_default
    ]
    enabled = "true" if auto["enabled"] else "false"
    lines = ["## Configuration", ""]
    lines += _para("These keys in `config.json` affect automatic routing.")
    lines += ["", "| Key | Default | Effect |", "|---|---|---|"]
    lines += [
        f"| `auto_routing.enabled` | `{enabled}` | When `false`, routing does not run. Every search "
        "without an explicit provider uses `default_provider`, with no fallback. Without "
        "`default_provider`, routing returns no provider (`auto_routing_disabled_no_default_provider`). |",
        "| `default_provider` | none | A top-level key. Used only when `auto_routing.enabled` is "
        "`false`. It cannot also be in `disabled_providers`. |",
        f"| `auto_routing.provider_priority` | {_code(default_priority)} | Order of the fallback "
        "chain, and of the first provider when no measured provider is eligible. See "
        "[The fallback chain](#the-fallback-chain). |",
        "| `auto_routing.disabled_providers` | `[]` | Providers that automatic routing and fallback "
        "never choose. |",
        f"| `auto_routing.auto_allow` | {_code(off_by_default)} are `false`; so are `providers.d` "
        "providers that do not opt in; all others are `true` | Per provider, `true` or `false`. A "
        "provider set to `false` is never chosen by automatic routing or fallback. Explicit "
        "`--provider` calls still work. |",
        f"| `auto_routing.fallback_provider` | `{auto['fallback_provider']}` | Used only when no "
        "provider is eligible. It is tried even if it has no key, and then the search fails with "
        "an error. |",
        f"| `auto_routing.confidence_threshold` | `{auto['confidence_threshold']}` | Accepted, and "
        "checked to be between 0.0 and 1.0. It has no effect. |",
        "| `auto_routing.adaptive_routing` | unset | Accepted. It has no effect: the first provider "
        "is not chosen from past latency or errors. |",
        "",
    ]
    return lines


def _inspect_section() -> List[str]:
    lines = ["## Inspecting a decision", "", "```bash", "python search.py --explain-routing -q \"your query\"", "```", ""]
    lines += _para(
        "`-q` is short for `--query`, which is required. The command prints JSON and makes no "
        "provider call. It uses your `config.json` and environment, so it shows what would "
        "happen on your machine."
    )
    lines.append("")
    lines += _bullet(
        "`routing_decision.provider` is the first provider. `reason` is `intent_<name>` when a "
        "cue fired, `no_signals_matched` when none did, or `no_available_providers`."
    )
    lines += _bullet(
        "`routing_decision.confidence` and `confidence_level` come from the intent detector. "
        "They are reported only."
    )
    lines += _bullet(
        "`routing_decision.candidate_order` lists the providers the router considered, in its "
        "order. `auto_allow_excluded` lists configured providers that automatic routing skips "
        "because of `auto_allow`."
    )
    lines += _bullet(
        "`intent` shows the intent and its `signals`, written `<intent>:<cue>`. The cue names "
        "are the ones listed in [The eight intents](#the-eight-intents)."
    )
    lines += _bullet(
        "`first_provider_rules` and `measured_order` are the two tables from this page as the "
        "running code has them."
    )
    lines += _bullet(
        "`query_analysis` shows the language hint and whether the query asks for recent "
        "results. `available_providers` lists the providers automatic routing may use."
    )
    lines.append("")
    lines += _para(
        "To see what a real search did, add `--quality-report` to a search. The JSON then has a "
        "`quality_report` object with `selected_provider`, `routing_reason`, `routing_policy`, "
        "`routing_class` (the intent), the providers considered and skipped, and the authority "
        "signals. See the [FAQ](FAQ.md#how-do-i-debug-routing-decisions)."
    )
    lines.append("")
    return lines


def render_routing_docs() -> str:
    _validate_sources()
    measured = routing.MEASURED_PROVIDER_ORDER
    sections = [
        ("How the first provider is chosen", _first_provider_section),
        ("The measured order", _measured_order_section),
        ("The fallback chain", _fallback_section),
        ("The eight intents", _intents_section),
        ("Authority domains", _domain_section),
        ("Cache lifetime", _cache_section),
        ("Configuration", _config_section),
        ("Inspecting a decision", _inspect_section),
    ]
    lines = [
        "# Routing Reference",
        "",
        "<!-- AUTO-GENERATED by scripts/gen_routing_docs.py - do not edit by hand.",
        "     Regenerate with: python scripts/gen_routing_docs.py -->",
        "",
    ]
    lines += _para(
        "This page explains how automatic provider selection (`provider=\"auto\"`, the default) "
        f"works in routing policy `{routing.ROUTING_POLICY}`. It is generated from "
        "`wsp_core/routing.py`, `wsp_core/intents.py`, `wsp_core/quality.py`, "
        "`wsp_core/cache.py` and `wsp_core/provider_registry.py`, and a test fails when it "
        "drifts from them."
    )
    lines.append("")
    exceptions = _exceptions_text()
    lines += _para(
        f"In short: the first provider is `{measured[0]}`, "
        + (f"with these exceptions: {exceptions}. " if exceptions else "with no exceptions. ")
        + "Everything after the first provider follows `provider_priority`."
    )
    lines += ["", "Contents:", ""]
    lines += [f"{number}. [{title}](#{anchor(title)})" for number, (title, _) in enumerate(sections, 1)]
    lines.append("")
    for _, render in sections:
        lines += render()
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/ROUTING.md from the routing, intent, quality and cache constants")
    parser.add_argument("--check", action="store_true", help="Exit 1 when docs/ROUTING.md drifts from the generator output")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="Output path (default: docs/ROUTING.md)")
    args = parser.parse_args()

    content = render_routing_docs()
    if args.check:
        existing = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if existing != content:
            print(f"DRIFT: {args.out} is stale; regenerate with: python scripts/gen_routing_docs.py", file=sys.stderr)
            return 1
        print(f"OK: {args.out} matches the generator output")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
