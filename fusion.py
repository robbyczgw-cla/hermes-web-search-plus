"""Opt-in fusion mode: parallel multi-provider search merged with RRF.

Fusion mode sits between normal single-provider routing and the heavier research
mode. It queries a few providers concurrently and merges their ranked results with
Reciprocal Rank Fusion, trading a couple of extra provider calls for much better
coverage and cross-provider agreement — while staying far cheaper and faster than
research mode because it does no extraction.

It is best-effort: a provider that errors or runs past the wall-clock budget is
recorded and skipped, and whatever arrived in time is still fused and returned.
"""

from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
)
from typing import Any, Callable, Dict, List, Optional, Tuple

from quality import reciprocal_rank_fusion


def _build_fusion_payload(
    query: str,
    fused: List[Dict[str, Any]],
    providers_queried: List[str],
    provider_errors: List[Dict[str, Any]],
    fusion_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "mode": "fusion",
        "provider": "fusion",
        "query": query,
        "results": fused,
        "routing": {
            "providers_queried": providers_queried,
            "provider_errors": provider_errors,
            "fusion_method": fusion_metadata.get("fusion_method"),
            "fusion_k": fusion_metadata.get("fusion_k"),
        },
        "metadata": {
            "providers_merged": providers_queried,
            "dedup_count": 0,
            "unique_results": fusion_metadata.get("unique_results", 0),
            "overlap_count": fusion_metadata.get("overlap_count", 0),
        },
    }


def run_fusion_mode(
    query: str,
    fusion_providers: List[str],
    execute_search: Callable[[str], Dict[str, Any]],
    max_results: int,
    k: int = 60,
    time_budget_seconds: Optional[float] = None,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Query several providers in parallel and merge their results with RRF.

    Args:
        query: The search query (echoed back into the payload).
        fusion_providers: Providers to query concurrently, in preferred order.
        execute_search: Callable that runs one provider and returns its payload.
        max_results: Number of fused results to keep.
        k: RRF damping constant (standard value is 60).
        time_budget_seconds: Best-effort wall-clock cap. Providers slower than the
            budget are dropped from the merge and recorded as skipped. ``None`` waits
            for every provider (each call is still bounded by its own HTTP timeout).
        max_workers: Thread pool size; defaults to one worker per provider.

    Returns a fusion payload with merged ``results`` and routing/metadata diagnostics.
    Uses wall-clock collection (not an injected clock) because parallel fan-out is
    inherently real-time; the pool is shut down without waiting so a straggler cannot
    delay the response beyond the budget.
    """
    providers = [p for p in fusion_providers if p]
    provider_results: List[Tuple[str, Dict[str, Any]]] = []
    provider_errors: List[Dict[str, Any]] = []

    if not providers:
        fused, fusion_metadata = reciprocal_rank_fusion([], max_results, k=k)
        return _build_fusion_payload(query, fused, [], [], fusion_metadata)

    workers = max(1, max_workers or len(providers))
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_provider = {executor.submit(execute_search, p): p for p in providers}
        pending = set(future_to_provider)
        try:
            for future in as_completed(future_to_provider, timeout=time_budget_seconds):
                pending.discard(future)
                provider = future_to_provider[future]
                try:
                    provider_results.append((provider, future.result()))
                except Exception as exc:  # provider call failed; keep the rest
                    provider_errors.append({"provider": provider, "error": str(exc)})
        except FutureTimeoutError:
            for future in pending:
                future.cancel()
                provider_errors.append({
                    "provider": future_to_provider[future],
                    "error": "skipped: fusion time budget exhausted",
                })
    finally:
        # Do not block on stragglers; each provider call is HTTP-timeout bounded.
        executor.shutdown(wait=False)

    # Preserve the requested provider order for stable, reproducible provenance.
    succeeded = {p for p, _ in provider_results}
    ordered_results = [
        (p, payload)
        for p in providers
        for (rp, payload) in provider_results
        if rp == p and p in succeeded
    ]
    fused, fusion_metadata = reciprocal_rank_fusion(ordered_results, max_results, k=k)
    return _build_fusion_payload(
        query,
        fused,
        [p for p, _ in ordered_results],
        provider_errors,
        fusion_metadata,
    )
