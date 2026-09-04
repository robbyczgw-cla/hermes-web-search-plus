"""Pure, fail-closed budget decisions made before v3 provider execution."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Mapping, Sequence


def daily_preflight_budget(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the optional global provider-call ledger settings for attempts."""
    raw_off = os.environ.get("WSP_BUDGET_PREFLIGHT_OFF")
    if raw_off is not None and raw_off.strip().strip('"').strip("'").lower() not in {
        "", "0", "false", "no", "off",
    }:
        return {}
    section = config.get("budget_preflight", {})
    if not isinstance(section, dict):
        raise ValueError("budget_preflight must be an object")
    limit = section.get("max_daily_provider_calls")
    if section.get("enabled") is not True or isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return {}
    return {
        "daily_budget_scope": "daily_provider_calls",
        "daily_budget_window": time.strftime("%Y-%m-%d", time.gmtime()),
        "daily_budget_limit_units": limit,
    }


CHECK_NAMES = (
    "provider_call_cap",
    "daily_quota",
    "timeout_budget",
    "context_budget",
)
_ACTIONS = {"proceed", "degrade", "abort"}
_ON_EXCEED = {"degrade", "abort"}
_ABORT_REASONS = {
    "daily_quota_exhausted",
    "budget_ledger_unavailable",
    "budget_unsatisfiable",
}


@dataclass(frozen=True)
class PreflightCheck:
    """One typed, deterministic preflight comparison."""

    check: str
    limit: int | None
    observed: int | None
    verdict: Literal["ok", "exceeded"]

    def __post_init__(self) -> None:
        if self.check not in CHECK_NAMES:
            raise ValueError("unknown preflight check")
        if self.verdict not in {"ok", "exceeded"}:
            raise ValueError("invalid preflight verdict")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "limit": self.limit,
            "observed": self.observed,
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class PreflightDecision:
    """The complete, provider-free result of budget preflight."""

    action: Literal["proceed", "degrade", "abort"]
    checks: tuple[PreflightCheck, ...] = ()
    adjustments: Dict[str, int] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("invalid preflight action")
        if self.action == "degrade" and not self.adjustments:
            raise ValueError("degrade preflight requires adjustments")
        if self.action != "degrade" and self.adjustments:
            raise ValueError("only degrade preflight may carry adjustments")
        if self.action == "abort" and self.reason not in _ABORT_REASONS:
            raise ValueError("abort preflight requires a typed reason")
        if self.action != "abort" and self.reason is not None:
            raise ValueError("only abort preflight may carry a reason")

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "action": self.action,
            "checks": [check.to_dict() for check in self.checks],
        }
        if self.adjustments:
            payload["adjustments"] = dict(self.adjustments)
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get("budget_preflight") or {}
    return section if isinstance(section, Mapping) else {}


def _limit(section: Mapping[str, Any], name: str) -> int | None:
    value = section.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"budget_preflight.{name} must be a positive integer or null")
    return value


def _planned_provider_calls(request: Any, plan: Any) -> int:
    candidates = tuple(getattr(plan, "candidate_order", ()) or ())
    requested = getattr(request, "budget", {}).get("max_provider_attempts")
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        return len(candidates)
    return min(len(candidates), requested)


def _request_timeout_seconds(request: Any) -> int | None:
    budget = getattr(request, "budget", {}) or {}
    wall_time = budget.get("max_wall_time_ms")
    if isinstance(wall_time, int) and not isinstance(wall_time, bool) and wall_time > 0:
        return (wall_time + 999) // 1000
    options = getattr(request, "options", {}) or {}
    if str(options.get("mode") or "normal") == "research":
        research_timeout = options.get("research_time_budget", 55)
        if isinstance(research_timeout, (int, float)) and not isinstance(
            research_timeout, bool
        ) and research_timeout > 0:
            whole = int(research_timeout)
            return whole if research_timeout == whole else whole + 1
    return None


def _context_limit(request: Any, config: Mapping[str, Any]) -> int:
    capability = str(getattr(request, "capability", ""))
    if capability != "extract":
        return 0
    options = getattr(request, "options", {}) or {}
    requested = options.get("max_context_chars")
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        return requested
    bounded = config.get("bounded_context") or {}
    if isinstance(bounded, Mapping):
        configured = bounded.get("max_context_chars")
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            return configured
    return 60_000


def _ledger_units(snapshot: Any) -> tuple[int, int] | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, Mapping):
        used = snapshot.get("used_units", 0)
        reserved = snapshot.get("reserved_units", 0)
    else:
        used = getattr(snapshot, "used_units", None)
        reserved = getattr(snapshot, "reserved_units", None)
    if (
        isinstance(used, bool)
        or isinstance(reserved, bool)
        or not isinstance(used, int)
        or not isinstance(reserved, int)
        or used < 0
        or reserved < 0
    ):
        return None
    return used, reserved


def _check(name: str, limit: int | None, observed: int | None) -> PreflightCheck:
    exceeded = (
        limit is not None
        and (observed is None or observed > limit)
    )
    return PreflightCheck(
        check=name,
        limit=limit,
        observed=observed,
        verdict="exceeded" if exceeded else "ok",
    )


def run_budget_preflight(
    request: Any,
    plan: Any,
    config: Mapping[str, Any],
    ledger_snapshot: Any,
) -> PreflightDecision:
    """Return the deterministic budget action without I/O or provider calls.

    ``ledger_snapshot`` is deliberately an input: state access belongs to the
    orchestration boundary so this function remains pure and straightforward to
    test. A missing snapshot is fail-closed only when a daily quota is enabled.
    """

    section = _section(config)
    limits = {
        "provider_call_cap": _limit(section, "max_provider_calls_per_request"),
        "daily_quota": _limit(section, "max_daily_provider_calls"),
        "timeout_budget": _limit(section, "max_timeout_seconds"),
        "context_budget": _limit(section, "max_context_chars"),
    }
    if section.get("enabled") is not True or not any(
        value is not None for value in limits.values()
    ):
        return PreflightDecision("proceed")

    planned_calls = _planned_provider_calls(request, plan)
    ledger = _ledger_units(ledger_snapshot)
    daily_observed = (
        None
        if limits["daily_quota"] is not None and ledger is None
        else (planned_calls + sum(ledger or (0, 0)))
    )
    timeout_observed = _request_timeout_seconds(request)
    context_observed = _context_limit(request, config)
    checks = (
        _check("provider_call_cap", limits["provider_call_cap"], planned_calls),
        _check("daily_quota", limits["daily_quota"], daily_observed),
        _check("timeout_budget", limits["timeout_budget"], timeout_observed),
        _check("context_budget", limits["context_budget"], context_observed),
    )
    exceeded = {check.check for check in checks if check.verdict == "exceeded"}
    if not exceeded:
        return PreflightDecision("proceed", checks=checks)

    if section.get("on_exceed", "degrade") not in _ON_EXCEED:
        raise ValueError("budget_preflight.on_exceed must be degrade or abort")
    if section.get("on_exceed", "degrade") == "abort":
        reason = (
            "budget_ledger_unavailable"
            if "daily_quota" in exceeded and ledger is None
            else "daily_quota_exhausted"
            if "daily_quota" in exceeded
            and limits["daily_quota"] is not None
            and daily_observed is not None
            and daily_observed - planned_calls >= limits["daily_quota"]
            else "budget_unsatisfiable"
        )
        return PreflightDecision("abort", checks=checks, reason=reason)

    if "daily_quota" in exceeded and ledger is None:
        return PreflightDecision(
            "abort", checks=checks, reason="budget_ledger_unavailable"
        )

    candidate_limits: Sequence[int] = (planned_calls,)
    if limits["provider_call_cap"] is not None:
        candidate_limits = (*candidate_limits, limits["provider_call_cap"])
    if limits["daily_quota"] is not None:
        candidate_limits = (
            *candidate_limits,
            limits["daily_quota"] - sum(ledger or (0, 0)),
        )
    allowed_calls = min(candidate_limits)
    if allowed_calls < 1:
        return PreflightDecision(
            "abort", checks=checks, reason="daily_quota_exhausted"
        )

    adjustments: Dict[str, int] = {}
    if allowed_calls < planned_calls:
        adjustments["max_provider_calls"] = allowed_calls
    if "timeout_budget" in exceeded and limits["timeout_budget"] is not None:
        adjustments["timeout_seconds"] = limits["timeout_budget"]
    if "context_budget" in exceeded and limits["context_budget"] is not None:
        adjustments["context_limit"] = limits["context_budget"]
    if not adjustments:
        return PreflightDecision(
            "abort", checks=checks, reason="budget_unsatisfiable"
        )
    return PreflightDecision("degrade", checks=checks, adjustments=adjustments)
