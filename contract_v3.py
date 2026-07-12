"""Frozen Web Search Plus v3 contract DTOs.

This module is deliberately provider- and policy-agnostic. It defines the
wire-level request/response vocabulary used by the engine, compatibility
projections, golden fixtures, and external adapters.
"""

from __future__ import annotations

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

    def to_dict(self) -> Dict[str, Any]:
        payload = _plain(self.__dict__)
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
        )


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
    ) -> "RequestV3":
        return cls(
            Capability.EXTRACT,
            {"urls": list(urls)},
            request_id=request_id,
            options={"output_format": output_format, "include_images": include_images},
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


@dataclass(frozen=True)
class ResponseV3:
    request_id: str
    capability: Capability
    status: ResponseStatus
    results: List[Dict[str, Any]]
    provider_attempts: List[ProviderAttemptV3]
    routing_receipt: Dict[str, Any]
    cache_status: Dict[str, Any]
    limits_applied: Dict[str, Any] = field(default_factory=dict)
    dedup_clusters: List[Dict[str, Any]] = field(default_factory=list)
    source_independence_estimate: Optional[Dict[str, Any]] = None
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[ErrorV3] = None
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported contract_version: {self.contract_version}")
        required_receipt_fields = {
            "policy_id",
            "policy_revision",
            "mode",
            "candidate_order",
            "selected_provider",
            "fallback_reason",
        }
        if not required_receipt_fields.issubset(self.routing_receipt):
            raise ValueError("routing_receipt is missing frozen required fields")
        if self.status is ResponseStatus.DEGRADED:
            accepted_codes = {reason.value for reason in DegradedReason}
            warning_codes = {
                warning.get("code")
                for warning in self.warnings
                if isinstance(warning, dict)
            }
            if not accepted_codes.intersection(warning_codes):
                raise ValueError(
                    "degraded response requires an enumerated degrade warning"
                )
        if self.status is ResponseStatus.FAILED and self.error is None:
            raise ValueError("failed response requires error")
        if self.status is not ResponseStatus.FAILED and self.error is not None:
            raise ValueError("top-level error is reserved for failed responses")

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "request_id": self.request_id,
            "capability": self.capability,
            "status": self.status,
            "results": self.results,
            "provider_attempts": [
                attempt.to_dict() for attempt in self.provider_attempts
            ],
            "routing_receipt": self.routing_receipt,
            "cache_status": self.cache_status,
            "limits_applied": self.limits_applied,
            "dedup_clusters": self.dedup_clusters,
            "source_independence_estimate": self.source_independence_estimate,
            "warnings": self.warnings,
            "error": self.error,
        }
        required_empty_fields = {
            "results",
            "provider_attempts",
            "routing_receipt",
            "cache_status",
            "limits_applied",
            "dedup_clusters",
            "warnings",
        }
        return {
            key: _plain(value)
            for key, value in payload.items()
            if value not in (None, {}, []) or key in required_empty_fields
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ResponseV3":
        return cls(
            request_id=str(payload["request_id"]),
            capability=Capability(payload["capability"]),
            status=ResponseStatus(payload["status"]),
            results=[dict(item) for item in payload.get("results", [])],
            provider_attempts=[
                ProviderAttemptV3.from_dict(item)
                for item in payload.get("provider_attempts", [])
            ],
            routing_receipt=dict(payload["routing_receipt"]),
            cache_status=dict(payload["cache_status"]),
            limits_applied=dict(payload.get("limits_applied") or {}),
            dedup_clusters=[dict(item) for item in payload.get("dedup_clusters", [])],
            source_independence_estimate=(
                dict(payload["source_independence_estimate"])
                if payload.get("source_independence_estimate") is not None
                else None
            ),
            warnings=[dict(item) for item in payload.get("warnings", [])],
            error=ErrorV3.from_dict(payload["error"]) if payload.get("error") else None,
            contract_version=str(payload.get("contract_version", "")),
        )
