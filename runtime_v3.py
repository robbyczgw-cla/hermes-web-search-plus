"""Normalize legacy provider-core payloads into the frozen ResponseV3 DTO."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timezone
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
)
from orchestrator_v3 import ProviderPlan
from independence_v3 import analyze_source_independence


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


def _provenance(
    provider: str, url: str, rank: int, retrieved_at: str
) -> List[Dict[str, Any]]:
    return [
        {
            "provider": provider,
            "source_url": url,
            "retrieved_at": retrieved_at,
            "provider_rank": rank,
        }
    ]


def _search_results(
    payload: Mapping[str, Any], provider: str, retrieved_at: str
) -> List[Dict[str, Any]]:
    normalized = []
    for rank, item in enumerate(payload.get("results") or [], 1):
        url = str(item.get("url") or "")
        if not url:
            continue
        canonical = _canonical_url(url)
        result = {
            "result_id": _stable_id("search", canonical, rank),
            "status": "ok",
            "title": str(item.get("title") or ""),
            "url": url,
            "canonical_url": canonical,
            "provenance": _provenance(provider, url, rank, retrieved_at),
        }
        if item.get("snippet") is not None:
            result["snippet"] = str(item["snippet"])
        published_at = _valid_rfc3339(item.get("published_at"))
        if published_at:
            result["published_at"] = published_at
        normalized.append(result)
    return normalized


def _extract_results(
    payload: Mapping[str, Any], provider: str, retrieved_at: str
) -> List[Dict[str, Any]]:
    normalized = []
    for rank, item in enumerate(payload.get("results") or [], 1):
        url = str(item.get("url") or "")
        if not url:
            continue
        canonical = _canonical_url(url)
        base = {
            "result_id": _stable_id("extract", canonical, rank),
            "title": str(item.get("title") or ""),
            "url": url,
            "canonical_url": canonical,
            "provenance": _provenance(provider, url, rank, retrieved_at),
        }
        if item.get("error"):
            base.update(
                {
                    "status": "failed",
                    "error": _error(str(item["error"]), provider).to_dict(),
                }
            )
        else:
            text = unicodedata.normalize(
                "NFC", str(item.get("content") or item.get("raw_content") or "")
            )
            base.update(
                {
                    "status": "ok",
                    "text": text,
                    "offset_unit": "unicode_codepoint",
                    "text_normalization": "NFC",
                    "segments": (
                        [{"segment_id": "segment-1", "start": 0, "end": len(text)}]
                        if text
                        else []
                    ),
                }
            )
        normalized.append(base)
    return normalized


def response_from_legacy(
    request: RequestV3,
    plan: ProviderPlan,
    payload: Dict[str, Any],
) -> ResponseV3:
    """Build a contract-valid ResponseV3 without modifying the legacy payload."""
    request_id = request.request_id or plan.execution_id
    routing = payload.get("routing") or {}
    selected = (
        routing.get("provider") or payload.get("provider") or plan.selected_provider
    )
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if request.capability is Capability.SEARCH:
        results = _search_results(payload, str(selected), retrieved_at)
    else:
        results = _extract_results(payload, str(selected), retrieved_at)

    failed_items = [item for item in results if item.get("status") == "failed"]
    top_error = payload.get("error")
    warnings: List[Dict[str, Any]] = []
    error = None
    if top_error and not results:
        status = ResponseStatus.FAILED
        error = _error(str(top_error), str(selected) if selected else None)
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

    clusters, independence_estimate = analyze_source_independence(results)
    return ResponseV3(
        request_id=request_id,
        capability=request.capability,
        status=status,
        results=results,
        provider_attempts=_attempts(request, plan, payload, selected, len(results)),
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
        source_independence_estimate=independence_estimate,
        warnings=warnings,
        error=error,
    )
