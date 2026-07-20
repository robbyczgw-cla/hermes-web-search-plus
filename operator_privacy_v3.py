"""Single fail-closed privacy boundary for WS-3 operator payloads."""

from __future__ import annotations

import re
from typing import Any, Iterable

from provider_registry import PROVIDER_SPECS


FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "query",
        "url",
        "urls",
        "title",
        "text",
        "snippet",
        "content",
        "fulltext",
        "full_text",
        "api_key",
        "secret",
        "token",
        "authorization",
        "headers",
        "credential_fingerprint",
        "credential_slot",
        "endpoint_url",
        "path",
        "file",
        "filename",
        "cache_dir",
        "state_path",
    }
)
_FORBIDDEN_COMPACT_FIELD_NAMES = frozenset(
    field.replace("_", "") for field in FORBIDDEN_FIELD_NAMES
)
_ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")
_AUTHORIZATION_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+")
_EXECUTION_ID = re.compile(
    r"^(?:exec_[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})$"
)
_ATTEMPT_ID = re.compile(r"^attempt_[a-f0-9]{16}$")
_ENTRY_ID = re.compile(r"^(?:search|extract)_[a-f0-9]{32}$")
_WSP_CODE = re.compile(r"^wsp\.[a-z0-9_.-]{1,96}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")
_ENUM_VALUES = {
    "status": {
        "ok", "degraded", "failed", "completed", "collected", "not_collected",
    },
    "capability": {"search", "extract"},
    "capabilities": {"search", "extract"},
    "kind": {"search", "extract"},
    "kinds": {"search", "extract"},
    "mode": {"classic", "shadow"},
    "authority": {"classic"},
    "execution_scope": {"current"},
    "decision": {
        "selected",
        "attempted_failed",
        "attempted_no_selection",
        "skipped",
        "not_attempted",
        "origin_selected",
    },
    "reason_code": {
        "classic_selected",
        "fallback_selected",
        "attempt_failed",
        "insufficient_results",
        "blocked_auth",
        "blocked_quota",
        "circuit_open",
        "budget_denied",
        "provider_unavailable",
        "not_attempted_after_success",
        "cache_origin_selected",
    },
    "fallback_reason": {
        "none", "selected_failed", "selected_skipped", "insufficient_results",
        "partial_content", "budget_chain", "provider_error",
    },
    "disposition": {
        "fresh_hit", "stale_hit", "miss", "bypassed", "unavailable",
    },
    "search": {"collected", "not_collected"},
    "extract": {"collected", "not_collected"},
    "action": {
        "excluded", "reranked", "demoted", "selected_as_representative",
        "truncated_by_limit", "budget_preflight", "degrade", "abort",
    },
    "reason": {
        "spam_domain", "intent_authority", "domain_diversity",
        "dedup_representative", "max_results", "max_content_bytes",
        "max_context_chars", "degraded", "aborted",
        "daily_quota_exhausted", "budget_ledger_unavailable",
        "budget_unsatisfiable",
    },
    "check": {
        "provider_call_cap", "daily_quota", "timeout_budget", "context_budget",
    },
    "verdict": {"ok", "exceeded"},
}
_PROVIDER_FIELDS = {
    "provider",
    "selected_provider",
    "shadow_provider",
    "classic_provider",
    "recommended_priority",
    "candidate_order",
}
_EXECUTION_FIELDS = {"execution_id", "origin_execution_id"}
_ATTEMPT_FIELDS = {"attempt_id", "current_provider_attempts"}


def _normalize_key(key: object) -> str:
    return str(key).lower().replace("-", "_")


def _validate_string(value: str, field_name: str | None, location: str) -> None:
    if _ABSOLUTE_PATH.match(value):
        raise ValueError(f"operator payload leaks absolute path at {location}")
    if _AUTHORIZATION_VALUE.search(value) or re.match(
        r"^[a-z]+://[^/@]+@", value, re.IGNORECASE
    ):
        raise ValueError(f"operator payload leaks authorization material at {location}")
    if field_name == "display_name":
        if not any(value == spec.display_name for spec in PROVIDER_SPECS.values()):
            raise ValueError(f"operator payload string is not known-safe at {location}")
        return
    if field_name in _ENUM_VALUES:
        if value not in _ENUM_VALUES[field_name]:
            raise ValueError(f"operator payload string is not known-safe at {location}")
        return
    if field_name in _PROVIDER_FIELDS:
        if value != "auto" and value not in PROVIDER_SPECS:
            raise ValueError(f"operator payload string lacks provider provenance at {location}")
        return
    if field_name in _EXECUTION_FIELDS:
        if not _EXECUTION_ID.fullmatch(value):
            raise ValueError(f"operator payload string lacks execution provenance at {location}")
        return
    if field_name in _ATTEMPT_FIELDS:
        if not _ATTEMPT_ID.fullmatch(value):
            raise ValueError(f"operator payload string lacks attempt provenance at {location}")
        return
    if field_name == "entry_id" and _ENTRY_ID.fullmatch(value):
        return
    if field_name in {"warning_codes", "error_code"} and _WSP_CODE.fullmatch(value):
        return
    if field_name == "contract_version" and value == "3.0":
        return
    if field_name == "plugin_version" and _SEMVER.fullmatch(value):
        return
    if field_name == "policy_id" and value in {
        "classic", "shadow-quality", "shadow-interface",
    }:
        return
    if field_name == "policy_revision" and value in {
        "v2.9.1", "routing-v2", "fixture", "1", "3.0", "3.1",
    }:
        return
    raise ValueError(f"operator payload string is not known-safe at {location}")


def _walk(value: Any, *, field_name: str | None, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalize_key(key)
            if (
                normalized in FORBIDDEN_FIELD_NAMES
                or normalized.replace("_", "") in _FORBIDDEN_COMPACT_FIELD_NAMES
            ):
                raise ValueError(f"forbidden operator field at {location}.{key}")
            _walk(child, field_name=normalized, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, field_name=field_name, location=f"{location}[{index}]")
    elif isinstance(value, str):
        _validate_string(value, field_name, location)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError(f"unsupported operator payload value at {location}")


def assert_operator_payload_safe(
    payload: Any, *, configured_secrets: Iterable[str] = ()
) -> None:
    """Reject payloads not built exclusively from known-safe operator values."""
    _walk(payload, field_name=None, location="$")
    serialized = repr(payload)
    for secret in configured_secrets:
        if secret and secret in serialized:
            raise ValueError("forbidden configured secret in operator payload")
