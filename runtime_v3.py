"""Normalize legacy provider-core payloads into the frozen ResponseV3 DTO."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Mapping
from urllib.parse import urlsplit, urlunsplit

from contract_v3 import (
    AttemptOutcome,
    Capability,
    CircuitState,
    DegradedReason,
    ErrorClass,
    ErrorV3,
    FallbackReason,
    ProviderAttemptV3,
    RequestV3,
    ResponseStatus,
    ResponseV3,
    SkipReason,
)
from orchestrator_v3 import ProviderPlan


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _valid_rfc3339(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return value


def _error(message: str, provider: str | None = None) -> ErrorV3:
    lowered = message.lower()
    if "missing" in lowered and ("key" in lowered or "credential" in lowered):
        error_class = ErrorClass.CONFIG
        code = "wsp.config.missing_credentials"
    elif "timeout" in lowered or "timed out" in lowered:
        error_class = ErrorClass.TIMEOUT
        code = "wsp.provider.timeout"
    elif "rate" in lowered or "429" in lowered:
        error_class = ErrorClass.RATE_LIMIT
        code = "wsp.provider.rate_limit"
    elif "security" in lowered or "blocked" in lowered:
        error_class = ErrorClass.SECURITY
        code = "wsp.security.request_blocked"
    else:
        error_class = ErrorClass.TRANSIENT
        code = "wsp.provider.failed"
    return ErrorV3(
        error_class=error_class,
        code=code,
        message=message or "Provider execution failed",
        retryable=error_class
        in {ErrorClass.TIMEOUT, ErrorClass.RATE_LIMIT, ErrorClass.TRANSIENT},
        provider=provider,
    )


def _error_items(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for candidate in (
        payload.get("provider_errors"),
        payload.get("fallback_errors"),
        (payload.get("routing") or {}).get("fallback_errors"),
    ):
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict) and item not in values:
                    values.append(dict(item))
    return values


def _attempts(
    request: RequestV3,
    plan: ProviderPlan,
    payload: Mapping[str, Any],
    selected: str | None,
    result_count: int,
) -> List[ProviderAttemptV3]:
    attempts: List[ProviderAttemptV3] = []
    for index, item in enumerate(_error_items(payload), 1):
        provider = str(
            item.get("provider")
            or plan.candidate_order[min(index - 1, len(plan.candidate_order) - 1)]
        )
        error = _error(str(item.get("error") or "Provider execution failed"), provider)
        attempts.append(
            ProviderAttemptV3(
                attempt_id=f"attempt-{index}",
                provider=provider,
                capability=request.capability,
                outcome=AttemptOutcome.FAILED,
                result_count=0,
                error=error,
                circuit_state_before=CircuitState.UNKNOWN,
                circuit_state_after=CircuitState.UNKNOWN,
            )
        )
    if selected and (result_count or not payload.get("error")):
        attempts.append(
            ProviderAttemptV3(
                attempt_id=f"attempt-{len(attempts) + 1}",
                provider=selected,
                capability=request.capability,
                outcome=AttemptOutcome.SUCCESS,
                result_count=result_count,
                circuit_state_before=CircuitState.UNKNOWN,
                circuit_state_after=CircuitState.CLOSED,
            )
        )
    return attempts


PROJECTION_REQUIRED_PROVIDERS = frozenset({"parallel", "you"})


def segment_canonical_text(text: str) -> List[Dict[str, Any]]:
    """Return contiguous NFC unicode-codepoint segments without rewriting text."""
    text = unicodedata.normalize("NFC", text)
    if not text:
        return []
    return [{"start": 0, "end": len(text), "text": text}]


def _projected_text(observation: Mapping[str, Any], source_field: str) -> Dict[str, Any] | None:
    value = observation.get(source_field)
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    return {
        "text": value,
        "text_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "origin": "provider",
        "provenance": {
            "observation_id": observation["observation_id"],
            "source_field": source_field,
            "transformations": ["mechanical_segmentation"],
        },
        "segments": segment_canonical_text(value),
    }


def _observation(
    item: Mapping[str, Any],
    *,
    provider: str,
    endpoint_id: str,
    attempt_id: str,
    index: int,
    capability: Capability,
) -> Dict[str, Any] | None:
    observed_url = str(item.get("url") or "")
    if not observed_url:
        return None
    kind = "search_result" if capability is Capability.SEARCH else "extracted_document"
    snippet = (
        unicodedata.normalize("NFC", str(item.get("snippet") or ""))
        if capability is Capability.SEARCH
        else None
    )
    text = None
    if capability is Capability.EXTRACT and not item.get("error"):
        text = unicodedata.normalize(
            "NFC", str(item.get("content") or item.get("raw_content") or "")
        )
    score = item.get("score")
    provider_score = None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        provider_score = {"value": float(score), "semantics": "unknown"}
    raw_date = item.get("published_at") or item.get("published_date") or item.get("date")
    published_at = None
    if isinstance(raw_date, str):
        published_at = {"raw": raw_date, "normalized": _valid_rfc3339(raw_date)}
    return {
        "observation_id": _stable_id("obs", attempt_id, index),
        "provider_attempt_id": attempt_id,
        "provider_result_index": index,
        "provider": provider,
        "endpoint_id": endpoint_id,
        "kind": kind,
        "url": {"observed": observed_url, "canonical": _canonical_url(observed_url)},
        "title": (
            unicodedata.normalize("NFC", str(item.get("title")))
            if item.get("title") is not None
            else None
        ),
        "snippet": snippet,
        "text": text,
        "provider_rank": index + 1,
        "provider_score": provider_score,
        "published_at": published_at,
        "provider_fields": {},
    }


def observations_from_legacy(
    payload: Mapping[str, Any],
    provider: str,
    capability: Capability,
    attempt_id: str,
) -> List[Dict[str, Any]]:
    endpoint_id = f"{provider}:{capability.value}"
    observations = []
    for index, item in enumerate(payload.get("results") or []):
        observation = _observation(
            item,
            provider=provider,
            endpoint_id=endpoint_id,
            attempt_id=attempt_id,
            index=index,
            capability=capability,
        )
        if observation is not None:
            observations.append(observation)
    return observations


def project_results_from_observations(
    observations: List[Dict[str, Any]],
    selected_items: List[Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """Project selected source clusters without deleting non-selected observations."""
    representatives = []
    if selected_items is None:
        representatives = [(observation, [observation]) for observation in observations]
    else:
        clusters: Dict[str, List[Dict[str, Any]]] = {}
        for observation in observations:
            clusters.setdefault(observation["url"]["canonical"], []).append(observation)
        emitted = set()
        for item in selected_items:
            canonical = _canonical_url(str(item.get("url") or ""))
            members = clusters.get(canonical) or []
            if not members or canonical in emitted:
                continue
            observed = str(item.get("url") or "")
            representative = next(
                (
                    observation
                    for observation in members
                    if observation["url"]["observed"] == observed
                ),
                members[0],
            )
            representatives.append((representative, members))
            emitted.add(canonical)

    results = []
    for rank, (observation, members) in enumerate(representatives, 1):
        observation_id = observation["observation_id"]
        cluster_id = _stable_id("cluster", observation["url"]["canonical"])
        results.append(
            {
                "result_id": _stable_id("result", observation_id),
                "kind": observation["kind"],
                "engine_rank": rank,
                "representative_observation_id": observation_id,
                "observation_ids": [member["observation_id"] for member in members],
                "dedup_cluster_id": cluster_id,
                "url": dict(observation["url"]),
                "title": _projected_text(observation, "title"),
                "snippet": _projected_text(observation, "snippet"),
                "text": _projected_text(observation, "text"),
            }
        )
    return results


def _source_diversity(observations: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    providers = {item["provider"] for item in observations}
    hosts = {
        urlsplit(item["url"]["canonical"]).hostname
        for item in observations
        if urlsplit(item["url"]["canonical"]).hostname
    }
    clusters = {item["dedup_cluster_id"] for item in results}
    return {
        "method": "component_count",
        "method_version": "1",
        "method_degraded": False,
        "provider_count": len(providers),
        "host_count": len(hosts),
        "source_family_count": len(providers),
        "unique_cluster_count": len(clusters),
    }


def render_response_v3(response: ResponseV3) -> str:
    """Render source projections only; this formatter has no answer concept."""
    lines = []
    for result in response.results:
        title = (result.get("title") or {}).get("text") or "Untitled source"
        url = (result.get("url") or {}).get("observed") or ""
        snippet = (result.get("snippet") or result.get("text") or {}).get("text") or ""
        lines.append(title)
        if url:
            lines.append(url)
        if snippet:
            lines.append(snippet)
    return "\n".join(lines)


def response_from_legacy(
    request: RequestV3,
    plan: ProviderPlan,
    payload: Dict[str, Any],
) -> ResponseV3:
    """Build a contract-valid ResponseV3 without modifying the legacy payload."""
    request_id = request.request_id or plan.execution_id
    routing = payload.get("routing") or {}
    aggregate_research = payload.get("provider") == "research"
    selected = (
        None
        if aggregate_research
        else routing.get("provider") or payload.get("provider") or plan.selected_provider
    )
    raw_items = [item for item in (payload.get("results") or []) if item.get("url")]
    observation_items = [
        item
        for item in (payload.get("_v3_raw_results") or raw_items)
        if isinstance(item, Mapping) and item.get("url")
    ]
    authoritative_attempts = payload.get("_v3_provider_attempts")
    provider_attempts = (
        list(authoritative_attempts)
        if isinstance(authoritative_attempts, (list, tuple))
        else _attempts(request, plan, payload, selected, len(raw_items))
    )
    successful_attempt = next(
        (attempt for attempt in reversed(provider_attempts) if attempt.outcome is AttemptOutcome.SUCCESS),
        None,
    )
    if aggregate_research and authoritative_attempts:
        observations = []
        for attempt in provider_attempts:
            if attempt.outcome is not AttemptOutcome.SUCCESS:
                continue
            provider_items = [
                item
                for item in observation_items
                if str(item.get("provider") or "") == attempt.provider
            ]
            if provider_items:
                observations.extend(
                    observations_from_legacy(
                        {**payload, "results": provider_items},
                        attempt.provider,
                        request.capability,
                        attempt.attempt_id,
                    )
                )
    else:
        observations = (
            observations_from_legacy(
                {**payload, "results": observation_items},
                str(selected),
                request.capability,
                successful_attempt.attempt_id,
            )
            if selected and successful_attempt
            else []
        )
    results = project_results_from_observations(observations, raw_items)
    failed_items = [item for item in raw_items if item.get("error")]
    top_error = payload.get("error")
    warnings: List[Dict[str, Any]] = []
    error = None
    if top_error and not results:
        status = ResponseStatus.FAILED
        error = next(
            (
                attempt.error
                for attempt in provider_attempts
                if attempt.provider == selected and attempt.error is not None
            ),
            None,
        ) or next(
            (
                attempt.error
                for attempt in reversed(provider_attempts)
                if attempt.error is not None
            ),
            None,
        ) or _error(str(top_error), str(selected) if selected else None)
    elif failed_items:
        status = ResponseStatus.DEGRADED
        warnings.append(
            {
                "code": DegradedReason.PARTIAL_EXTRACTION.value,
                "message": "One or more extraction results failed.",
                "details": {"failed_count": len(failed_items)},
            }
        )
    else:
        status = ResponseStatus.OK

    budget_limited = payload.get("_v3_budget_limited") is True or any(
        attempt.outcome is AttemptOutcome.CANCELLED
        or attempt.skip_reason
        in {SkipReason.BUDGET_BLOCKED, SkipReason.DEADLINE_EXCEEDED}
        for attempt in provider_attempts
    )
    if status is not ResponseStatus.FAILED and budget_limited:
        status = ResponseStatus.DEGRADED
        warnings.append(
            {
                "code": DegradedReason.BUDGET_LIMITED.value,
                "message": "Execution was limited by its attempt or time budget.",
            }
        )

    if any(
        attempt.budget_decision == "store_unavailable"
        for attempt in provider_attempts
    ):
        warnings.append(
            {
                "code": "wsp.state.store_unavailable",
                "message": (
                    "Operational state unavailable; provider execution continued "
                    "without persistent circuit or budget state."
                ),
            }
        )

    fallback_used = bool(routing.get("fallback_used") or _error_items(payload))
    fallback_reason = (
        FallbackReason.SELECTED_FAILED.value
        if fallback_used
        else FallbackReason.NONE.value
    )
    cached = bool(payload.get("cached"))
    if request.cache.get("mode") == "bypass":
        cache_status = {"disposition": "bypassed"}
    elif cached:
        cache_status = {
            "disposition": "fresh_hit",
            "age_seconds": max(0, int(payload.get("cache_age_seconds", 0))),
            "source_contract_version": "2.x",
        }
    else:
        cache_status = {"disposition": "miss"}

    clusters = [
        {
            "dedup_cluster_id": result["dedup_cluster_id"],
            "observation_ids": list(result["observation_ids"]),
            "representative_observation_id": result["representative_observation_id"],
        }
        for result in results
    ]
    policy_actions = [
        {
            "action": "selected_as_representative",
            "observation_id": result["representative_observation_id"],
            "reason": "dedup_representative",
        }
        for result in results
    ]
    selected_observation_ids = {
        observation_id
        for result in results
        for observation_id in result["observation_ids"]
    }
    spam_domains = set(
        ((payload.get("metadata") or {}).get("spam_filtered") or {}).get("domains")
        or []
    )
    for observation in observations:
        if observation["observation_id"] in selected_observation_ids:
            continue
        host = urlsplit(observation["url"]["canonical"]).hostname or ""
        if host in spam_domains:
            policy_actions.append(
                {
                    "action": "excluded",
                    "observation_id": observation["observation_id"],
                    "reason": "spam_domain",
                }
            )
    return ResponseV3(
        request_id=request_id,
        execution_id=plan.execution_id,
        capability=request.capability,
        status=status,
        results=results,
        observations=observations,
        policy_actions=policy_actions,
        source_diversity=_source_diversity(observations, results),
        provider_attempts=provider_attempts,
        routing_receipt={
            "policy_id": "classic",
            "policy_revision": str(routing.get("routing_policy") or "v2.9.1"),
            "mode": plan.mode,
            "candidate_order": list(plan.candidate_order),
            "selected_provider": selected if results else None,
            "fallback_reason": fallback_reason,
        },
        cache_status=cache_status,
        limits_applied={"max_results": request.options.get("max_results")}
        if request.capability is Capability.SEARCH
        else {},
        dedup_clusters=clusters,
        warnings=warnings,
        error=error,
    )
