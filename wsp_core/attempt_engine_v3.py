"""Engine-owned admission, retry, circuit and budget handling for v3 attempts."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from .contract_v3 import (
    AttemptEndReason,
    AttemptOutcome,
    Capability,
    CircuitState,
    ErrorClass,
    ErrorV3,
    ProviderAttemptV3,
    SkipReason,
)
from .credentials import is_secret_scope_error
from .errors_v3 import classify_provider_error
from .http_client import (
    ProviderBatchStopped,
    RequestDeadlineExceeded,
    is_request_deadline_abort,
    request_deadline_scope,
)
from .provider_health import MAX_RETRY_AFTER_WAIT_SECONDS, _retry_delay
from .state_store_v3 import BLOCKING_ERROR_CLASSES, CircuitKey, SQLiteStateStore


_MIN_RETRY_WINDOW_SECONDS = 1.0
_HEALTH_ERROR_CLASSES = frozenset(BLOCKING_ERROR_CLASSES)


@dataclass(frozen=True)
class AttemptContext:
    provider: str
    capability: Capability
    endpoint: str
    credential_fingerprint: str
    budget_scope: str
    budget_window: str
    budget_units: int = 1
    budget_limit_units: int = 3
    daily_budget_scope: str | None = None
    daily_budget_window: str | None = None
    daily_budget_limit_units: int | None = None
    deadline_monotonic: float | None = None

    def __post_init__(self) -> None:
        if self.budget_units < 0 or self.budget_limit_units < 0:
            raise ValueError("budget units must be non-negative")
        daily_values = (
            self.daily_budget_scope,
            self.daily_budget_window,
            self.daily_budget_limit_units,
        )
        if any(value is not None for value in daily_values) and not all(
            value is not None for value in daily_values
        ):
            raise ValueError("daily budget settings must be specified together")
        if self.daily_budget_limit_units is not None and self.daily_budget_limit_units < 1:
            raise ValueError("daily budget limit must be positive")
        if self.deadline_monotonic is not None and self.deadline_monotonic <= 0:
            raise ValueError("deadline must be positive")

    @property
    def circuit_key(self) -> CircuitKey:
        return CircuitKey(
            self.provider,
            self.capability,
            self.endpoint,
            self.credential_fingerprint,
        )


@dataclass(frozen=True)
class AttemptExecution:
    payload: Optional[Dict]
    receipt: ProviderAttemptV3


class AttemptEngine:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.store = store
        self.max_attempts = max_attempts
        self.sleep = sleep

    @staticmethod
    def _retry_wait(
        context: AttemptContext, error, attempt_index: int
    ) -> Optional[float]:
        """Seconds to pause before the next try, or None when no retry should run.

        A Retry-After above the inline cap (or past the request deadline) is not
        waited out: the failure is recorded and the circuit holds the provider.
        Without Retry-After a short backoff spaces the retry, as the legacy path did.
        """
        retry_after = error.retry_after_seconds
        if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0:
            wait = float(retry_after)
            if wait > MAX_RETRY_AFTER_WAIT_SECONDS:
                return None
        else:
            wait = _retry_delay(attempt_index)
        if (
            context.deadline_monotonic is not None
            and time.monotonic() + wait + _MIN_RETRY_WINDOW_SECONDS > context.deadline_monotonic
        ):
            return None
        return wait

    @staticmethod
    def _receipt(
        context: AttemptContext, started: int, payload: Optional[Dict] = None, **fields: Any
    ) -> AttemptExecution:
        return AttemptExecution(
            payload,
            ProviderAttemptV3(
                attempt_id="attempt_" + uuid.uuid4().hex[:16],
                provider=context.provider,
                capability=context.capability,
                started_at=_utc(started),
                endpoint_id=f"{context.provider}:{context.capability.value}",
                **fields,
            ),
        )

    def skip(
        self,
        context: AttemptContext,
        reason: SkipReason,
        *,
        now: Callable[[], float] = time.time,
    ) -> AttemptExecution:
        """Emit an explicit receipt for a candidate excluded before admission."""
        return self._receipt(
            context,
            int(now()),
            outcome=AttemptOutcome.SKIPPED,
            duration_ms=0,
            skip_reason=reason,
            budget_decision="unknown",
            circuit_state_before=CircuitState.CLOSED,
            circuit_state_after=CircuitState.CLOSED,
            decision="skipped",
        )

    def cancel_started(
        self,
        context: AttemptContext,
        *,
        started_at: float,
        duration_ms: int,
        reason: AttemptEndReason = AttemptEndReason.DEADLINE,
        message: str | None = None,
    ) -> AttemptExecution:
        """Describe a launched provider call that its caller did not wait for.

        ``reason`` says why: the caller's deadline (the default, a budget
        limit), another candidate's win, the research quorum, or an engine
        error that lost the call's own receipt. Only the deadline is a budget
        limit; the receipt carries the reason in ``error.details`` so that
        status derivation never has to guess it from timing.
        """
        started = int(started_at)
        elapsed_ms = max(0, int(duration_ms))
        if reason is AttemptEndReason.DEADLINE:
            error = classify_provider_error(
                TimeoutError("provider call exceeded the caller deadline"),
                provider=context.provider,
            )
        else:
            code, default_message = _CANCELLATION_TEXT[reason]
            error = ErrorV3(
                error_class=(
                    ErrorClass.INTERNAL
                    if reason is AttemptEndReason.ENGINE_ERROR
                    else ErrorClass.CANCELLED
                ),
                code=code,
                message=message or default_message,
                retryable=False,
                provider=context.provider,
                details={"end_reason": reason.value},
            )
        return self._receipt(
            context,
            started,
            outcome=AttemptOutcome.CANCELLED,
            end_reason=reason,
            duration_ms=elapsed_ms,
            error=error,
            budget_decision="unknown",
            tries=[_try(1, started, elapsed_ms, error)],
        )

    def execute(
        self,
        context: AttemptContext,
        operation: Callable[[], Dict],
        *,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> AttemptExecution:
        """Run one provider attempt, up to ``max_attempts`` tries, to one receipt.

        Every try takes the same steps and ends in exactly one of three ways::

            admitted -> budgets reserved -> dispatched -> answered | failed
                 \\______________\\_______________\\____ not dispatched

        Between admission and its end a try holds its budget reservations
        and, when admission granted one, the half-open probe lease. Each
        ending settles both, and nothing else does:

        ==============  ============  ========================================
        the try ended   reservations  probe lease
        ==============  ============  ========================================
        not dispatched  given back    given back
        answered        charged       used: the success closes the circuit
        failed          charged       kept for the retry, or used by the
                                      recorded failure; given back when the
                                      error says nothing about provider health
        ==============  ============  ========================================
        """
        started = int(now())
        attempt_started_monotonic = time.monotonic()
        before = CircuitState.CLOSED
        last_error = None
        encountered: set[ErrorClass] = set()
        tries: list = []
        lease = None
        units = context.budget_units
        request_budget = (context.budget_scope, context.budget_window)
        daily_budget = (
            None
            if context.daily_budget_scope is None
            else (context.daily_budget_scope, context.daily_budget_window or "")
        )

        def release_lease(decision) -> None:
            """Give back a probe lease so that the next caller can probe."""
            if (
                decision.circuit_state is CircuitState.HALF_OPEN
                and decision.blocking_error_class is not None
            ):
                self.store.release_half_open(
                    context.circuit_key,
                    decision.blocking_error_class,
                    now=int(now()),
                    lease_token=decision.lease_token,
                )

        def failed(
            error, budget_decision: str, *, deadline_aborted: bool = False,
            payload: Optional[Dict] = None,
        ) -> AttemptExecution:
            """The receipt of an attempt whose last dispatched try failed.

            Only a provider-owned failure is recorded as health. A ``payload``
            (results fetched before a provider-wide error stopped the batch) is
            kept and the attempt is reported as partial.
            """
            after = before
            if not deadline_aborted and error.error_class in _HEALTH_ERROR_CLASSES:
                after = self.store.record_failure(
                    context.circuit_key,
                    error.error_class,
                    now=int(now()),
                    retry_after_seconds=error.retry_after_seconds,
                ).state
            outcome = AttemptOutcome.FAILED
            end_reason = AttemptEndReason.PROVIDER_ERROR
            if deadline_aborted:
                outcome = AttemptOutcome.CANCELLED
                end_reason = AttemptEndReason.DEADLINE
            elif payload is not None:
                outcome = AttemptOutcome.PARTIAL
                end_reason = AttemptEndReason.ANSWERED
            return self._receipt(
                context,
                started,
                payload,
                outcome=outcome,
                end_reason=end_reason,
                retry_count=len(tries) - 1,
                result_count=len((payload or {}).get("results") or []),
                duration_ms=max(
                    0, int((time.monotonic() - attempt_started_monotonic) * 1000)
                ),
                error=error,
                budget_decision=budget_decision,
                circuit_state_before=before,
                circuit_state_after=after,
                tries=tries,
            )

        def not_dispatched(
            reason: SkipReason, state: CircuitState, budget_decision: str
        ) -> AttemptExecution:
            """The receipt of an attempt whose current try was stopped before its call.

            A retry stopped by the budget, the circuit or the deadline must not
            erase the paid earlier try from the receipt: the attempt is
            reported as the failure it was. Only an attempt that never ran is
            skipped.
            """
            if tries:
                return failed(last_error, budget_decision)
            return self._receipt(
                context,
                started,
                outcome=AttemptOutcome.SKIPPED,
                skip_reason=reason,
                budget_decision=budget_decision,
                circuit_state_before=state,
                circuit_state_after=state,
                decision="skipped",
            )

        self.store.configure_budget(
            *request_budget, limit_units=context.budget_limit_units
        )

        for index in range(self.max_attempts):
            if (
                context.deadline_monotonic is not None
                and (
                    time.monotonic() >= context.deadline_monotonic
                    or (index > 0 and time.monotonic() + _MIN_RETRY_WINDOW_SECONDS > context.deadline_monotonic)
                )
            ):
                return not_dispatched(
                    SkipReason.DEADLINE_EXCEEDED, CircuitState.CLOSED, "not_reserved"
                )

            # Admitted?
            if index > 0 and lease is not None:
                # This call holds the half-open probe lease; re-admitting would
                # find its own lease and block the retry it is entitled to.
                decision = lease
            else:
                decision = self.store.admit(context.circuit_key, now=int(now()))
            if index == 0:
                before = decision.circuit_state
            if decision.allowed and decision.circuit_state is CircuitState.HALF_OPEN:
                lease = decision
            if decision.allowed and decision.blocking_error_class is not None:
                encountered.add(decision.blocking_error_class)
            encountered.update(decision.failing_error_classes)
            if not decision.allowed:
                return not_dispatched(
                    decision.skip_reason or SkipReason.CIRCUIT_OPEN,
                    decision.circuit_state,
                    "not_reserved",
                )

            # Budgets reserved? The per-request budget lives in memory and is
            # enforced even when SQLite is down. Budgets that need the ledger
            # (a non-request window such as the daily provider-call limit) fail
            # closed when the store cannot be read: an unverifiable limit blocks
            # the call instead of allowing unlimited paid operations.
            budget_decision = "reserved" if decision.store_available else "store_unavailable"
            reserved = []
            if self.store.reserve_budget(*request_budget, units=units):
                reserved.append(request_budget)
                if daily_budget is not None:
                    self.store.configure_budget(
                        *daily_budget,
                        limit_units=int(context.daily_budget_limit_units or 0),
                    )
                    if self.store.reserve_budget(*daily_budget, units=units):
                        reserved.append(daily_budget)
            if len(reserved) < (1 if daily_budget is None else 2):
                for budget in reserved:
                    self.store.release_budget(*budget, units=units)
                release_lease(decision)
                return not_dispatched(
                    SkipReason.BUDGET_BLOCKED,
                    decision.circuit_state,
                    "blocked" if self.store.available else "store_unavailable",
                )

            # Dispatched.
            try_started = int(now())
            call_started = time.monotonic()
            try:
                # Adapters check this deadline before each HTTP request, so a
                # batch or an adapter retry starts nothing after it.
                with request_deadline_scope(context.deadline_monotonic):
                    payload = operation()
            except BaseException as exc:
                if isinstance(exc, RequestDeadlineExceeded) and not is_secret_scope_error(exc):
                    # The adapter sent nothing on this try: not dispatched.
                    for budget in reserved:
                        self.store.release_budget(*budget, units=units)
                    release_lease(decision)
                    return not_dispatched(
                        SkipReason.DEADLINE_EXCEEDED,
                        decision.circuit_state,
                        "not_reserved" if self.store.available else "store_unavailable",
                    )
                if not all([self.store.commit_budget(*budget, units=units) for budget in reserved]):
                    budget_decision = "store_unavailable"
                if not isinstance(exc, Exception) or is_secret_scope_error(exc):
                    # Control-flow and host-scope errors must reach the caller,
                    # without retaining a probe that produced no health verdict.
                    release_lease(decision)
                    raise
                stopped = exc if isinstance(exc, ProviderBatchStopped) else None
                if stopped is not None:
                    exc = stopped.cause
                last_error = classify_provider_error(exc, provider=context.provider)
                encountered.add(last_error.error_class)
                tries.append(
                    _try(
                        index + 1,
                        try_started,
                        max(0, int((time.monotonic() - call_started) * 1000)),
                        last_error,
                    )
                )
                deadline_aborted = is_request_deadline_abort(
                    exc, deadline_monotonic=context.deadline_monotonic
                )
                if deadline_aborted or last_error.error_class not in _HEALTH_ERROR_CLASSES:
                    # The caller's deadline or a request, contract or config
                    # error: no verdict on provider health, the probe goes back.
                    release_lease(decision)
                retry_wait = (
                    self._retry_wait(context, last_error, index)
                    if not deadline_aborted and stopped is None and last_error.retryable
                    and index < self.max_attempts - 1
                    else None
                )
                if retry_wait is None:
                    return failed(
                        last_error,
                        budget_decision,
                        deadline_aborted=deadline_aborted,
                        payload=stopped.payload if stopped is not None else None,
                    )
                try:
                    self.sleep(retry_wait)
                except BaseException:
                    release_lease(decision)
                    raise
                continue

            # Answered.
            if not all([self.store.commit_budget(*budget, units=units) for budget in reserved]):
                budget_decision = "store_unavailable"
            for error_class in encountered:
                self.store.record_success(
                    context.circuit_key, error_class, now=int(now())
                )
            result_count = len(payload.get("results") or [])
            tries.append(
                _try(
                    index + 1,
                    try_started,
                    max(0, int((time.monotonic() - call_started) * 1000)),
                    None,
                )
            )
            return self._receipt(
                context,
                started,
                payload,
                outcome=AttemptOutcome.SUCCESS,
                end_reason=(
                    AttemptEndReason.ANSWERED
                    if result_count
                    else AttemptEndReason.ANSWERED_EMPTY
                ),
                retry_count=index,
                result_count=result_count,
                duration_ms=max(
                    0, int((time.monotonic() - attempt_started_monotonic) * 1000)
                ),
                budget_decision=budget_decision,
                circuit_state_before=before,
                circuit_state_after=(
                    CircuitState.CLOSED
                    if self.store.available
                    else CircuitState.UNKNOWN
                ),
                tries=tries,
            )

        raise RuntimeError(last_error or "attempt loop exhausted")


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _try(number: int, started: int, duration_ms: int, error: Optional[ErrorV3]) -> Dict:
    """One entry of a receipt's ``tries``: a dispatched provider call."""
    if error is None:
        return {
            "try_number": number,
            "started_at": _utc(started),
            "duration_ms": duration_ms,
            "outcome": "success",
            "error": None,
        }
    retry_after_ms = None
    if error.retry_after_seconds is not None and math.isfinite(error.retry_after_seconds):
        retry_after_ms = max(0, int(error.retry_after_seconds * 1000))
    return {
        "try_number": number,
        "started_at": _utc(started),
        "duration_ms": duration_ms,
        "outcome": "error",
        "error": {
            "error_class": error.error_class.value,
            "code": error.code,
            "http_status": error.http_status,
            "retryable": error.retryable,
            "retry_after_ms": retry_after_ms,
        },
    }


_CANCELLATION_TEXT = {
    AttemptEndReason.SUPERSEDED_BY_WINNER: (
        "wsp.attempt.superseded",
        "Cancelled: another provider answered first",
    ),
    AttemptEndReason.PREEMPTED_AFTER_QUORUM: (
        "wsp.attempt.preempted",
        "Cancelled: the research quorum was reached without it",
    ),
    AttemptEndReason.ENGINE_ERROR: (
        "wsp.internal.attempt_lost",
        "Provider execution failed before its receipt was written",
    ),
}


def request_deadline(budget: Dict[str, Any]) -> float | None:
    """Monotonic deadline from a request's positive integer max_wall_time_ms."""
    ms = budget.get("max_wall_time_ms")
    if isinstance(ms, int) and not isinstance(ms, bool) and ms > 0:
        return time.monotonic() + ms / 1000
    return None


def provider_attempt_context(
    store: Any,
    provider: str,
    capability: Capability,
    provider_config: Dict[str, Any],
    credential: str | None,
    **budget: Any,
) -> AttemptContext:
    """Attempt context for one provider: endpoint, credential fingerprint, budget."""
    from .credentials import redact_url

    endpoint = (
        provider_config.get("endpoint")
        or provider_config.get("base_url")
        or provider_config.get("url")
    )
    endpoint = redact_url(str(endpoint), origin_only=True) if endpoint else f"provider://{provider}/{capability.value}"
    return AttemptContext(
        provider=provider,
        capability=capability,
        endpoint=endpoint,
        credential_fingerprint=store.fingerprint_credential(credential or f"keyless:{provider}"),
        budget_window="request",
        **budget,
    )
