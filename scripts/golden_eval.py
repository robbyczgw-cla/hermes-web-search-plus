#!/usr/bin/env python3
"""Golden query evaluator for web-search-plus local spikes.

Runs a fixed set of representative queries against normal search and optional
research mode, then writes machine-readable JSONL plus a compact markdown report.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse


DEFAULT_GOLDEN_QUERIES: List[Dict[str, Any]] = [
    {
        "id": "hifi-turntables-under-1000",
        "category": "hifi_product",
        "query": "best turntables under 1000 euro 2026",
        "notes": "HiFi/product query; should avoid SEO sludge where possible.",
    },
    {
        "id": "graz-weather-today",
        "category": "local_realtime",
        "query": "Graz weather today",
        "notes": "Local/current factual query.",
    },
    {
        "id": "tailscale-release-notes",
        "category": "tech_release",
        "query": "latest Tailscale release notes subnet router",
        "notes": "Current software/release query.",
    },
    {
        "id": "eu-ai-act-small-business",
        "category": "research_policy",
        "query": "EU AI Act compliance requirements for small businesses 2026",
        "notes": "Policy/research query where source grounding matters.",
    },
    {
        "id": "deutsche-ki-news",
        "category": "german_realtime",
        "query": "aktuelle KI Regulierung Österreich Unternehmen 2026",
        "notes": "German-language current query.",
    },
    {
        "id": "reddit-local-llama",
        "category": "reddit_community",
        "query": "site:reddit.com/r/LocalLLaMA best local LLM inference server 2026",
        "notes": "Community/reddit query; browser often blocked.",
    },
    {
        "id": "academic-rag-eval",
        "category": "academic",
        "query": "recent papers retrieval augmented generation evaluation benchmark 2026",
        "notes": "Academic discovery query.",
    },
    {
        "id": "js-heavy-extraction",
        "category": "js_extraction",
        "query": "JavaScript heavy pricing page AI search API extraction example",
        "notes": "Extraction-sensitive / JS-ish query.",
    },
]


def load_golden_queries(path: Path | None = None) -> List[Dict[str, Any]]:
    if path is None:
        return [case.copy() for case in DEFAULT_GOLDEN_QUERIES]
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Golden query file must contain a JSON list")
    return data


def _safe_domain(url: str) -> str:
    try:
        netloc = urlparse(url or "").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _normalized_result_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{netloc}{parsed.path.rstrip('/')}"
    except Exception:
        return ""


def _duplicate_url_count(results: List[Dict[str, Any]]) -> int:
    seen = set()
    duplicates = 0
    for item in results:
        normalized = _normalized_result_url(item.get("url", ""))
        if not normalized:
            continue
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
    return duplicates


def _json_from_stdout(stdout: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(stdout or "{}")
        return parsed if isinstance(parsed, dict) else {"error": "stdout was not a JSON object", "results": []}
    except json.JSONDecodeError as e:
        return {"error": f"invalid JSON stdout: {e}", "results": []}


def _result_text(item: Dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in ("title", "snippet", "description", "content", "raw_content", "summary")
    )


def evaluate_snapshot_quality(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate an offline replay payload against deterministic quality expectations.

    This is intentionally not a live benchmark: fixtures pin representative
    provider outputs and assert source-quality properties that schema contract
    tests cannot see, such as primary-domain presence and extraction substance.
    """
    payload = snapshot.get("payload") or {}
    expect = snapshot.get("expect") or {}
    results = payload.get("results") or []
    domains = [_safe_domain(item.get("url", "")) for item in results if item.get("url")]
    domain_set = set(d for d in domains if d)
    top_domain = domains[0] if domains else ""
    combined_text = "\n".join(_result_text(item) for item in results)
    source_summaries = payload.get("source_summaries") or []
    content_items = list(results) + list(source_summaries)
    content_chars = sum(len(str(item.get("content") or item.get("raw_content") or "")) for item in content_items)
    reported_duplicate_count = int((payload.get("quality_report") or {}).get("duplicate_count", payload.get("metadata", {}).get("dedup_count", 0)) or 0)
    duplicate_count = max(reported_duplicate_count, _duplicate_url_count(results))

    failure_flags: List[str] = []
    min_results = expect.get("min_results")
    if min_results is not None and len(results) < int(min_results):
        failure_flags.append("too_few_results")
    min_domain_count = expect.get("min_domain_count")
    if min_domain_count is not None and len(domain_set) < int(min_domain_count):
        failure_flags.append("low_domain_count")
    min_content_chars = expect.get("min_content_chars")
    if min_content_chars is not None and content_chars < int(min_content_chars):
        failure_flags.append("thin_extracted_content")

    required_domains = set(expect.get("must_include_domains") or [])
    missing_domains = sorted(required_domains - domain_set)
    if missing_domains:
        failure_flags.append("missing_required_domain")

    top_domain_any_of = set(expect.get("top_domain_any_of") or [])
    if top_domain_any_of and top_domain not in top_domain_any_of:
        failure_flags.append("top_domain_not_canonical")

    blocked_domains = set(expect.get("blocked_domains") or [])
    blocked_hits = sorted(blocked_domains & domain_set)
    if blocked_hits and expect.get("allow_blocked_domains") is not True:
        failure_flags.append("blocked_domain_present")

    missing_terms = [term for term in expect.get("required_terms") or [] if term.lower() not in combined_text.lower()]
    if missing_terms:
        failure_flags.append("missing_required_terms")

    max_duplicate_count = expect.get("max_duplicate_count")
    if max_duplicate_count is not None and duplicate_count > int(max_duplicate_count):
        failure_flags.append("too_many_duplicates")

    return {
        "id": snapshot["id"],
        "category": snapshot.get("category"),
        "query": snapshot.get("query"),
        "status": "ok" if not failure_flags else "fail",
        "failure_flags": failure_flags,
        "result_count": len(results),
        "domain_count": len(domain_set),
        "domains": sorted(domain_set),
        "top_domain": top_domain,
        "missing_domains": missing_domains,
        "blocked_domain_hits": blocked_hits,
        "missing_terms": missing_terms,
        "content_chars": content_chars,
        "duplicate_count": duplicate_count,
    }


def load_snapshot_fixtures(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Golden snapshot fixture must contain a JSON list")
    return data


def run_snapshot_quality(snapshots: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [evaluate_snapshot_quality(snapshot) for snapshot in snapshots]


def summarize_result(
    case: Dict[str, Any],
    mode: str,
    payload: Dict[str, Any],
    latency_ms: int,
    returncode: int,
    stderr: str,
) -> Dict[str, Any]:
    results = payload.get("results") or []
    source_summaries = payload.get("source_summaries") or []
    quality = payload.get("quality_report") or {}
    metadata = payload.get("metadata") or {}
    routing = payload.get("routing") or {}
    domains = quality.get("domains") or sorted({_safe_domain(r.get("url", "")) for r in results if r.get("url")})
    domains = [d for d in domains if d]
    extracted_chars = sum(len((s.get("content") or s.get("raw_content") or "")) for s in source_summaries)

    failure_flags: List[str] = []
    if not results:
        failure_flags.append("no_results")
    if payload.get("error"):
        failure_flags.append("provider_error")
    if returncode != 0:
        failure_flags.append("nonzero_exit")
    if mode == "research" and not source_summaries:
        failure_flags.append("no_source_summaries")
    if mode == "research" and source_summaries and extracted_chars == 0:
        failure_flags.append("empty_source_summaries")
    if latency_ms > (30000 if mode == "research" else 12000):
        failure_flags.append("slow")

    return {
        "id": case["id"],
        "category": case["category"],
        "query": case["query"],
        "mode": mode,
        "status": "error" if (returncode != 0 or payload.get("error")) else "ok",
        "failure_flags": failure_flags,
        "latency_ms": latency_ms,
        "provider": payload.get("provider"),
        "routing_provider": routing.get("provider"),
        "providers_queried": routing.get("providers_queried"),
        "provider_errors": routing.get("provider_errors") or payload.get("provider_errors"),
        "result_count": len(results),
        "source_summary_count": len(source_summaries),
        "extracted_chars": extracted_chars,
        "domain_count": quality.get("domain_count", len(domains)),
        "domain_diversity": quality.get("domain_diversity"),
        "domains": domains,
        "dedup_count": quality.get("duplicate_count", metadata.get("dedup_count", 0)),
        "extract_recommended": quality.get("extract_recommended"),
        "extract_reasons": quality.get("extract_reasons"),
        "stderr_tail": stderr.strip()[-500:] if stderr else "",
    }


def run_case(
    case: Dict[str, Any],
    script_path: Path,
    modes: List[str],
    max_results: int,
    research_extract_count: int,
    timeout_seconds: int,
    env: Dict[str, str],
) -> List[Dict[str, Any]]:
    rows = []
    for mode in modes:
        cmd = [
            sys.executable,
            str(script_path),
            "--query",
            case["query"],
            "--provider",
            case.get("provider", "auto"),
            "--max-results",
            str(max_results),
            "--quality-report",
            "--no-cache",
            "--compact",
        ]
        if mode == "research":
            cmd += ["--mode", "research", "--research-extract-count", str(research_extract_count)]
            if case.get("research_providers"):
                cmd += ["--research-providers", *case["research_providers"]]
        start = time.perf_counter()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, env=env)
            latency_ms = int((time.perf_counter() - start) * 1000)
            payload = _json_from_stdout(proc.stdout if proc.returncode == 0 else (proc.stdout or proc.stderr))
            rows.append(summarize_result(case, mode, payload, latency_ms, proc.returncode, proc.stderr))
        except subprocess.TimeoutExpired as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            rows.append(summarize_result(
                case,
                mode,
                {"error": f"timeout after {timeout_seconds}s", "provider": case.get("provider", "auto"), "results": []},
                latency_ms,
                124,
                (e.stderr or "") if isinstance(e.stderr, str) else "",
            ))
    return rows


def write_jsonl(rows: Iterable[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_markdown_report(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = len(rows)
    ok = sum(1 for r in rows if r.get("status") == "ok" and not r.get("failure_flags"))
    errored = sum(1 for r in rows if r.get("status") != "ok")
    flagged = sum(1 for r in rows if r.get("failure_flags"))
    lines = [
        "# web-search-plus Golden Query Evaluation",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        f"- Rows: {total}",
        f"- Clean: {ok}",
        f"- Flagged: {flagged}",
        f"- Errors: {errored}",
        "",
        "## Results",
        "",
    ]
    for row in rows:
        flags = ", ".join(row.get("failure_flags") or []) or "none"
        providers = row.get("providers_queried") or row.get("provider") or "unknown"
        lines.extend([
            f"### {row['id']} — {row['mode']}",
            f"- Category: {row.get('category')}",
            f"- Provider(s): {providers}",
            f"- Status: {row.get('status')} / flags: {flags}",
            f"- Latency: {row.get('latency_ms')} ms",
            f"- Results: {row.get('result_count')} / source summaries: {row.get('source_summary_count')}",
            f"- Domains: {row.get('domain_count')} / diversity: {row.get('domain_diversity')}",
            f"- Dedup: {row.get('dedup_count')} / extract recommended: {row.get('extract_recommended')}",
            "",
        ])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run web-search-plus golden query evaluation")
    parser.add_argument("--queries", type=Path, help="Optional JSON file with golden query cases")
    parser.add_argument("--modes", nargs="+", default=["normal", "research"], choices=["normal", "research"])
    parser.add_argument("--max-results", type=int, default=4)
    parser.add_argument("--research-extract-count", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--out", type=Path, default=Path("eval/golden-results.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("eval/golden-report.md"))
    parser.add_argument("--limit", type=int, help="Limit number of cases for smoke runs")
    parser.add_argument(
        "--snapshot-fixtures",
        type=Path,
        help="Validate offline replay fixtures instead of making live provider calls",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    if args.snapshot_fixtures:
        fixture_path = args.snapshot_fixtures if args.snapshot_fixtures.is_absolute() else repo_root / args.snapshot_fixtures
        rows = run_snapshot_quality(load_snapshot_fixtures(fixture_path))
        write_jsonl(rows, repo_root / args.out if not args.out.is_absolute() else args.out)
        failed = [row for row in rows if row.get("status") != "ok"]
        print(json.dumps({
            "snapshots": len(rows),
            "out": str(args.out),
            "failed": len(failed),
            "failure_flags": {row["id"]: row.get("failure_flags") for row in failed},
        }, indent=2, sort_keys=True))
        return 1 if failed else 0

    script_path = repo_root / "search.py"
    cases = load_golden_queries(args.queries)
    if args.limit:
        cases = cases[: args.limit]

    env = os.environ.copy()
    rows: List[Dict[str, Any]] = []
    for case in cases:
        rows.extend(run_case(
            case=case,
            script_path=script_path,
            modes=args.modes,
            max_results=args.max_results,
            research_extract_count=args.research_extract_count,
            timeout_seconds=args.timeout,
            env=env,
        ))

    write_jsonl(rows, repo_root / args.out if not args.out.is_absolute() else args.out)
    write_markdown_report(rows, repo_root / args.report if not args.report.is_absolute() else args.report)
    print(json.dumps({
        "cases": len(cases),
        "rows": len(rows),
        "out": str(args.out),
        "report": str(args.report),
        "flagged": sum(1 for r in rows if r.get("failure_flags")),
        "errors": sum(1 for r in rows if r.get("status") != "ok"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
