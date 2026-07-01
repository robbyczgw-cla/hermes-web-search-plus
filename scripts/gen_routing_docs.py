#!/usr/bin/env python3
"""Generate docs/ROUTING.md from the Routing v2 data structures.

The reference is rendered deterministically from the real module constants in
routing.py (class rules, provider boosts) and quality.py (canonical domain
rules), so the document cannot drift from behavior without failing the
--check mode used in CI/tests.

Usage:
    python scripts/gen_routing_docs.py            # (re)write docs/ROUTING.md
    python scripts/gen_routing_docs.py --check    # exit 1 when the file drifts
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routing  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402
from quality import CANONICAL_DOMAIN_RULES  # noqa: E402


DEFAULT_OUTPUT = REPO_ROOT / "docs" / "ROUTING.md"

# Maximum number of representative signal patterns shown per class.
MAX_SIGNAL_EXAMPLES = 5

# Doc-only prose per routing class. The renderer refuses to run when this map
# and the classes defined in routing.py disagree, so a new/renamed class fails
# the drift check instead of silently missing from the reference.
ROUTING_CLASS_DESCRIPTIONS: Dict[str, str] = {
    "briefing_synthesis": "Comparison, summary, and briefing queries that want a synthesized overview rather than a single page.",
    "security_advisory": "Security advisories and CVE lookups (OpenSSL/OpenSSH vulnerabilities, mitigations, zero-days).",
    "patents": "Patent lookups and patent-office searches (USPTO, Espacenet, PATENTSCOPE, Google Patents).",
    "policy_pdf": "Policy and regulatory PDF documents, such as NIST publications or EU AI Act guidance.",
    "official_regulatory": "Official regulatory information: EU AI Act, commission obligations, regulatory filings, public authorities.",
    "finance_earnings_official": "Official earnings and investor-relations material (quarterly results, guidance, 10-Q/10-K, revenue).",
    "finance_investor_monthly": "Monthly investor reports, factsheets, AUM, and fund-flow updates.",
    "reddit_community": "Reddit-specific queries: site:reddit.com constraints, r/... subreddits, thread and discussion lookups.",
    "shopping_reviews_local": "Shopping queries for concrete products with explicit review/test/comparison intent, often AT/DE local.",
    "shopping_specs": "Shopping, price, and spec queries for concrete products without explicit review intent.",
    "community_forum": "Forum and community discussion queries (Head-Fi, AudioScienceReview, hifi-forum, Erfahrungen).",
    "academic_arxiv": "Academic discovery: arXiv, papers, randomized trials, primary sources.",
    "github_docs": "GitHub repositories and plugin documentation lookups.",
    "official_docs": "Official documentation, API references, and developer docs.",
    "official_vendor_release": "Official releases and announcements from major AI/tech vendors (Mistral, Anthropic, OpenAI, ...).",
    "docs_api": "Package and API documentation, changelogs, and release notes (Python, Pydantic, Node.js, ...).",
    "local_at": "Local Austrian queries: Graz, opening hours, addresses, restaurants.",
    "sports_current": "Current sports standings, fixtures, lineups, and scores (Bundesliga and friends).",
    "weather_local": "Weather and forecast queries.",
    "oss_discovery": "Discovery of alternatives, competitors, and open-source or self-hosted tools.",
    "multilingual_current": "Fallback for queries in languages other than English/German; provider boosts depend on the detected script.",
    "general": "Default class when no rule matches; the base intent-signal scores decide on their own.",
    "shopping_at": "Legacy shopping class kept for boost compatibility; no detection rule currently emits this label.",
}


def _split_top_level_alternation(pattern: str) -> List[str]:
    """Split a regex on ``|`` at nesting depth zero, respecting escapes."""
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\" and i + 1 < len(pattern):
            current.append(pattern[i:i + 2])
            i += 2
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    parts.append("".join(current))
    return parts


def _is_wrapped_group(text: str) -> bool:
    """Return True when the whole text is a single ``(...)`` group."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
        i += 1
    return False


def _pattern_examples(pattern: str) -> List[str]:
    """Extract readable example alternatives from one signal regex."""
    examples: List[str] = []
    for part in _split_top_level_alternation(pattern):
        part = part.strip()
        while part.startswith(r"\b"):
            part = part[2:]
        while part.endswith(r"\b"):
            part = part[:-2]
        if _is_wrapped_group(part):
            inner = part[1:-1]
            if inner.startswith("?:"):
                inner = inner[2:]
            if "|" in inner:
                examples.extend(_pattern_examples(inner))
                continue
            part = inner
        if part:
            examples.append(part)
    return examples


def _signal_example_lines(rule_groups: List[Tuple[str, ...]]) -> List[str]:
    """Render up to MAX_SIGNAL_EXAMPLES representative patterns per class.

    ``rule_groups`` holds one tuple of regex pattern groups per rule entry for
    the class; within one entry every group must match (logical AND).
    """
    lines: List[str] = []
    for groups in rule_groups:
        count = len(groups)
        quotas = [
            MAX_SIGNAL_EXAMPLES // count + (1 if i < MAX_SIGNAL_EXAMPLES % count else 0)
            for i in range(count)
        ]
        rendered_groups = []
        for pattern, quota in zip(groups, quotas):
            terms = _pattern_examples(pattern)[:max(quota, 1)]
            rendered_groups.append(", ".join(f"`{term}`" for term in terms))
        if count > 1:
            lines.append(" **AND** ".join(f"({group})" for group in rendered_groups) + " (all parts must match)")
        else:
            lines.append(rendered_groups[0])
    return lines


def _format_boost(value: float) -> str:
    return f"{value:+g}"


def _provider_lines(boosts: List[Tuple[str, float]]) -> Tuple[str, str]:
    preferred = [f"`{provider}` ({_format_boost(value)})" for provider, value in boosts if value > 0]
    demoted = [f"`{provider}` ({_format_boost(value)})" for provider, value in boosts if value < 0]
    return (
        ", ".join(preferred) if preferred else "none",
        ", ".join(demoted) if demoted else "none",
    )


def _domain_lines(routing_class: str) -> List[str]:
    rules = CANONICAL_DOMAIN_RULES.get(routing_class)
    if not rules:
        return []
    lines = []
    boost = rules.get("boost") or []
    demote = rules.get("demote") or []
    if boost:
        lines.append("- **Boost domains:** " + ", ".join(f"`{domain}`" for domain in boost))
    if demote:
        lines.append("- **Demote domains:** " + ", ".join(f"`{domain}`" for domain in demote))
    return lines


def _all_routing_classes() -> List[str]:
    """Every class name in detection order, then fallbacks, then boost-only classes."""
    ordered: List[str] = []
    for name, _ in routing.ROUTING_CLASS_RULES:
        if name not in ordered:
            ordered.append(name)
    for name in (routing.MULTILINGUAL_ROUTING_CLASS, routing.DEFAULT_ROUTING_CLASS):
        if name not in ordered:
            ordered.append(name)
    for name in sorted(routing.ROUTING_CLASS_PROVIDER_BOOSTS):
        if name not in ordered:
            ordered.append(name)
    return ordered


def _validate_descriptions(classes: List[str]) -> None:
    missing = sorted(set(classes) - set(ROUTING_CLASS_DESCRIPTIONS))
    stale = sorted(set(ROUTING_CLASS_DESCRIPTIONS) - set(classes))
    if missing or stale:
        raise SystemExit(
            "ROUTING_CLASS_DESCRIPTIONS out of sync with routing.py "
            f"(missing: {missing}, stale: {stale}); update scripts/gen_routing_docs.py"
        )


def _class_section(routing_class: str) -> List[str]:
    lines = [f"### `{routing_class}`", "", ROUTING_CLASS_DESCRIPTIONS[routing_class], ""]

    rule_groups = [groups for name, groups in routing.ROUTING_CLASS_RULES if name == routing_class]
    if rule_groups:
        for example_line in _signal_example_lines(rule_groups):
            lines.append(f"- **Example signals:** {example_line}")
    elif routing_class == routing.MULTILINGUAL_ROUTING_CLASS:
        lines.append("- **Example signals:** assigned when the language/script hint is neither `en` nor `de`.")
    elif routing_class == routing.DEFAULT_ROUTING_CLASS:
        lines.append("- **Example signals:** assigned when no class rule matches an `en`/`de` query.")
    else:
        lines.append("- **Example signals:** none (no detection rule; class-boost table entry only).")

    if routing_class == routing.MULTILINGUAL_ROUTING_CLASS:
        for hint in sorted(routing.LANGUAGE_HINT_PROVIDER_BOOSTS):
            preferred, demoted = _provider_lines(routing.LANGUAGE_HINT_PROVIDER_BOOSTS[hint])
            label = "other scripts" if hint == "default" else f"`{hint}`"
            lines.append(f"- **Preferred providers ({label}):** {preferred}")
            if demoted != "none":
                lines.append(f"- **Demoted providers ({label}):** {demoted}")
        lines.append("- `you` additionally gains up to +3 from the recency score.")
    else:
        boosts = routing.ROUTING_CLASS_PROVIDER_BOOSTS.get(routing_class)
        if boosts:
            preferred, demoted = _provider_lines(boosts)
            lines.append(f"- **Preferred providers:** {preferred}")
            lines.append(f"- **Demoted providers:** {demoted}")
        else:
            lines.append("- **Preferred providers:** none (base intent-signal scores decide).")

    lines.extend(_domain_lines(routing_class))
    lines.append("")
    return lines


def render_routing_docs() -> str:
    classes = _all_routing_classes()
    _validate_descriptions(classes)

    auto_routing = DEFAULT_CONFIG["auto_routing"]
    fallback_provider = auto_routing["fallback_provider"]
    confidence_threshold = auto_routing["confidence_threshold"]

    lines = [
        "# Routing v2 Reference",
        "",
        "<!-- AUTO-GENERATED by scripts/gen_routing_docs.py - do not edit by hand.",
        "     Regenerate with: python scripts/gen_routing_docs.py -->",
        "",
        f"This reference is generated from the `{routing.ROUTING_POLICY}` data structures in",
        "`routing.py` and `quality.py`. It documents how automatic provider selection",
        "(`provider=\"auto\"`) decides, class by class.",
        "",
        "## How Routing v2 decides",
        "",
        "Routing is rule-based, in four stages:",
        "",
        "1. **Classification** — the query is scanned for weighted intent signals",
        "   (shopping, research, discovery, local/news, RAG, privacy, source-grounding,",
        "   deep search/reasoning) plus complexity, recency, and a language/script hint.",
        "   A coarse routing class is assigned by the ordered rules below: the first",
        "   rule whose pattern groups all match wins. Non-`en`/`de` queries without a",
        f"   matching rule fall back to `{routing.MULTILINGUAL_ROUTING_CLASS}`, everything else to",
        f"   `{routing.DEFAULT_ROUTING_CLASS}`.",
        "2. **Provider scoring** — intent-signal scores map to per-provider scores; the",
        "   routing class then adds the conservative, benchmark-derived boosts and",
        "   penalties listed below, and bounded adaptive adjustments from recent",
        "   provider performance may break close calls.",
        "3. **Auto-allow gate** — providers without an API key, providers listed in",
        "   `disabled_providers`, and providers with `auto_allow=false` are removed",
        "   from automatic selection (explicit `provider=...` calls still work). See",
        "   [Architecture](ARCHITECTURE.md#auto-allow-gate).",
        "4. **Winner and fallback** — the highest-scoring remaining provider wins, with",
        "   deterministic per-query tie-breaking over `provider_priority`. When no",
        "   provider is eligible, the configured fallback provider (default:",
        f"   `{fallback_provider}`) is used and the decision is reported as",
        "   `no_available_providers`.",
        "",
        "### Confidence levels",
        "",
        "Confidence combines the normalized winning score (60%) with the relative",
        "margin over the runner-up (40%):",
        "",
        "- **high** — confidence >= 0.7: a strong, clear winner.",
        "- **medium** — confidence >= 0.4: a plausible winner without a large margin.",
        "- **low** — confidence < 0.4: weak or ambiguous signals.",
        "",
        f"Decisions below `confidence_threshold` (default: {confidence_threshold}) are additionally",
        "flagged as `below_threshold`.",
        "",
        "### Debugging routing decisions",
        "",
        "```bash",
        "python3 search.py --explain-routing -q \"your query\"",
        "```",
        "",
        "This prints the winning provider, per-provider scores, matched signals, the",
        "detected routing class, and any `auto_allow_excluded` providers. `--quality-report`",
        "adds post-retrieval diagnostics. See the",
        "[FAQ](FAQ.md#how-do-i-debug-routing-decisions) for details.",
        "",
        "## Routing classes",
        "",
        "Classes are listed in detection order (first match wins), followed by the",
        "fallback and boost-only classes. Provider boosts are additive score",
        "adjustments; negative values demote a provider for that class. Boost/demote",
        "domains come from `CANONICAL_DOMAIN_RULES` in `quality.py` and rerank results",
        "after retrieval for classes where source authority matters.",
        "",
    ]
    for routing_class in classes:
        lines.extend(_class_section(routing_class))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/ROUTING.md from routing.py/quality.py")
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
