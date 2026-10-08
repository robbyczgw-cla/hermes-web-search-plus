#!/usr/bin/env python3
"""Behaviour lock: freeze routing decisions and tool output, then compare.

The expected files under tests/fixtures/lock/ were generated from the v4.3.5
tag. A refactor that claims to be behaviour-neutral must reproduce them
byte-for-byte. A pull request that changes behaviour on purpose regenerates
them with ``--update`` so the fixture diff shows exactly what changed.

    python scripts/behaviour_lock/lock.py                      # compare current tree
    python scripts/behaviour_lock/lock.py --update             # rewrite expectations
    python scripts/behaviour_lock/lock.py --plugin-dir /tmp/v435 --update
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).resolve().parent / "worker.py"
LOCK_DIR = REPO / "tests" / "fixtures" / "lock"
ROUTE_SOURCES = [LOCK_DIR / "queries.jsonl", LOCK_DIR / "tuning_queries.jsonl"]
TOOL_CASES = LOCK_DIR / "tool_cases.jsonl"
ROUTE_EXPECTED = LOCK_DIR / "routing.expected.jsonl"
TOOL_EXPECTED = LOCK_DIR / "tool.expected.jsonl"
# Research members race on daemon threads; quorum early return would make the
# merged page depend on thread timing. The lock pins the deterministic path.
LOCK_CONFIG = {"quality": {"research_quorum": {"enabled": False}}}


def _run_worker(plugin_dir: Path, mode: str, cases: Path, out: Path) -> None:
    command = [
        sys.executable, "-I", str(WORKER),
        "--plugin-dir", str(plugin_dir),
        "--cases", str(cases),
        "--out", str(out),
        "--mode", mode,
        "--transport", "synthetic",
        "--config", json.dumps(LOCK_CONFIG),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def _lock_rows(path: Path, mode: str) -> List[str]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if "meta" in row:
            continue
        if mode == "route":
            kept = {"id": row["id"], "decision": row["decision"]}
        else:
            # A repeated query is served from cache; only its age is volatile.
            output = re.sub(r"cached \d+s ago", "cached Ns ago", row["output"])
            kept = {"id": row["id"], "output": output, "payload": row["payload"], "raised": row["raised"],
                    # Research members run concurrently: compare the calls as a set.
                    "http": sorted([h["provider"], h["method"], h["path"], h["status"]] for h in row["http"])}
        rows.append(json.dumps(kept, ensure_ascii=False, sort_keys=True))
    return rows


def generate(plugin_dir: Path) -> Tuple[List[str], List[str]]:
    with tempfile.TemporaryDirectory(prefix="wsp-lock-") as tmp:
        tmp_path = Path(tmp)
        route_cases = tmp_path / "route_cases.jsonl"
        route_cases.write_text(
            "".join(source.read_text(encoding="utf-8") for source in ROUTE_SOURCES), encoding="utf-8"
        )
        _run_worker(plugin_dir, "route", route_cases, tmp_path / "route.jsonl")
        _run_worker(plugin_dir, "tool", TOOL_CASES, tmp_path / "tool.jsonl")
        return _lock_rows(tmp_path / "route.jsonl", "route"), _lock_rows(tmp_path / "tool.jsonl", "tool")


def _pretty(row: str) -> List[str]:
    return json.dumps(json.loads(row), ensure_ascii=False, indent=1, sort_keys=True).splitlines()


def compare(expected: List[str], actual: List[str], label: str, limit: int = 3) -> List[str]:
    """Return human-readable differences (empty when identical)."""
    problems: List[str] = []
    expected_by_id: Dict[str, str] = {json.loads(r)["id"]: r for r in expected}
    actual_by_id: Dict[str, str] = {json.loads(r)["id"]: r for r in actual}
    missing = sorted(set(expected_by_id) - set(actual_by_id))
    extra = sorted(set(actual_by_id) - set(expected_by_id))
    if missing:
        problems.append(f"{label}: missing cases {missing[:10]}")
    if extra:
        problems.append(f"{label}: unexpected cases {extra[:10]}")
    changed = [case_id for case_id in expected_by_id if case_id in actual_by_id
               and expected_by_id[case_id] != actual_by_id[case_id]]
    if changed:
        problems.append(f"{label}: {len(changed)} case(s) changed: {changed[:20]}")
        for case_id in changed[:limit]:
            diff = difflib.unified_diff(_pretty(expected_by_id[case_id]), _pretty(actual_by_id[case_id]),
                                        f"expected/{case_id}", f"actual/{case_id}", lineterm="", n=2)
            problems.append("\n".join(list(diff)[:60]))
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=REPO)
    parser.add_argument("--update", action="store_true", help="rewrite the expected lock files")
    args = parser.parse_args(argv)
    route_rows, tool_rows = generate(args.plugin_dir.resolve())
    if args.update:
        ROUTE_EXPECTED.write_text("\n".join(route_rows) + "\n", encoding="utf-8")
        TOOL_EXPECTED.write_text("\n".join(tool_rows) + "\n", encoding="utf-8")
        print(f"wrote {len(route_rows)} routing and {len(tool_rows)} tool expectations")
        return 0
    problems = compare(ROUTE_EXPECTED.read_text(encoding="utf-8").splitlines(), route_rows, "routing")
    problems += compare(TOOL_EXPECTED.read_text(encoding="utf-8").splitlines(), tool_rows, "tool")
    print("\n".join(problems) if problems else "behaviour lock: identical")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
