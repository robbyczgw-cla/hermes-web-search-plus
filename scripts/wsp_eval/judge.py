#!/usr/bin/env python3
"""Pooled relevance judgments and nDCG@5 scoring for recorded WSP runs.

Pool: every URL any recorded provider returned for a query (the normalized
``payload.results`` of a ``worker.py`` run), keyed by a canonical URL. Each
query's pool is judged once (grades 0-3); every system variant is then scored
offline against the same judgments.

    # judge (needs a judge command that reads a prompt on stdin, prints JSON)
    python scripts/wsp_eval/judge.py judge --runs eval/rec/search_run.jsonl \\
        --out eval/judge/qrels.jsonl --judge-cmd "grok -m grok-4.7 ..."
    # score one or more worker runs
    python scripts/wsp_eval/judge.py score --qrels eval/judge/qrels.jsonl \\
        --run baseline=eval/replay/v435.jsonl --run candidate=eval/replay/new.jsonl

The judge sees title, URL and snippet only (no page fetch), like a searcher
scanning a result page; grades are therefore snippet-level relevance.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPO = Path(__file__).resolve().parents[2]
QUERIES = REPO / "benchmarks" / "queries.jsonl"
GRADE_GAIN = {0: 0.0, 1: 1.0, 2: 3.0, 3: 7.0}  # 2^g - 1
TRACKING = re.compile(r"^(utm_.*|gclid|fbclid|srsltid|mc_cid|mc_eid|ref|ref_src)$", re.IGNORECASE)

JUDGE_PREAMBLE = (
    "Arbeite die Aufgabe vollständig ab und antworte in EINER einzigen Nachricht mit dem fertigen "
    "Ergebnis. Kündige nichts an, beschreibe dein Vorgehen nicht, beende deinen Turn nicht vor dem Ergebnis.\n\n"
)
JUDGE_TEMPLATE = """You grade web search results for relevance. Today is {today}.

Query: {query}
Query intent: {intent}{recency}

For each result, judge how useful it is for answering this query, using only its title, URL and snippet:
3 = directly answers the query from an authoritative or primary source (official docs, the paper itself, the vendor, the advisory, a reputable outlet for news)
2 = relevant and useful, but secondary, partial or less authoritative
1 = marginally related (topic overlaps, does not help answer)
0 = not relevant, spam, SEO filler, wrong language/region for the query, or outdated when the query needs recent information

Results:
{results}

Answer with exactly one JSON object mapping each result id to its grade, e.g. {{"r1": 3, "r2": 0}}. No other text.
"""


def canonical_url(url: str) -> str:
    """Stable judging key: scheme/www./m./AMP/tracking/fragment/trailing-slash insensitive."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = (parts.hostname or "").lower().rstrip(".")
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = re.sub(r"/(amp|amp\.html)/?$", "/", parts.path or "/")
    path = path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)))
    return urlunsplit(("", host, path, query, ""))


def load_queries() -> Dict[str, Dict[str, Any]]:
    return {row["id"]: row for row in map(json.loads, QUERIES.read_text(encoding="utf-8").splitlines())}


def iter_run(path: Path) -> Iterable[Dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if "meta" not in row:
            yield row


def run_results(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = row.get("payload") or {}
    return [item for item in payload.get("results") or [] if isinstance(item, dict) and item.get("url")]


def build_pool(runs: List[Path]) -> Dict[str, Dict[str, Dict[str, str]]]:
    """qid -> canonical url -> {url, title, snippet} (first sighting wins)."""
    pool: Dict[str, Dict[str, Dict[str, str]]] = defaultdict(dict)
    for path in runs:
        for row in iter_run(path):
            for item in run_results(row):
                key = canonical_url(item["url"])
                pool[row["id"]].setdefault(key, {
                    "url": str(item.get("url") or ""),
                    "title": str(item.get("title") or "")[:200],
                    "snippet": re.sub(r"\s+", " ", str(item.get("snippet") or item.get("content") or ""))[:400],
                })
    return pool


def _judge_prompt(query: Dict[str, Any], items: List[Tuple[str, Dict[str, str]]], today: str) -> str:
    recency = f"\nThe query needs recent information (within the last {query['recency']})." if query.get("recency") else ""
    lines = [f'{rid}: title="{doc["title"]}" url={doc["url"]} snippet="{doc["snippet"]}"' for rid, doc in items]
    return JUDGE_PREAMBLE + JUDGE_TEMPLATE.format(
        today=today, query=query["query"], intent=query["intent"], recency=recency, results="\n".join(lines)
    )


def _parse_grades(text: str, ids: List[str]) -> Optional[Dict[str, int]]:
    for match in reversed(list(re.finditer(r"\{[^{}]*\}", text, re.S))):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        grades = {rid: int(data[rid]) for rid in ids if rid in data and str(data[rid]).strip().isdigit()}
        if len(grades) == len(ids) and all(0 <= g <= 3 for g in grades.values()):
            return grades
    return None


def judge_query(qid: str, query: Dict[str, Any], docs: Dict[str, Dict[str, str]], judge_cmd: str,
                today: str, timeout: int) -> List[Dict[str, Any]]:
    keys = sorted(docs)
    items = [(f"r{i + 1}", docs[key]) for i, key in enumerate(keys)]
    prompt = _judge_prompt(query, items, today)
    ids = [rid for rid, _ in items]
    for attempt in range(2):
        proc = subprocess.run(shlex.split(judge_cmd) + [prompt], capture_output=True, text=True, timeout=timeout)
        grades = _parse_grades(proc.stdout, ids)
        if grades is not None:
            return [{"qid": qid, "url_key": key, "url": docs[key]["url"], "grade": grades[rid]}
                    for (rid, _), key in zip(items, keys)]
    raise RuntimeError(f"judge returned no parseable grades for {qid}: {proc.stdout[-300:]!r} {proc.stderr[-300:]!r}")


def cmd_judge(args: argparse.Namespace) -> int:
    queries = load_queries()
    pool = build_pool(args.runs)
    done = set()
    if args.out.exists():
        done = {row["qid"] for row in map(json.loads, args.out.read_text(encoding="utf-8").splitlines())}
    todo = [qid for qid in sorted(pool) if qid not in done and qid in queries]
    print(f"pool: {len(pool)} queries, {sum(len(v) for v in pool.values())} urls; judging {len(todo)}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor, \
            args.out.open("a", encoding="utf-8") as out:
        futures = {executor.submit(judge_query, qid, queries[qid], pool[qid], args.judge_cmd, args.today,
                                   args.timeout): qid for qid in todo}
        for future in concurrent.futures.as_completed(futures):
            qid = futures[future]
            try:
                rows = future.result()
            except Exception as exc:  # report and keep going; rerun fills the gaps
                failures += 1
                print(f"FAIL {qid}: {exc}", file=sys.stderr, flush=True)
                continue
            for row in rows:
                out.write(json.dumps({**row, "judge": args.judge_label}, ensure_ascii=False) + "\n")
            out.flush()
            print(f"ok {qid} ({len(rows)} urls)", flush=True)
    return 1 if failures else 0


def load_qrels(path: Path) -> Dict[str, Dict[str, int]]:
    qrels: Dict[str, Dict[str, int]] = defaultdict(dict)
    for row in map(json.loads, path.read_text(encoding="utf-8").splitlines()):
        qrels[row["qid"]][row["url_key"]] = int(row["grade"])
    return qrels


def ndcg_at(ranked: List[str], judged: Dict[str, int], k: int = 5) -> float:
    dcg = sum(GRADE_GAIN[judged.get(key, 0)] / math.log2(rank + 2) for rank, key in enumerate(ranked[:k]))
    ideal = sorted(judged.values(), reverse=True)[:k]
    idcg = sum(GRADE_GAIN[g] / math.log2(rank + 2) for rank, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def score_run(path: Path, qrels: Dict[str, Dict[str, int]], queries: Dict[str, Dict[str, Any]],
              k: int = 5) -> Dict[str, Any]:
    per_query = {}
    for row in iter_run(path):
        qid = row["id"]
        if qid not in qrels:
            continue
        urls = [item["url"] for item in run_results(row)]
        ranked = []
        for url in urls:  # duplicates of an already-ranked page add nothing
            key = canonical_url(url)
            if key not in ranked:
                ranked.append(key)
        judged = qrels[qid]
        unjudged = sum(1 for key in ranked[:k] if key not in judged)
        authority = queries.get(qid, {}).get("authority_domains") or []
        hit = None
        if authority:
            hit = any(any(_domain(u) == d or _domain(u).endswith("." + d) for d in authority) for u in urls[:k])
        per_query[qid] = {"ndcg": ndcg_at(ranked, judged, k), "unjudged_topk": unjudged, "authority_hit": hit,
                          "results": len(urls), "provider": (row.get("payload") or {}).get("provider")}
    values = [v["ndcg"] for v in per_query.values()]
    hits = [v["authority_hit"] for v in per_query.values() if v["authority_hit"] is not None]
    return {
        "queries": len(per_query),
        "ndcg@5": sum(values) / len(values) if values else 0.0,
        "authority_hit@5": (sum(hits) / len(hits)) if hits else None,
        "empty": sum(1 for v in per_query.values() if v["results"] == 0),
        "unjudged_in_top5": sum(v["unjudged_topk"] for v in per_query.values()),
        "per_query": per_query,
    }


def cmd_score(args: argparse.Namespace) -> int:
    queries = load_queries()
    qrels = load_qrels(args.qrels)
    print("| run | queries | nDCG@5 | authority hit@5 | empty answers | unjudged in top 5 |")
    print("|---|---:|---:|---:|---:|---:|")
    report = {}
    for spec in args.run:
        label, _, path = spec.partition("=")
        result = score_run(Path(path), qrels, queries)
        report[label] = result
        hit = "-" if result["authority_hit@5"] is None else f"{result['authority_hit@5']:.3f}"
        print(f"| {label} | {result['queries']} | {result['ndcg@5']:.3f} | {hit} | {result['empty']} | "
              f"{result['unjudged_in_top5']} |")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    judge = sub.add_parser("judge")
    judge.add_argument("--runs", type=Path, nargs="+", required=True)
    judge.add_argument("--out", type=Path, required=True)
    judge.add_argument("--judge-cmd", required=True, help="command; the prompt is appended as last argument")
    judge.add_argument("--judge-label", default="judge")
    judge.add_argument("--today", default="2026-10-08")
    judge.add_argument("--workers", type=int, default=4)
    judge.add_argument("--timeout", type=int, default=300)
    judge.set_defaults(func=cmd_judge)
    score = sub.add_parser("score")
    score.add_argument("--qrels", type=Path, required=True)
    score.add_argument("--run", action="append", required=True, help="label=path/to/worker_run.jsonl")
    score.add_argument("--json", type=Path)
    score.set_defaults(func=cmd_score)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
