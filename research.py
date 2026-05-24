"""Opt-in research mode orchestration."""

import time
from typing import Any, Dict, List, Tuple

from quality import deduplicate_results_across_providers


def run_research_mode(
    query: str,
    research_providers: List[str],
    execute_search,
    extract_urls,
    max_results: int,
    max_extract_urls: int = 3,
    time_budget_seconds: float | None = None,
    now_fn=None,
) -> Dict[str, Any]:
    """Run broad search, deduplicate, then extract top sources for grounding.

    Research mode is intentionally best-effort: provider/extraction failures should
    produce diagnostics and partial search results instead of throwing away the
    whole response. The optional time budget is checked between expensive calls so
    the mode can degrade safely before starting more provider work or extraction.
    """
    provider_results: List[Tuple[str, Dict[str, Any]]] = []
    provider_errors: List[Dict[str, Any]] = []
    now = now_fn or time.monotonic
    start = now()

    def budget_exhausted() -> bool:
        return time_budget_seconds is not None and (now() - start) >= time_budget_seconds

    for provider in research_providers:
        if budget_exhausted():
            provider_errors.append({"provider": provider, "error": "skipped: research time budget exhausted"})
            continue
        try:
            payload = execute_search(provider)
            provider_results.append((provider, payload))
        except Exception as e:
            provider_errors.append({"provider": provider, "error": str(e)})

    deduped, dedup_count = deduplicate_results_across_providers(provider_results, max_results)
    urls = [r.get("url") for r in deduped if r.get("url")][:max(0, max_extract_urls)]
    extracted = {"provider": None, "results": []}
    extraction_error = None
    if urls:
        if budget_exhausted():
            extraction_error = "skipped: research time budget exhausted"
        else:
            try:
                extracted = extract_urls(urls) or {"provider": None, "results": []}
            except Exception as e:
                extraction_error = str(e)
                extracted = {"provider": None, "results": []}

    routing = {
        "providers_queried": [p for p, _ in provider_results],
        "provider_errors": provider_errors,
        "extraction_provider": extracted.get("provider"),
    }
    if extraction_error:
        routing["extraction_error"] = extraction_error

    source_summaries = extracted.get("results", []) or []

    return {
        "mode": "research",
        "provider": "research",
        "query": query,
        "results": deduped,
        "source_summaries": source_summaries,
        "routing": routing,
        "metadata": {
            "dedup_count": dedup_count,
            "providers_merged": [p for p, _ in provider_results],
            "extracted_url_count": len(source_summaries),
        },
    }
