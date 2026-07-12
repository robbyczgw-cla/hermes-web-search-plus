"""Frozen Web Search Plus v3 contract DTOs.

This module is deliberately provider- and policy-agnostic. It defines the
wire-level request/response vocabulary used by the engine, compatibility
projections, golden fixtures, and external adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


CONTRACT_VERSION = "3.0"


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Capability(StrEnum):
    SEARCH = "search"
    EXTRACT = "extract"


class ResponseStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class DegradedReason(StrEnum):
    SERVED_STALE = "wsp.cache.served_stale"
    CONTENT_TRUNCATED = "wsp.content.truncated"
    URLS_OMITTED = "wsp.extract.urls_omitted"
    PARTIAL_EXTRACTION = "wsp.extract.partial"
    BUDGET_LIMITED = "wsp.budget.limited"
    FINGERPRINTING_REDUCED = "wsp.independence.method_degraded"


class ErrorClass(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED = "unsupported"
    CONFIG = "config"
    AUTH = "auth"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    PROVIDER_CONTRACT = "provider_contract"
    CONTENT = "content"
    SECURITY = "security"
    BUDGET = "budget"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SkipReason(StrEnum):
    DISABLED = "disabled"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    NOT_CONFIGURED = "not_configured"
    MISSING_CREDENTIALS = "missing_credentials"
    AUTH_BLOCKED = "auth_blocked"
    QUOTA_BLOCKED = "quota_blocked"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    BUDGET_BLOCKED = "budget_blocked"
    POLICY_EXCLUDED = "policy_excluded"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class FallbackReason(StrEnum):
    NONE = "none"
    SELECTED_FAILED = "selected_failed"
    SELECTED_SKIPPED = "selected_skipped"
    INSUFFICIENT_RESULTS = "insufficient_results"
    PARTIAL_CONTENT = "partial_content"
    BUDGET_CHAIN = "budget_chain"


class CandidateDecision(StrEnum):
    SELECTED = "selected"
    ATTEMPTED_FAILED = "attempted_failed"
    ATTEMPTED_NO_SELECTION = "attempted_no_selection"
    SKIPPED = "skipped"
    NOT_ATTEMPTED = "not_attempted"
    ORIGIN_SELECTED = "origin_selected"


class CandidateReasonCode(StrEnum):
    CLASSIC_SELECTED = "classic_selected"
    FALLBACK_SELECTED = "fallback_selected"
    ATTEMPT_FAILED = "attempt_failed"
    INSUFFICIENT_RESULTS = "insufficient_results"
    BLOCKED_AUTH = "blocked_auth"
    BLOCKED_QUOTA = "blocked_quota"
    CIRCUIT_OPEN = "circuit_open"
    BUDGET_DENIED = "budget_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NOT_ATTEMPTED_AFTER_SUCCESS = "not_attempted_after_success"
    CACHE_ORIGIN_SELECTED = "cache_origin_selected"


class CacheDisposition(StrEnum):
    FRESH_HIT = "fresh_hit"
    STALE_HIT = "stale_hit"
    MISS = "miss"
    BYPASSED = "bypassed"
    UNAVAILABLE = "unavailable"


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    BLOCKED_AUTH = "blocked_auth"
    BLOCKED_QUOTA = "blocked_quota"
    UNKNOWN = "unknown"


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if item is not None}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _wire_plain(value: Any) -> Any:
    """Serialize contract payloads while preserving required nullable fields."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_wire_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _wire_plain(item) for key, item in value.items()}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass(frozen=True)
class ErrorV3:
    error_class: ErrorClass
    code: str
    message: str
    retryable: bool = False
    provider: Optional[str] = None
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _plain(self.__dict__)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ErrorV3":
        return cls(
            error_class=ErrorClass(payload["error_class"]),
            code=str(payload["code"]),
            message=str(payload["message"]),
            retryable=bool(payload.get("retryable", False)),
            provider=payload.get("provider"),
            http_status=payload.get("http_status"),
            retry_after_seconds=payload.get("retry_after_seconds"),
            details=dict(payload.get("details") or {}),
        )


@dataclass(frozen=True)
class ProviderAttemptV3:
    attempt_id: str
    provider: str
    capability: Capability
    outcome: AttemptOutcome
    retry_count: int = 0
    result_count: int = 0
    started_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[ErrorV3] = None
    skip_reason: Optional[SkipReason] = None
    budget_decision: Optional[str] = None
    circuit_state_before: CircuitState = CircuitState.UNKNOWN
    circuit_state_after: CircuitState = CircuitState.UNKNOWN
    endpoint_id: str = ""
    decision: str = "attempted"
    tries: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.retry_count < 0 or self.result_count < 0:
            raise ValueError("retry_count and result_count must be non-negative")
        if self.outcome is AttemptOutcome.SKIPPED and self.skip_reason is None:
            raise ValueError("skipped attempts require skip_reason")
        if self.outcome is AttemptOutcome.FAILED and self.error is None:
            raise ValueError("failed attempts require error")
        if (
            self.outcome in {AttemptOutcome.SUCCESS, AttemptOutcome.PARTIAL}
            and self.skip_reason is not None
        ):
            raise ValueError("executed attempts cannot carry skip_reason")
        if self.decision not in {"attempted", "skipped"}:
            raise ValueError("attempt decision must be attempted or skipped")
        if self.decision == "skipped" and (self.skip_reason is None or self.tries):
            raise ValueError("skipped decisions require skip_reason and no tries")
        if self.endpoint_id and self.decision == "attempted" and not self.tries:
            raise ValueError("attempted decisions require at least one try")
        for index, provider_try in enumerate(self.tries, 1):
            if provider_try.get("try_number") != index:
                raise ValueError("provider tries must be densely numbered from one")
            if provider_try.get("outcome") not in {"success", "error"}:
                raise ValueError("provider try has invalid outcome")
            has_error = provider_try.get("error") is not None
            if has_error != (provider_try.get("outcome") == "error"):
                raise ValueError("provider try error must match its outcome")

    def to_dict(self) -> Dict[str, Any]:
        payload = _wire_plain(self.__dict__)
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ProviderAttemptV3":
        return cls(
            attempt_id=str(payload["attempt_id"]),
            provider=str(payload["provider"]),
            capability=Capability(payload["capability"]),
            outcome=AttemptOutcome(payload["outcome"]),
            retry_count=int(payload.get("retry_count", 0)),
            result_count=int(payload.get("result_count", 0)),
            started_at=payload.get("started_at"),
            duration_ms=payload.get("duration_ms"),
            error=ErrorV3.from_dict(payload["error"]) if payload.get("error") else None,
            skip_reason=SkipReason(payload["skip_reason"])
            if payload.get("skip_reason")
            else None,
            budget_decision=payload.get("budget_decision"),
            circuit_state_before=CircuitState(
                payload.get("circuit_state_before", "unknown")
            ),
            circuit_state_after=CircuitState(
                payload.get("circuit_state_after", "unknown")
            ),
            endpoint_id=str(payload.get("endpoint_id") or ""),
            decision=str(payload.get("decision") or "attempted"),
            tries=[dict(item) for item in payload.get("tries", [])],
        )


_SKIP_REASON_TO_CANDIDATE_REASON = {
    SkipReason.AUTH_BLOCKED: CandidateReasonCode.BLOCKED_AUTH,
    SkipReason.QUOTA_BLOCKED: CandidateReasonCode.BLOCKED_QUOTA,
    SkipReason.CIRCUIT_OPEN: CandidateReasonCode.CIRCUIT_OPEN,
    SkipReason.BUDGET_BLOCKED: CandidateReasonCode.BUDGET_DENIED,
}
_COMPLETED_RECEIPT_FIELDS = {
    "authority",
    "execution_scope",
    "candidate_decisions",
    "cache_origin",
    "shadow_observation",
}
_CANDIDATE_FIELDS = {
    "provider", "position", "decision", "reason_code", "attempt_id",
}
_DECISION_REASONS = {
    CandidateDecision.SELECTED: {
        CandidateReasonCode.CLASSIC_SELECTED,
        CandidateReasonCode.FALLBACK_SELECTED,
    },
    CandidateDecision.ATTEMPTED_FAILED: {CandidateReasonCode.ATTEMPT_FAILED},
    CandidateDecision.ATTEMPTED_NO_SELECTION: {
        CandidateReasonCode.INSUFFICIENT_RESULTS,
    },
    CandidateDecision.SKIPPED: {
        CandidateReasonCode.BLOCKED_AUTH,
        CandidateReasonCode.BLOCKED_QUOTA,
        CandidateReasonCode.CIRCUIT_OPEN,
        CandidateReasonCode.BUDGET_DENIED,
        CandidateReasonCode.PROVIDER_UNAVAILABLE,
    },
    CandidateDecision.NOT_ATTEMPTED: {
        CandidateReasonCode.PROVIDER_UNAVAILABLE,
        CandidateReasonCode.NOT_ATTEMPTED_AFTER_SUCCESS,
    },
    CandidateDecision.ORIGIN_SELECTED: {
        CandidateReasonCode.CACHE_ORIGIN_SELECTED,
    },
}


def _candidate_decision(
    provider: str,
    position: int,
    decision: CandidateDecision,
    reason: CandidateReasonCode,
    attempt_id: str | None,
) -> Dict[str, Any]:
    return {
        "provider": provider,
        "position": position,
        "decision": decision.value,
        "reason_code": reason.value,
        "attempt_id": attempt_id,
    }


def complete_routing_receipt_v3(
    receipt: Dict[str, Any],
    attempts: List[ProviderAttemptV3],
    *,
    shadow_observation: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Complete a classic receipt without changing the authoritative plan."""
    completed = dict(receipt)
    order = [str(provider) for provider in receipt.get("candidate_order") or []]
    selected = receipt.get("selected_provider")
    attempts_by_provider = {item.provider: item for item in attempts}
    decisions = []
    selected_seen = False
    for position, provider in enumerate(order, 1):
        provider_attempt = attempts_by_provider.get(provider)
        if provider == selected:
            reason = (
                CandidateReasonCode.CLASSIC_SELECTED
                if position == 1
                else CandidateReasonCode.FALLBACK_SELECTED
            )
            decisions.append(
                _candidate_decision(
                    provider,
                    position,
                    CandidateDecision.SELECTED,
                    reason,
                    provider_attempt.attempt_id if provider_attempt else None,
                )
            )
            selected_seen = True
        elif provider_attempt and provider_attempt.outcome is AttemptOutcome.SKIPPED:
            reason = (
                _SKIP_REASON_TO_CANDIDATE_REASON.get(
                    provider_attempt.skip_reason,
                    CandidateReasonCode.PROVIDER_UNAVAILABLE,
                )
                if provider_attempt.skip_reason is not None
                else CandidateReasonCode.PROVIDER_UNAVAILABLE
            )
            decisions.append(
                _candidate_decision(
                    provider,
                    position,
                    CandidateDecision.SKIPPED,
                    reason,
                    provider_attempt.attempt_id,
                )
            )
        elif provider_attempt and provider_attempt.outcome in {
            AttemptOutcome.FAILED,
            AttemptOutcome.CANCELLED,
        }:
            decisions.append(
                _candidate_decision(
                    provider,
                    position,
                    CandidateDecision.ATTEMPTED_FAILED,
                    CandidateReasonCode.ATTEMPT_FAILED,
                    provider_attempt.attempt_id,
                )
            )
        elif provider_attempt and provider_attempt.outcome in {
            AttemptOutcome.SUCCESS,
            AttemptOutcome.PARTIAL,
        }:
            decisions.append(
                _candidate_decision(
                    provider,
                    position,
                    CandidateDecision.ATTEMPTED_NO_SELECTION,
                    CandidateReasonCode.INSUFFICIENT_RESULTS,
                    provider_attempt.attempt_id,
                )
            )
            selected_seen = True
        else:
            reason = (
                CandidateReasonCode.NOT_ATTEMPTED_AFTER_SUCCESS
                if selected_seen
                else CandidateReasonCode.PROVIDER_UNAVAILABLE
            )
            decisions.append(
                _candidate_decision(
                    provider,
                    position,
                    CandidateDecision.NOT_ATTEMPTED,
                    reason,
                    None,
                )
            )
    if selected in order and order.index(selected) > 0:
        prior_decisions = decisions[: order.index(selected)]
        if any(
            item["decision"] == CandidateDecision.ATTEMPTED_NO_SELECTION.value
            for item in prior_decisions
        ):
            completed["fallback_reason"] = FallbackReason.INSUFFICIENT_RESULTS.value
        elif any(
            item["decision"] == CandidateDecision.ATTEMPTED_FAILED.value
            for item in prior_decisions
        ):
            completed["fallback_reason"] = FallbackReason.SELECTED_FAILED.value
        elif any(
            item["decision"] == CandidateDecision.SKIPPED.value
            for item in prior_decisions
        ):
            completed["fallback_reason"] = FallbackReason.SELECTED_SKIPPED.value
    completed.update(
        {
            "authority": "classic",
            "execution_scope": "current",
            "candidate_decisions": decisions,
            "cache_origin": None,
            "shadow_observation": shadow_observation,
        }
    )
    validate_routing_receipt_v3(completed, attempts, require_completed=True)
    return completed


def cache_hit_routing_receipt_v3(
    origin_receipt: Dict[str, Any], *, origin_execution_id: str
) -> Dict[str, Any]:
    """Build a neutral current receipt with historical origin evidence nested."""
    origin_decisions = []
    for item in origin_receipt.get("candidate_decisions") or []:
        if item.get("decision") == CandidateDecision.SELECTED.value:
            origin_decisions.append(
                _candidate_decision(
                    str(item["provider"]),
                    int(item["position"]),
                    CandidateDecision.ORIGIN_SELECTED,
                    CandidateReasonCode.CACHE_ORIGIN_SELECTED,
                    None,
                )
            )
    if not origin_decisions and origin_receipt.get("selected_provider"):
        provider = str(origin_receipt["selected_provider"])
        order = list(origin_receipt.get("candidate_order") or [])
        position = order.index(provider) + 1 if provider in order else 1
        origin_decisions.append(
            _candidate_decision(
                provider,
                position,
                CandidateDecision.ORIGIN_SELECTED,
                CandidateReasonCode.CACHE_ORIGIN_SELECTED,
                None,
            )
        )
    receipt = {
        "policy_id": str(origin_receipt.get("policy_id") or "classic"),
        "policy_revision": str(origin_receipt.get("policy_revision") or "v2.9.1"),
        "mode": "classic",
        "candidate_order": [],
        "selected_provider": None,
        "fallback_reason": "none",
        "authority": "classic",
        "execution_scope": "current",
        "candidate_decisions": [],
        "cache_origin": {
            "execution_id": origin_execution_id,
            "policy_id": str(origin_receipt.get("policy_id") or "classic"),
            "policy_revision": str(
                origin_receipt.get("policy_revision") or "v2.9.1"
            ),
            "candidate_order": list(origin_receipt.get("candidate_order") or []),
            "selected_provider": origin_receipt.get("selected_provider"),
            "fallback_reason": str(
                origin_receipt.get("fallback_reason") or "none"
            ),
            "candidate_decisions": origin_decisions,
        },
        "shadow_observation": None,
    }
    validate_routing_receipt_v3(receipt, require_completed=True)
    return receipt


def validate_routing_receipt_v3(
    receipt: Dict[str, Any],
    attempts: List[ProviderAttemptV3] | None = None,
    *,
    require_completed: bool = False,
) -> None:
    base_fields = {
        "policy_id", "policy_revision", "mode", "candidate_order",
        "selected_provider", "fallback_reason",
    }
    if not base_fields.issubset(receipt):
        raise ValueError("routing_receipt is missing frozen required fields")
    present = _COMPLETED_RECEIPT_FIELDS.intersection(receipt)
    if not present and not require_completed:
        return
    if present != _COMPLETED_RECEIPT_FIELDS:
        raise ValueError("routing_receipt completion fields must be all-or-none")
    if receipt["authority"] != "classic" or receipt["execution_scope"] != "current":
        raise ValueError("routing receipt authority/scope is invalid")
    decisions = receipt["candidate_decisions"]
    if not isinstance(decisions, list):
        raise ValueError("candidate_decisions must be an array")
    order = receipt["candidate_order"]
    if not isinstance(order, list) or len(order) != len(set(order)):
        raise ValueError("candidate_order must be a unique array")
    if len(decisions) != len(order):
        raise ValueError("candidate decisions must cover every candidate")
    try:
        FallbackReason(receipt["fallback_reason"])
    except (TypeError, ValueError) as exc:
        raise ValueError("fallback_reason is invalid") from exc
    attempt_map = {item.attempt_id: item for item in attempts or []}
    selected_count = 0
    for index, item in enumerate(decisions, 1):
        if not isinstance(item, dict) or set(item) != _CANDIDATE_FIELDS:
            raise ValueError("candidate decision fields are invalid")
        decision = CandidateDecision(item["decision"])
        reason = CandidateReasonCode(item["reason_code"])
        if reason not in _DECISION_REASONS[decision]:
            raise ValueError("candidate decision/reason combination is invalid")
        if item["position"] != index or item["provider"] != order[index - 1]:
            raise ValueError("candidate decisions must follow stable candidate order")
        if decision is CandidateDecision.ORIGIN_SELECTED:
            raise ValueError("origin_selected is forbidden in current decisions")
        if decision is CandidateDecision.SELECTED:
            selected_count += 1
            if item["provider"] != receipt["selected_provider"]:
                raise ValueError("selected decision/provider mismatch")
        attempt_id = item["attempt_id"]
        if decision in {
            CandidateDecision.ATTEMPTED_FAILED,
            CandidateDecision.ATTEMPTED_NO_SELECTION,
            CandidateDecision.SKIPPED,
        } and attempt_id is None:
            raise ValueError("executed/skipped candidate decision requires attempt_id")
        if decision is CandidateDecision.NOT_ATTEMPTED and attempt_id is not None:
            raise ValueError("not_attempted candidate cannot reference an attempt")
        if attempt_id is not None and attempts is not None:
            provider_attempt = attempt_map.get(attempt_id)
            if provider_attempt is None or provider_attempt.provider != item["provider"]:
                raise ValueError("candidate decision references invalid current attempt")
            expected_outcomes = {
                CandidateDecision.SELECTED: {
                    AttemptOutcome.SUCCESS,
                    AttemptOutcome.PARTIAL,
                },
                CandidateDecision.ATTEMPTED_FAILED: {
                    AttemptOutcome.FAILED,
                    AttemptOutcome.CANCELLED,
                },
                CandidateDecision.ATTEMPTED_NO_SELECTION: {
                    AttemptOutcome.SUCCESS,
                    AttemptOutcome.PARTIAL,
                },
                CandidateDecision.SKIPPED: {AttemptOutcome.SKIPPED},
            }
            if (
                decision in expected_outcomes
                and provider_attempt.outcome not in expected_outcomes[decision]
            ):
                raise ValueError("candidate decision contradicts attempt outcome")
            if decision is CandidateDecision.SKIPPED:
                expected_skip_reason = (
                    _SKIP_REASON_TO_CANDIDATE_REASON.get(
                        provider_attempt.skip_reason,
                        CandidateReasonCode.PROVIDER_UNAVAILABLE,
                    )
                    if provider_attempt.skip_reason is not None
                    else CandidateReasonCode.PROVIDER_UNAVAILABLE
                )
                if reason is not expected_skip_reason:
                    raise ValueError("candidate reason contradicts skip reason")
    fallback = FallbackReason(receipt["fallback_reason"])
    selected_positions = [
        item["position"]
        for item in decisions
        if item["decision"] == CandidateDecision.SELECTED.value
    ]
    if selected_positions:
        selected_position = selected_positions[0]
        prior_decisions = decisions[: selected_position - 1]
        if selected_position == 1 and fallback is not FallbackReason.NONE:
            raise ValueError("direct selection cannot claim fallback")
        if selected_position > 1 and fallback is FallbackReason.NONE:
            raise ValueError("fallback selection requires fallback_reason")
        if (
            fallback is FallbackReason.SELECTED_FAILED
            and not any(
                item["decision"] == CandidateDecision.ATTEMPTED_FAILED.value
                for item in prior_decisions
            )
        ):
            raise ValueError("selected_failed requires a failed prior candidate")
        if (
            fallback is FallbackReason.INSUFFICIENT_RESULTS
            and not any(
                item["decision"]
                == CandidateDecision.ATTEMPTED_NO_SELECTION.value
                for item in prior_decisions
            )
        ):
            raise ValueError(
                "insufficient_results requires a successful unselected candidate"
            )
        if (
            fallback is FallbackReason.SELECTED_SKIPPED
            and not any(
                item["decision"] == CandidateDecision.SKIPPED.value
                for item in prior_decisions
            )
        ):
            raise ValueError("selected_skipped requires a skipped prior candidate")
    if receipt["selected_provider"] is not None and selected_count != 1:
        raise ValueError("completed receipt requires exactly one selected decision")
    origin = receipt["cache_origin"]
    if origin is not None:
        required_origin = {
            "execution_id", "policy_id", "policy_revision", "candidate_order",
            "selected_provider", "fallback_reason", "candidate_decisions",
        }
        if not isinstance(origin, dict) or set(origin) != required_origin:
            raise ValueError("cache_origin fields are invalid")
        if receipt["candidate_order"] or receipt["selected_provider"] is not None or decisions:
            raise ValueError("cache hit cannot invent current routing decisions")
        try:
            FallbackReason(origin["fallback_reason"])
        except (TypeError, ValueError) as exc:
            raise ValueError("cache_origin fallback_reason is invalid") from exc
        origin_order = origin["candidate_order"]
        if not isinstance(origin_order, list) or len(origin_order) != len(set(origin_order)):
            raise ValueError("cache_origin candidate_order must be unique")
        origin_selected_count = 0
        for item in origin["candidate_decisions"]:
            if not isinstance(item, dict) or set(item) != _CANDIDATE_FIELDS:
                raise ValueError("cache_origin decision fields are invalid")
            position = item.get("position")
            if (
                not isinstance(position, int)
                or isinstance(position, bool)
                or position < 1
                or position > len(origin_order)
                or item.get("provider") != origin_order[position - 1]
                or item.get("provider") != origin["selected_provider"]
                or item.get("decision") != CandidateDecision.ORIGIN_SELECTED.value
                or item.get("reason_code")
                != CandidateReasonCode.CACHE_ORIGIN_SELECTED.value
                or item.get("attempt_id") is not None
            ):
                raise ValueError("cache_origin decision is invalid")
            origin_selected_count += 1
        expected_origin_selected = 1 if origin["selected_provider"] is not None else 0
        if origin_selected_count != expected_origin_selected:
            raise ValueError("cache_origin selected decision/provider mismatch")
    shadow = receipt["shadow_observation"]
    if shadow is not None:
        shadow_fields = {
            "observed", "policy_id", "policy_revision", "selected_provider",
            "affected_execution",
        }
        if (
            not isinstance(shadow, dict)
            or set(shadow) != shadow_fields
            or shadow["observed"] is not True
            or shadow["affected_execution"] is not False
        ):
            raise ValueError("shadow observation must be typed and observational")


@dataclass(frozen=True)
class RequestV3:
    capability: Capability
    input: Dict[str, Any]
    request_id: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    cache: Dict[str, Any] = field(default_factory=dict)
    routing: Dict[str, Any] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)
    client: Dict[str, Any] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract_version: {self.contract_version}")
        if self.capability is Capability.SEARCH:
            query = self.input.get("query")
            if not isinstance(query, str) or not query.strip() or "urls" in self.input:
                raise ValueError(
                    "search input requires non-empty query and forbids urls"
                )
        elif self.capability is Capability.EXTRACT:
            urls = self.input.get("urls")
            if (
                not isinstance(urls, list)
                or not urls
                or not all(isinstance(url, str) and url for url in urls)
                or "query" in self.input
            ):
                raise ValueError(
                    "extract input requires non-empty urls and forbids query"
                )
            for option_name in ("max_urls", "max_context_chars"):
                option_value = self.options.get(option_name)
                if option_value is not None and (
                    isinstance(option_value, bool) or not isinstance(option_value, int)
                ):
                    raise ValueError(f"{option_name} must be an integer")

    @classmethod
    def search(
        cls,
        query: str,
        *,
        request_id: Optional[str] = None,
        max_results: int = 5,
        freshness: Optional[str] = None,
        accept_features: Optional[List[str]] = None,
    ) -> "RequestV3":
        options: Dict[str, Any] = {"max_results": max_results}
        if freshness is not None:
            options["freshness"] = freshness
        client = {
            "accept_contract_versions": [CONTRACT_VERSION],
            "accept_features": list(accept_features or []),
        }
        return cls(
            Capability.SEARCH,
            {"query": query},
            request_id=request_id,
            options=options,
            client=client,
        )

    @classmethod
    def extract(
        cls,
        urls: List[str],
        *,
        request_id: Optional[str] = None,
        output_format: str = "markdown",
        include_images: bool = False,
        max_urls: Optional[int] = None,
        max_context_chars: Optional[int] = None,
    ) -> "RequestV3":
        options: Dict[str, Any] = {
            "output_format": output_format,
            "include_images": include_images,
        }
        if max_urls is not None:
            options["max_urls"] = max_urls
        if max_context_chars is not None:
            options["max_context_chars"] = max_context_chars
        return cls(
            Capability.EXTRACT,
            {"urls": list(urls)},
            request_id=request_id,
            options=options,
            client={
                "accept_contract_versions": [CONTRACT_VERSION],
                "accept_features": [],
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "capability": self.capability,
            "input": self.input,
            "options": self.options,
            "cache": self.cache,
            "routing": self.routing,
            "budget": self.budget,
            "client": self.client,
        }
        return {
            key: _plain(value)
            for key, value in payload.items()
            if value not in (None, {})
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RequestV3":
        return cls(
            capability=Capability(payload["capability"]),
            input=dict(payload["input"]),
            request_id=payload.get("request_id"),
            options=dict(payload.get("options") or {}),
            cache=dict(payload.get("cache") or {}),
            routing=dict(payload.get("routing") or {}),
            budget=dict(payload.get("budget") or {}),
            client=dict(payload.get("client") or {}),
            contract_version=str(payload.get("contract_version", "")),
        )


_BANNED_CANONICAL_FIELDS = {
    "answer",
    "full_synthesis",
    "claim",
    "verification",
    "truth_confidence",
}
_ALLOWED_TRANSFORMATIONS = {
    "whitespace_norm",
    "deterministic_truncation",
    "mechanical_segmentation",
    "image_base64_replace",
}
_DIVERSITY_FIELDS = {
    "method",
    "method_version",
    "method_degraded",
    "provider_count",
    "host_count",
    "source_family_count",
    "unique_cluster_count",
}
_POLICY_ACTION_REASONS = {
    "excluded": {"spam_domain"},
    "reranked": {"intent_authority"},
    "demoted": {"domain_diversity"},
    "selected_as_representative": {"dedup_representative"},
    "truncated_by_limit": {
        "max_results", "max_content_bytes", "max_context_chars",
    },
}
_BASE64_IMAGE_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+")


def _validate_no_banned_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _BANNED_CANONICAL_FIELDS:
                raise ValueError(f"banned canonical field: {key}")
            if key == "score":
                raise ValueError("bare score is banned; use typed provider_score")
            if key == "type" and child == "synthesis":
                raise ValueError("banned canonical type: synthesis")
            _validate_no_banned_fields(child)
    elif isinstance(value, list):
        for child in value:
            _validate_no_banned_fields(child)


def _validate_provider_fields(observation: Dict[str, Any]) -> None:
    fields = observation.get("provider_fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("provider_fields must be an object")
    if len(json.dumps(fields, ensure_ascii=False).encode("utf-8")) > 4096:
        raise ValueError("provider_fields exceeds 4096 UTF-8 bytes")
    provider = observation.get("provider")
    if fields and set(fields) != {provider}:
        raise ValueError("provider_fields must be scoped to the observation provider")
    if not fields:
        return
    from provider_registry import PROVIDER_SPECS

    spec = PROVIDER_SPECS.get(str(provider))
    allowed = set(spec.provider_fields_allowlist if spec else ())
    supplied = set(fields[provider]) if isinstance(fields[provider], dict) else set()
    if not isinstance(fields[provider], dict) or not supplied <= allowed:
        raise ValueError("provider_fields contains non-allowlisted fields")


def _transformed_text(raw: str, projected: str, transformations: List[str]) -> bool:
    current = raw
    for transformation in transformations:
        if transformation == "whitespace_norm":
            current = " ".join(current.split())
        elif transformation == "deterministic_truncation":
            if not current.startswith(projected):
                return False
            current = projected
        elif transformation == "mechanical_segmentation":
            pass
        elif transformation == "image_base64_replace":
            current = _BASE64_IMAGE_RE.sub("[inline image removed]", current)
        else:
            return False
    return current == projected


def _validate_projected_text(
    projected: Dict[str, Any], observations: Dict[str, Dict[str, Any]]
) -> None:
    provenance = projected.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("projected content requires provenance")
    observation_id = provenance.get("observation_id")
    if observation_id not in observations:
        raise ValueError("projected content references missing observation")
    source_field = provenance.get("source_field")
    if source_field not in {"title", "snippet", "text"}:
        raise ValueError("projected content has invalid source_field")
    raw = observations[observation_id].get(source_field)
    text = projected.get("text")
    transformations = provenance.get("transformations")
    if not isinstance(raw, str) or not isinstance(text, str):
        raise ValueError("single-source content requires string source and text")
    if not isinstance(transformations, list) or not set(transformations) <= _ALLOWED_TRANSFORMATIONS:
        raise ValueError("projected content has invalid transformation")
    if not _transformed_text(raw, text, transformations):
        raise ValueError("single-source transformation does not match observation")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if projected.get("text_sha256") != digest:
        raise ValueError("projected content sha256 mismatch")
    segments = projected.get("segments") or []
    offset = 0
    rebuilt = []
    for segment in segments:
        start, end = segment.get("start"), segment.get("end")
        if start != offset or not isinstance(end, int) or end <= start:
            raise ValueError("segments must be contiguous non-empty codepoint ranges")
        part = text[start:end]
        if segment.get("text") != part:
            raise ValueError("segment text does not match codepoint offsets")
        rebuilt.append(part)
        offset = end
    if text and (offset != len(text) or "".join(rebuilt) != text):
        raise ValueError("segments must cover projected text exactly")
    if not text and segments:
        raise ValueError("empty projected text must have no segments")


def _default_diversity() -> Dict[str, Any]:
    return {
        "method": "component_count",
        "method_version": "1",
        "method_degraded": False,
        "provider_count": 0,
        "host_count": 0,
        "source_family_count": 0,
        "unique_cluster_count": 0,
    }


@dataclass(frozen=True)
class ResponseV3:
    request_id: str
    capability: Capability
    status: ResponseStatus
    results: List[Dict[str, Any]]
    provider_attempts: List[ProviderAttemptV3]
    routing_receipt: Dict[str, Any]
    cache_status: Dict[str, Any]
    execution_id: str = field(default_factory=lambda: f"exec_{uuid.uuid4().hex}")
    observations: List[Dict[str, Any]] = field(default_factory=list)
    policy_actions: List[Dict[str, Any]] = field(default_factory=list)
    source_diversity: Dict[str, Any] = field(default_factory=_default_diversity)
    engine: Optional[Dict[str, str]] = None
    limits_applied: Dict[str, Any] = field(default_factory=dict)
    stored_content: List[Dict[str, Any]] = field(default_factory=list)
    dedup_clusters: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[ErrorV3] = None
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract_version: {self.contract_version}")
        if not self.execution_id:
            raise ValueError("execution_id is required")
        if self.engine is not None:
            if set(self.engine) != {"name", "version", "build_commit"}:
                raise ValueError("engine must contain name, version, and build_commit")
            if not all(isinstance(value, str) and value for value in self.engine.values()):
                raise ValueError("engine fields must be non-empty strings")
        validate_routing_receipt_v3(
            self.routing_receipt,
            self.provider_attempts,
            require_completed=False,
        )
        _validate_no_banned_fields(self.results)
        _validate_no_banned_fields(self.observations)
        if set(self.source_diversity) != _DIVERSITY_FIELDS:
            extras = set(self.source_diversity) - _DIVERSITY_FIELDS
            if extras:
                raise ValueError("source_diversity scalar or additional field is banned in 3.0")
            raise ValueError("source_diversity is missing required components")
        observations = {}
        for observation in self.observations:
            observation_id = observation.get("observation_id")
            if not isinstance(observation_id, str) or not observation_id.startswith("obs_"):
                raise ValueError("observation_id must use obs_ prefix")
            if observation_id in observations:
                raise ValueError("duplicate observation_id")
            observations[observation_id] = observation
            _validate_provider_fields(observation)
        for result in self.results:
            representative = result.get("representative_observation_id")
            members = result.get("observation_ids")
            if representative not in observations:
                raise ValueError("result references missing representative observation")
            if not isinstance(members, list) or not members or representative not in members:
                raise ValueError("result observation_ids must include representative")
            if any(member not in observations for member in members):
                raise ValueError("result references missing observation")
            for field_name in ("title", "snippet", "text"):
                projected = result.get(field_name)
                if projected is not None:
                    if not isinstance(projected, dict):
                        raise ValueError("content-bearing result fields must be projected objects")
                    _validate_projected_text(projected, observations)
        for action in self.policy_actions:
            reasons = _POLICY_ACTION_REASONS.get(str(action.get("action")))
            if reasons is None or action.get("reason") not in reasons:
                raise ValueError("invalid policy action/reason combination")
            if action.get("observation_id") not in observations:
                raise ValueError("policy action references missing observation")
        extract_limits = self.limits_applied.get("extract")
        if extract_limits is not None:
            required_limit_fields = {
                "requested_url_count", "processed_urls", "omitted_urls",
                "omitted_url_count", "max_urls", "max_context_chars",
                "context_chars_returned", "truncated",
            }
            if not isinstance(extract_limits, dict) or set(extract_limits) != required_limit_fields:
                raise ValueError("limits_applied.extract has invalid fields")
            processed_urls = extract_limits["processed_urls"]
            omitted_urls = extract_limits["omitted_urls"]
            if not isinstance(processed_urls, list) or not all(
                isinstance(url, str) and url for url in processed_urls
            ):
                raise ValueError("processed_urls must be non-empty URL strings")
            if not isinstance(omitted_urls, list) or not all(
                isinstance(url, str) and url for url in omitted_urls
            ):
                raise ValueError("omitted_urls must be URL strings")
            if extract_limits["omitted_url_count"] != len(omitted_urls):
                raise ValueError("omitted_url_count mismatch")
            if extract_limits["requested_url_count"] != len(processed_urls) + len(omitted_urls):
                raise ValueError("requested_url_count mismatch")
            for name in (
                "requested_url_count", "omitted_url_count", "max_urls",
                "max_context_chars", "context_chars_returned",
            ):
                value = extract_limits[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(
                        f"limits_applied.extract.{name} must be non-negative integer"
                    )
            if not isinstance(extract_limits["truncated"], bool):
                raise ValueError("limits_applied.extract.truncated must be boolean")
        stored_observations = set()
        required_stored_fields = {
            "observation_id", "storage_attempted", "storage_succeeded",
            "reference", "full_text_sha256", "full_text_chars",
        }
        for stored in self.stored_content:
            if not isinstance(stored, dict) or set(stored) != required_stored_fields:
                raise ValueError("stored_content has invalid fields")
            observation_id = stored["observation_id"]
            if observation_id not in observations or observation_id in stored_observations:
                raise ValueError("stored_content observation reference is invalid")
            stored_observations.add(observation_id)
            if stored["storage_attempted"] is not True or not isinstance(
                stored["storage_succeeded"], bool
            ):
                raise ValueError("stored_content storage flags are invalid")
            if stored["storage_succeeded"]:
                reference = stored["reference"]
                if (
                    not isinstance(reference, dict)
                    or set(reference) != {"store", "key", "media_type"}
                    or reference["store"] != "web_text_v3"
                    or reference["media_type"] != "text/markdown"
                    or not re.fullmatch(r"[a-f0-9]{64}", str(reference["key"]))
                    or not re.fullmatch(r"[a-f0-9]{64}", str(stored["full_text_sha256"]))
                    or isinstance(stored["full_text_chars"], bool)
                    or not isinstance(stored["full_text_chars"], int)
                    or stored["full_text_chars"] < 0
                ):
                    raise ValueError("successful stored_content metadata is invalid")
            elif any(
                stored[field] is not None
                for field in ("reference", "full_text_sha256", "full_text_chars")
            ):
                raise ValueError(
                    "failed stored_content must not expose reference metadata"
                )
        if self.status is ResponseStatus.DEGRADED:
            accepted_codes = {reason.value for reason in DegradedReason}
            warning_codes = {
                warning.get("code") for warning in self.warnings if isinstance(warning, dict)
            }
            if not accepted_codes.intersection(warning_codes):
                raise ValueError("degraded response requires an enumerated degrade warning")
        if self.status is ResponseStatus.FAILED and self.error is None:
            raise ValueError("failed response requires error")
        if self.status is not ResponseStatus.FAILED and self.error is not None:
            raise ValueError("top-level error is reserved for failed responses")

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "execution_id": self.execution_id,
            "capability": self.capability,
            "status": self.status,
            "results": self.results,
            "observations": self.observations,
            "policy_actions": self.policy_actions,
            "source_diversity": self.source_diversity,
            "engine": self.engine,
            "provider_attempts": [attempt.to_dict() for attempt in self.provider_attempts],
            "routing_receipt": self.routing_receipt,
            "cache_status": self.cache_status,
            "limits_applied": self.limits_applied,
            "stored_content": self.stored_content,
            "dedup_clusters": self.dedup_clusters,
            "warnings": self.warnings,
            "error": self.error,
        }
        required_empty_fields = {
            "results", "observations", "policy_actions", "provider_attempts",
            "routing_receipt", "cache_status", "limits_applied", "dedup_clusters",
            "stored_content", "warnings",
        }
        return {
            key: _wire_plain(value)
            for key, value in payload.items()
            if value not in (None, {}, []) or key in required_empty_fields
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ResponseV3":
        if "source_independence_estimate" in payload:
            raise ValueError("source_independence_estimate is banned by Amendment 002")
        return cls(
            request_id=str(payload["request_id"]),
            execution_id=str(payload["execution_id"]),
            capability=Capability(payload["capability"]),
            status=ResponseStatus(payload["status"]),
            results=[dict(item) for item in payload.get("results", [])],
            observations=[dict(item) for item in payload.get("observations", [])],
            policy_actions=[dict(item) for item in payload.get("policy_actions", [])],
            source_diversity=dict(payload["source_diversity"]),
            engine=dict(payload["engine"]) if payload.get("engine") is not None else None,
            provider_attempts=[ProviderAttemptV3.from_dict(item) for item in payload.get("provider_attempts", [])],
            routing_receipt=dict(payload["routing_receipt"]),
            cache_status=dict(payload["cache_status"]),
            limits_applied=dict(payload.get("limits_applied") or {}),
            stored_content=[dict(item) for item in payload.get("stored_content", [])],
            dedup_clusters=[dict(item) for item in payload.get("dedup_clusters", [])],
            warnings=[dict(item) for item in payload.get("warnings", [])],
            error=ErrorV3.from_dict(payload["error"]) if payload.get("error") else None,
            contract_version=str(payload.get("contract_version", "")),
        )
