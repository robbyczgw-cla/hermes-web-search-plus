#!/usr/bin/env python3
"""Measure WSP's own per-call cost with zero-latency synthetic providers.

Provider latency dominates real searches, but everything WSP does around a
provider call (argument parsing, routing, cache, JSON, formatting) is paid on
every call too. This script isolates that local overhead so refactors can be
compared directly:

    python scripts/wsp_eval/overhead.py --plugin-dir /tmp/v435 --plugin-dir .
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

WORKER = Path(__file__).resolve().parent / "worker.py"
REPO = Path(__file__).resolve().parents[2]


def _percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def measure(plugin_dir: Path, cases: List[Dict], repeat: int, no_cache: bool) -> Dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="wsp-overhead-") as tmp:
        case_file = Path(tmp) / "cases.jsonl"
        with case_file.open("w", encoding="utf-8") as fh:
            for case in cases:
                args = dict(case.get("args") or {})
                if no_cache:
                    args["no_cache"] = True
                fh.write(json.dumps({**case, "args": args}) + "\n")
        out = Path(tmp) / "out.jsonl"
        subprocess.run([sys.executable, "-I", str(WORKER), "--plugin-dir", str(plugin_dir), "--cases",
                        str(case_file), "--out", str(out), "--mode", "tool", "--repeat", str(repeat)],
                       check=True, capture_output=True, text=True)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    meta = rows[0]["meta"]
    walls: List[float] = []
    for row in rows[1:]:
        walls.extend(row["wall_ms"][1:] if len(row["wall_ms"]) > 1 else row["wall_ms"])
    return {"import_ms": meta["import_ms"], "calls": len(walls), "p50_ms": _percentile(walls, 50),
            "p95_ms": _percentile(walls, 95), "mean_ms": statistics.fmean(walls) if walls else 0.0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, action="append", required=True)
    parser.add_argument("--queries", type=Path, default=REPO / "benchmarks" / "queries.jsonl")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--repeat", type=int, default=6, help="calls per case; the first one is warm-up")
    args = parser.parse_args(argv)
    queries = [json.loads(line) for line in args.queries.read_text(encoding="utf-8").splitlines()][: args.limit]
    normal = [{"id": q["id"], "query": q["query"]} for q in queries]
    research = [{"id": q["id"], "query": q["query"], "args": {"mode": "research"}} for q in queries[:10]]
    print("| checkout | import ms | normal p50 ms | normal p95 ms | research p50 ms | research p95 ms |")
    print("|---|---:|---:|---:|---:|---:|")
    for plugin_dir in args.plugin_dir:
        n = measure(plugin_dir.resolve(), normal, args.repeat, no_cache=True)
        r = measure(plugin_dir.resolve(), research, max(2, args.repeat // 2), no_cache=True)
        print(f"| {plugin_dir} | {n['import_ms']:.0f} | {n['p50_ms']:.2f} | {n['p95_ms']:.2f} | "
              f"{r['p50_ms']:.2f} | {r['p95_ms']:.2f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
