#!/usr/bin/env python3
"""Evaluate the query-intent detector (``wsp_core.intents``) against a labelled query set.

    python scripts/wsp_eval/intent_eval.py                  # tuning set (benchmarks/tuning_queries.jsonl)
    python scripts/wsp_eval/intent_eval.py --set heldout    # held-out set (benchmarks/queries.jsonl)
    python scripts/wsp_eval/intent_eval.py --errors         # also list every wrong label

The detector is compared with a baseline: the v4 router's ``routing_class``
(``QueryAnalyzer.analyze(query)["routing_class"]``, the value the router puts in
``analysis_summary``) mapped to the same eight intents with ``BASELINE_MAP``.

Printed: accuracy, the 8x8 confusion matrix (rows gold, columns predicted),
per-intent precision/recall/F1, precision and recall of academic/docs/shopping
(the labels that move a query away from the Brave default), per-language
accuracy, the routing-group view (Exa first / Serper first / Brave first), a
confidence summary, and the queries wrongly labelled academic, docs or shopping.

The tuning set is the only one to look at while designing the detector. The
held-out set is for one final run; ``--set heldout`` says so before it prints.
No network access, no provider keys.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from wsp_core import intents  # noqa: E402

SETS = {
    "tuning": REPO / "benchmarks" / "tuning_queries.jsonl",
    "heldout": REPO / "benchmarks" / "queries.jsonl",
}
INTENTS = intents.INTENTS
PRECISE = ("academic", "docs", "shopping")

# routing_class (QueryAnalyzer, ROUTING_CLASS_RULES plus the two fallbacks) -> intent.
# Defined from the class names and the provider boosts the router attaches to them,
# before looking at any result: classes the router treats as "documentation" or
# "academic search" map there, vendor/finance/sports news maps to news, and classes
# without a topical intent (briefing, policy PDF, regulation, plain fallbacks) map to
# general. oss_discovery (alternatives / open source) is discovery, not documentation.
BASELINE_MAP: Dict[str, str] = {
    "security_advisory": "security",
    "academic_arxiv": "academic",
    "patents": "academic",
    "github_docs": "docs",
    "official_docs": "docs",
    "docs_api": "docs",
    "shopping_reviews_local": "shopping",
    "shopping_specs": "shopping",
    "reddit_community": "community",
    "community_forum": "community",
    "local_at": "local",
    "weather_local": "local",
    "official_vendor_release": "news",
    "finance_earnings_official": "news",
    "finance_investor_monthly": "news",
    "sports_current": "news",
    "briefing_synthesis": "general",
    "policy_pdf": "general",
    "official_regulatory": "general",
    "oss_discovery": "general",
    "multilingual_current": "general",
    "general": "general",
}

# What the 5.0 router would do with an intent: which provider goes first.
FIRST_PROVIDER = {"academic": "exa", "docs": "exa", "shopping": "serper"}


def first_provider(intent: str) -> str:
    return FIRST_PROVIDER.get(intent, "brave")


Row = Dict[str, object]


def load(path: Path) -> List[Row]:
    rows: List[Row] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def detector_predict(rows: Sequence[Row]) -> List[Tuple[str, float, Tuple[str, ...]]]:
    out = []
    for row in rows:
        decision = intents.classify_intent(str(row["query"]))
        out.append((decision.intent, decision.confidence, decision.signals))
    return out


def baseline_predict(rows: Sequence[Row], baseline_dir: Path) -> List[Tuple[str, float, Tuple[str, ...]]]:
    """routing_class of the pre-5.0 router, loaded from a v4.3.5 checkout (flat layout)."""
    sys.path.insert(0, str(baseline_dir))
    import config as legacy_config  # noqa: E402
    from routing import QueryAnalyzer  # noqa: E402

    analyzer = QueryAnalyzer(legacy_config._deepcopy_default_config())
    out = []
    for row in rows:
        routing_class = analyzer.analyze(str(row["query"]))["routing_class"]
        if routing_class not in BASELINE_MAP:
            raise KeyError(f"unmapped routing_class {routing_class!r}; extend BASELINE_MAP")
        out.append((BASELINE_MAP[routing_class], 1.0, (routing_class,)))
    return out


def confusion(gold: Sequence[str], pred: Sequence[str]) -> Dict[Tuple[str, str], int]:
    counts: Counter = Counter()
    for g, p in zip(gold, pred):
        counts[(g, p)] += 1
    return counts


def prf(counts: Dict[Tuple[str, str], int], label: str) -> Tuple[float, float, float, int, int, int]:
    tp = counts.get((label, label), 0)
    predicted = sum(n for (g, p), n in counts.items() if p == label)
    actual = sum(n for (g, p), n in counts.items() if g == label)
    precision = tp / predicted if predicted else float("nan")
    recall = tp / actual if actual else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if tp else (0.0 if predicted or actual else float("nan"))
    return precision, recall, f1, tp, predicted, actual


def pct(value: float) -> str:
    return "  n/a" if value != value else f"{100 * value:5.1f}"


def print_confusion(counts: Dict[Tuple[str, str], int]) -> None:
    width = 9
    print("gold \\ pred".ljust(12) + "".join(name[:width].rjust(width + 1) for name in INTENTS) + "  total")
    for g in INTENTS:
        cells = [counts.get((g, p), 0) for p in INTENTS]
        print(g.ljust(12) + "".join(str(c).rjust(width + 1) for c in cells) + str(sum(cells)).rjust(7))
    totals = [sum(counts.get((g, p), 0) for g in INTENTS) for p in INTENTS]
    print("predicted".ljust(12) + "".join(str(t).rjust(width + 1) for t in totals) + str(sum(totals)).rjust(7))


def print_prf(counts: Dict[Tuple[str, str], int]) -> None:
    print("intent        precision  recall     F1    TP  pred  gold")
    for label in INTENTS:
        precision, recall, f1, tp, predicted, actual = prf(counts, label)
        marker = "  <- precise label" if label in PRECISE else ""
        print(f"{label:<12}  {pct(precision)}    {pct(recall)}   {pct(f1)}  {tp:>4}  {predicted:>4}  {actual:>4}{marker}")


def print_language(rows: Sequence[Row], gold: Sequence[str], pred: Sequence[str]) -> None:
    by_lang: Dict[str, List[bool]] = defaultdict(list)
    for row, g, p in zip(rows, gold, pred):
        by_lang[str(row.get("lang", "?"))].append(g == p)
    print("lang  n    accuracy")
    for lang, hits in sorted(by_lang.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"{lang:<5} {len(hits):>3}  {pct(sum(hits) / len(hits))}")
    non_en = [h for lang, hs in by_lang.items() if lang != "en" for h in hs]
    if non_en:
        print(f"non-en {len(non_en):>3}  {pct(sum(non_en) / len(non_en))}")


def print_routing(gold: Sequence[str], pred: Sequence[str]) -> None:
    """What the first-provider choice would be, against the first provider for the gold intent."""
    groups = ("exa", "serper", "brave")
    counts = Counter((first_provider(g), first_provider(p)) for g, p in zip(gold, pred))
    print("first provider  gold -> predicted (rows gold)")
    print("gold \\ pred".ljust(12) + "".join(g.rjust(8) for g in groups))
    for g in groups:
        print(g.ljust(12) + "".join(str(counts.get((g, p), 0)).rjust(8) for p in groups))
    total = len(gold)
    same = sum(counts.get((g, g), 0) for g in groups)
    wrong_away = sum(n for (g, p), n in counts.items() if p != "brave" and p != g)
    missed = sum(n for (g, p), n in counts.items() if g != "brave" and p == "brave")
    print(f"first provider right: {same}/{total} ({pct(same / total).strip()}%)")
    print(f"  sent away from Brave wrongly (costly): {wrong_away}   kept on Brave but should have left (cheap): {missed}")


def print_confidence(gold: Sequence[str], pred: Sequence[str], conf: Sequence[float]) -> None:
    bins = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.01)]
    print("confidence    n   accuracy")
    for low, high in bins:
        hits = [g == p for g, p, c in zip(gold, pred, conf) if low <= c < high]
        if hits:
            print(f"[{low:.2f},{min(high, 1.0):.2f})  {len(hits):>3}  {pct(sum(hits) / len(hits))}")


def report(title: str, rows: Sequence[Row], preds: List[Tuple[str, float, Tuple[str, ...]]],
           show_errors: bool, detail: bool) -> None:
    gold = [str(r["intent"]) for r in rows]
    pred = [p[0] for p in preds]
    conf = [p[1] for p in preds]
    counts = confusion(gold, pred)
    correct = sum(g == p for g, p in zip(gold, pred))
    print("=" * 78)
    print(f"{title}: accuracy {correct}/{len(rows)} = {pct(correct / len(rows)).strip()}%")
    print("=" * 78)
    print("\nConfusion matrix")
    print_confusion(counts)
    print("\nPer intent")
    print_prf(counts)
    print("\nacademic / docs / shopping (a wrong label here sends the query away from Brave)")
    for label in PRECISE:
        precision, recall, _, tp, predicted, actual = prf(counts, label)
        print(f"  {label:<9} precision {pct(precision).strip()}% ({tp}/{predicted})   recall {pct(recall).strip()}% ({tp}/{actual})")
    print("\nPer language")
    print_language(rows, gold, pred)
    print("\nRouting view")
    print_routing(gold, pred)
    if detail and any(c != 1.0 for c in conf):
        print("\nConfidence calibration (detector heuristic)")
        print_confidence(gold, pred, conf)
    wrong_precise = [(r, g, p, s) for r, g, (p, _, s) in zip(rows, gold, preds) if p in PRECISE and p != g]
    print(f"\nQueries wrongly labelled academic/docs/shopping: {len(wrong_precise)}")
    for row, g, p, sig in wrong_precise:
        print(f"  [{row.get('lang', '?')}] gold={g:<9} pred={p:<9} {row['query']}")
        if detail:
            print(f"        signals: {', '.join(sig)}")
    if show_errors:
        others = [(r, g, p, s) for r, g, (p, _, s) in zip(rows, gold, preds)
                  if p != g and p not in PRECISE]
        print(f"\nOther wrong labels: {len(others)}")
        for row, g, p, sig in others:
            print(f"  [{row.get('lang', '?')}] gold={g:<9} pred={p:<9} {row['query']}")
            if detail:
                print(f"        signals: {', '.join(sig)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=sorted(SETS), default="tuning", help="query set (default: tuning)")
    parser.add_argument("--file", type=Path, help="evaluate this JSONL file instead (fields: intent, lang, query)")
    parser.add_argument("--errors", action="store_true", help="also list wrong labels that are not academic/docs/shopping")
    parser.add_argument("--detail", action="store_true", help="show detector signals and a confidence summary")
    parser.add_argument("--baseline-dir", type=Path,
                        help="v4.3.5 checkout: also evaluate its routing_class mapped to intents")
    args = parser.parse_args(argv)

    path = args.file or SETS[args.set]
    if args.file is None and args.set == "heldout":
        print("*** HELD-OUT evaluation (benchmarks/queries.jsonl): do not tune against these numbers ***\n")
    rows = load(path)
    unknown = {str(r["intent"]) for r in rows} - set(INTENTS)
    if unknown:
        print(f"unknown gold intents in {path}: {sorted(unknown)}", file=sys.stderr)
        return 2
    print(f"set: {path.relative_to(REPO) if path.is_relative_to(REPO) else path}  ({len(rows)} queries)\n")
    report("DETECTOR (wsp_core.intents)", rows, detector_predict(rows), args.errors, args.detail)
    if args.baseline_dir:
        print("\nBaseline mapping (routing_class -> intent):")
        for routing_class, intent in sorted(BASELINE_MAP.items(), key=lambda kv: (kv[1], kv[0])):
            print(f"  {routing_class:<28} -> {intent}")
        print()
        report("BASELINE (routing_class mapped)", rows, baseline_predict(rows, args.baseline_dir), args.errors, args.detail)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
