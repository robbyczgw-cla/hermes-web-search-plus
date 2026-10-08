"""Research mode orchestration helpers."""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .daemon_tasks import DaemonTask
from .diversity_v3 import DEFAULT_NEAR_DUPLICATE_THRESHOLD, rerank_duplicate_candidates
from .quality import deduplicate_results_across_providers, normalize_result_url


# Small real-time grace given to already-submitted provider calls once the
# (possibly fake-clock) budget reads as exhausted, so completed futures can
# still be harvested without blocking on slow ones.
_RESULT_GRACE_SECONDS = 0.25
_DEFAULT_QUORUM_RESULT_TARGET_CAP = 5
_DEFAULT_QUORUM_MIN_DOMAINS = 3


def _positive_int(value: Any, default: int, minimum: int = 1) -> int:
    """Return a bounded integer without letting direct callers disable safety."""
    if isinstance(value, bool):
        return default
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _research_quorum_snapshot(
    provider_results: List[Tuple[str, Dict[str, Any]]],
    *,
    result_target: int,
) -> Tuple[int, int, List[str]]:
    """Return unique candidate/domain/provider counts for an early-return check.

    This intentionally mirrors stable provider order rather than arrival order.
    ``result_target`` is a minimum threshold, not a scan cap: all completed
    providers must remain able to contribute evidence even when the first one
    alone fills the public result page. Duplicate/empty responses still cannot
    manufacture a quorum.
    """
    seen_urls = set()
    domains = set()
    contributors: List[str] = []
    candidate_count = 0
    for provider, payload in provider_results:
        contributed = False
        for item in payload.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            normalized = normalize_result_url(url)
            if not normalized or normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            candidate_count += 1
            contributed = True
            domain = urlparse(url).netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            if domain:
                domains.add(domain)
        if contributed:
            contributors.append(provider)
    return candidate_count, len(domains), contributors


def _quorum_metadata(
    provider_results: List[Tuple[str, Dict[str, Any]]],
    *,
    enabled: bool,
    min_contributing_providers: int,
    result_target: int,
    min_unique_domains: int,
    triggered: bool,
) -> Dict[str, Any]:
    candidate_count, domain_count, contributors = _research_quorum_snapshot(
        provider_results, result_target=result_target
    )
    return {
        "enabled": enabled,
        "triggered": triggered,
        "min_contributing_providers": min_contributing_providers,
        "result_target": result_target,
        "min_unique_domains": min_unique_domains,
        "contributing_providers": contributors,
        "deduplicated_result_count": candidate_count,
        "unique_domain_count": domain_count,
    }


def run_research_mode(
    query: str,
    research_providers: List[str],
    execute_search,
    extract_urls,
    max_results: int,
    max_extract_urls: int = 3,
    time_budget_seconds: float | None = None,
    now_fn=None,
    max_workers: int | None = None,
    on_provider_timeout=None,
    diversity_rerank: bool = False,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    quorum_enabled: bool = True,
    quorum_min_contributing_providers: int = 2,
    quorum_result_target_cap: int = _DEFAULT_QUORUM_RESULT_TARGET_CAP,
    quorum_min_unique_domains: int = _DEFAULT_QUORUM_MIN_DOMAINS,
) -> Dict[str, Any]:
    """Run broad search, deduplicate, then extract top sources for grounding.

    Research mode is intentionally best-effort: provider/extraction failures should
    produce diagnostics and partial search results instead of throwing away the
    whole response. Provider searches run concurrently to keep the wall-clock cost
    close to the slowest single provider rather than the sum of all of them. The
    optional time budget gates which providers are launched (checked before each
    submission, so a tight budget still skips later providers deterministically),
    bounds how long already-launched providers may run, and gates whether
    extraction runs at all — so the budget caps total wall-clock time instead of
    only limiting how many providers start.

    Results are harvested in completion order so a blocked early submission
    cannot hide later providers. Final provider/result ordering nevertheless
    stays in submission order for deterministic deduplication. Once at least
    two providers have contributed unique URL results, an opt-in-by-default
    conservative quorum may return early only after its capped result target
    and domain-diversity target are both met; otherwise every provider remains
    eligible and small/poor result sets retain their full recall.

    The completion-order/quorum design adapts ideas from the independent
    MIT-licensed Hound/Master-Fetch project by Bishesh Bhandari (dondai1234),
    reworked here around WSP's own provider, budget, and receipt contracts.
    """
    provider_errors: List[Dict[str, Any]] = []
    now = now_fn or time.monotonic
    start = now()

    def remaining_budget() -> Optional[float]:
        if time_budget_seconds is None:
            return None
        return time_budget_seconds - (now() - start)

    # Submit providers (budget gate is sequential/deterministic); the actual
    # provider HTTP calls run concurrently on daemon threads. Daemon threads —
    # unlike ThreadPoolExecutor workers — are not joined at interpreter exit,
    # so an overdue provider cannot stall CLI/subprocess shutdown either.
    pending: List[Tuple[int, str]] = []
    tasks: Dict[int, DaemonTask] = {}
    completed = queue.Queue()
    workers = max_workers or max(1, len(research_providers))
    gate = threading.Semaphore(workers)

    # At most the requested result count is needed, but asking for a huge result
    # page must not turn the early-return quality bar into an arbitrary latency
    # deadline. The cap is an explicit operator setting and is deliberately
    # conservative at five by default.
    quorum_enabled = quorum_enabled is True
    quorum_min_contributing_providers = max(
        2, _positive_int(quorum_min_contributing_providers, 2)
    )
    quorum_result_target_cap = _positive_int(
        quorum_result_target_cap, _DEFAULT_QUORUM_RESULT_TARGET_CAP
    )
    quorum_result_target = min(
        max(1, _positive_int(max_results, 1)), quorum_result_target_cap
    )
    quorum_min_unique_domains = min(
        quorum_result_target,
        _positive_int(quorum_min_unique_domains, _DEFAULT_QUORUM_MIN_DOMAINS),
    )

    def run_gated(provider_name: str) -> Dict[str, Any]:
        with gate:
            return execute_search(provider_name)

    for index, provider in enumerate(research_providers):
        remaining = remaining_budget()
        if remaining is not None and remaining <= 0:
            provider_errors.append({"provider": provider, "error": "skipped: research time budget exhausted"})
            continue
        task = DaemonTask(run_gated, provider)
        tasks[index] = task
        pending.append((index, provider))
        task.add_done_callback(lambda _task, completed_index=index: completed.put(completed_index))

    results_by_index: Dict[int, Tuple[str, Dict[str, Any]]] = {}
    pending_by_index = {index: provider for index, provider in pending}
    quorum_triggered = False

    def provider_results_in_submission_order() -> List[Tuple[str, Dict[str, Any]]]:
        return [results_by_index[index] for index in sorted(results_by_index)]

    def harvest(index: int) -> None:
        """Collect an already-completed provider without introducing a wait."""
        provider = pending_by_index.pop(index, None)
        if provider is None:
            return
        try:
            payload = tasks[index].result(timeout=0)
            if not isinstance(payload, dict):
                raise TypeError("provider returned a non-object result")
            results_by_index[index] = (provider, payload)
        except FuturesTimeoutError:
            # A completion callback and Event publication are ordered, but keep
            # this defensive branch non-blocking if a custom task ever differs.
            pending_by_index[index] = provider
        except Exception as e:
            provider_errors.append({"provider": provider, "error": str(e)})

    def drain_completed() -> None:
        """Harvest all completions currently observable in arrival order."""
        while True:
            try:
                index = completed.get_nowait()
            except queue.Empty:
                break
            harvest(index)
        # A callback is advisory. The done() fallback closes a tiny race between
        # worker completion and queue notification without waiting on any task.
        for index in sorted(pending_by_index):
            if tasks[index].done():
                harvest(index)

    def quorum_reached() -> bool:
        if not quorum_enabled:
            return False
        candidate_count, domain_count, contributors = _research_quorum_snapshot(
            provider_results_in_submission_order(), result_target=quorum_result_target
        )
        return (
            len(contributors) >= quorum_min_contributing_providers
            and candidate_count >= quorum_result_target
            and domain_count >= quorum_min_unique_domains
        )

    def mark_budget_timeouts() -> None:
        # A final non-blocking drain avoids classifying a just-finished daemon as
        # timed out. Remaining daemon tasks keep running but never block exit.
        drain_completed()
        for index in sorted(pending_by_index):
            provider = pending_by_index.pop(index)
            if on_provider_timeout is not None:
                on_provider_timeout(provider)
            provider_errors.append({"provider": provider, "error": "timed out: research time budget exhausted"})

    while pending_by_index:
        drain_completed()
        if not pending_by_index:
            break
        if quorum_reached():
            # Only incomplete tasks are preempted. Completed work was drained
            # above, preserving recall whenever it is already available.
            for index in sorted(pending_by_index):
                provider = pending_by_index.pop(index)
                provider_errors.append({"provider": provider, "error": "preempted_after_quorum"})
            quorum_triggered = True
            break

        remaining = remaining_budget()
        timeout = remaining
        if timeout is not None and timeout <= 0:
            # Preserve the existing real-time grace for fake-clock callers and
            # races at the deadline; never wait beyond this short fixed grace.
            timeout = _RESULT_GRACE_SECONDS
        try:
            index = completed.get(timeout=timeout)
        except queue.Empty:
            drain_completed()
            if quorum_reached():
                continue
            current_remaining = remaining_budget()
            if current_remaining is not None and current_remaining <= 0:
                mark_budget_timeouts()
                break
            # A spurious/expired wait with an injected clock: loop and derive a
            # fresh deadline instead of translating it into a provider timeout.
            continue
        harvest(index)

    # Completion timing must not leak into the public diagnostic order.
    provider_order = {
        provider: index for index, provider in enumerate(research_providers)
    }
    provider_errors.sort(
        key=lambda item: (
            provider_order.get(
                str(item.get("provider") or ""), len(provider_order)
            ),
            str(item.get("error") or ""),
        )
    )

    provider_results = provider_results_in_submission_order()

    diversity_duplicates = []
    if diversity_rerank:
        # The normal merge drops cross-provider URL duplicates.  The explicit
        # diversity mode instead retains every candidate long enough to move
        # duplicate URLs and snippets behind the diverse head, then applies
        # the caller's ordinary result-count cap.
        merged_candidates: List[Dict[str, Any]] = []
        for provider_name, payload in provider_results:
            for item in payload.get("results", []):
                if not isinstance(item, dict):
                    continue
                candidate = item.copy()
                candidate.setdefault("provider", provider_name)
                merged_candidates.append(candidate)
        reranked_candidates, diversity_duplicates = rerank_duplicate_candidates(
            merged_candidates, near_duplicate_threshold=near_duplicate_threshold
        )
        deduped = reranked_candidates[:max_results]
        dedup_count = 0
    else:
        deduped, dedup_count = deduplicate_results_across_providers(provider_results, max_results)
    urls = [r.get("url") for r in deduped if r.get("url")][:max(0, max_extract_urls)]
    extracted = {"provider": None, "results": []}
    extraction_error = None
    if urls:
        remaining = remaining_budget()
        if remaining is not None and remaining <= 0:
            extraction_error = "skipped: research time budget exhausted"
        elif remaining is None:
            try:
                extracted = extract_urls(urls) or {"provider": None, "results": []}
            except Exception as e:
                extraction_error = str(e)
                extracted = {"provider": None, "results": []}
        else:
            # Run extraction on a daemon thread so the remaining budget bounds it too.
            try:
                extracted = DaemonTask(extract_urls, urls).result(timeout=remaining) or {"provider": None, "results": []}
            except FuturesTimeoutError:
                extraction_error = "timed out: research time budget exhausted"
                extracted = {"provider": None, "results": []}
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

    metadata = {
        "dedup_count": dedup_count,
        "providers_merged": [p for p, _ in provider_results],
        "extracted_url_count": len(source_summaries),
        "research_quorum": _quorum_metadata(
            provider_results,
            enabled=quorum_enabled,
            min_contributing_providers=quorum_min_contributing_providers,
            result_target=quorum_result_target,
            min_unique_domains=quorum_min_unique_domains,
            triggered=quorum_triggered,
        ),
    }
    if diversity_rerank:
        metadata["diversity_reranked"] = len(diversity_duplicates)

    return {
        "mode": "research",
        "provider": "research",
        "query": query,
        "results": deduped,
        "source_summaries": source_summaries,
        "routing": routing,
        "metadata": metadata,
    }
