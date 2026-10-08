#!/usr/bin/env python3
"""Latency and fallback under provider faults, replayed for several checkouts.

    python scripts/wsp_eval/scenarios.py --corpus eval/rec/full_corpus.jsonl \\
        --cases eval/rec/replay_normal.jsonl --qrels eval/judge/qrels.jsonl \\
        --plugin-dir v435=/tmp/v435 --plugin-dir current=.

Every checkout replays the same recording in the "core-4" configuration. A
fault scenario makes one provider hang (until the client timeout), answer
empty, or answer HTTP 503 on every call. Replays run time-scaled; reported
latencies are divided by the scale, and the hedge floor is scaled with it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge  # noqa: E402

WORKER = Path(__file__).resolve().parent / "worker.py"
SCENARIOS = {
    "no fault": {},
    "serper hangs": {"serper": "timeout"},
    "serper empty": {"serper": "empty"},
    "serper 503": {"serper": "http503"},
    "brave hangs": {"brave": "timeout"},
}


def _pct(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))] if ordered else 0.0


def run(plugin_dir: Path, cases: Path, corpus: Path, faults: Dict[str, str], scale: float, keys: str,
        out: Path) -> None:
    config = {"v3": {"hedge_min_delay_seconds": 2.5 * scale}}
    subprocess.run([sys.executable, "-I", str(WORKER), "--plugin-dir", str(plugin_dir), "--cases", str(cases),
                    "--out", str(out), "--mode", "tool", "--transport", "replay", "--corpus", str(corpus),
                    "--latency-scale", str(scale), "--keys", keys, "--faults", json.dumps(faults),
                    "--config", json.dumps(config)], check=True, capture_output=True, text=True)


def summarize(path: Path, scale: float, qrels, queries) -> Dict[str, float]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()][1:]
    walls = [row["wall_ms"][0] / scale for row in rows]
    empty = sum(1 for row in rows if not (row.get("payload") or {}).get("results"))
    fallback = sum(1 for row in rows if ((row.get("payload") or {}).get("routing") or {}).get("fallback_used"))
    calls = statistics.fmean(len(row.get("http") or []) for row in rows)
    scored = judge.score_run(path, qrels, queries)
    return {"p50": _pct(walls, 50), "p95": _pct(walls, 95), "max": max(walls), "empty": empty,
            "fallback": fallback, "calls": calls, "ndcg": scored["ndcg@5"], "n": len(rows)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", action="append", required=True, help="label=path")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--qrels", type=Path, required=True)
    parser.add_argument("--keys", default="serper,brave,exa,tavily")
    parser.add_argument("--scale", type=float, default=0.05)
    parser.add_argument("--scenario", action="append", choices=list(SCENARIOS))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    qrels = judge.load_qrels(args.qrels)
    queries = judge.load_queries()
    report = {}
    print("| scenario | checkout | p50 | p95 | max | empty answers | fallback used | provider calls/query | nDCG@5 |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    with tempfile.TemporaryDirectory(prefix="wsp-scenarios-") as tmp:
        for name in args.scenario or list(SCENARIOS):
            for spec in args.plugin_dir:
                label, _, path = spec.partition("=")
                out = Path(tmp) / f"{label}-{len(report)}.jsonl"
                run(Path(path).resolve(), args.cases, args.corpus, SCENARIOS[name], args.scale, args.keys, out)
                summary = summarize(out, args.scale, qrels, queries)
                report.setdefault(name, {})[label] = summary
                print(f"| {name} | {label} | {summary['p50']:.0f} ms | {summary['p95']:.0f} ms | "
                      f"{summary['max']:.0f} ms | {summary['empty']} | {summary['fallback']} | "
                      f"{summary['calls']:.2f} | {summary['ndcg']:.3f} |", flush=True)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
