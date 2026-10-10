"""Engine-owned admission, retry, circuit and budget handling for v3 attempts."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from .contract_v3 import (
    AttemptOutcome,
    Capability,
    CircuitState,
    ErrorClass,
    ProviderAttemptV3,
    SkipReason,
)
from .errors_v3 import classify_provider_error
from .provider_health import MAX_RETRY_AFTER_WAIT_SECONDS, _retry_delay
from .state_store_v3 import CircuitKey, SQLiteStateStore


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
    def _attempt_id(context: AttemptContext, started: int) -> str:
        del context, started
        return "attempt_" + uuid.uuid4().hex[:16]

    @staticmethod
    def _started_at(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _try_error(error) -> Dict:
        retry_after_ms = None
        if error.retry_after_seconds is not None and math.isfinite(
            error.retry_after_seconds
        ):
            retry_after_ms = max(0, int(error.retry_after_seconds * 1000))
        return {
            "error_class": error.error_class.value,
            "code": error.code,
            "http_status": error.http_status,
            "retryable": error.retryable,
            "retry_after_ms": retry_after_ms,
        }

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
            and time.monotonic() + wait >= context.deadline_monotonic
        ):
            return None
        return wait

    def _skipped(
        self,
        context: AttemptContext,
        *,
        started: int,
        retry_count: int,
        state: CircuitState,
        reason: SkipReason,
        budget_decision: str,
    ) -> AttemptExecution:
        return AttemptExecution(
            None,
            ProviderAttemptV3(
                attempt_id=self._attempt_id(context, started),
                provider=context.provider,
                capability=context.capability,
                outcome=AttemptOutcome.SKIPPED,
                retry_count=retry_count,
                started_at=self._started_at(started),
                skip_reason=reason,
                budget_decision=budget_decision,
                circuit_state_before=state,
                circuit_state_after=state,
                endpoint_id=f"{context.provider}:{context.capability.value}",
                decision="skipped",
                tries=[],
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
        started = int(now())
        return AttemptExecution(
            None,
            ProviderAttemptV3(
                attempt_id=self._attempt_id(context, started),
                provider=context.provider,
                capability=context.capability,
                outcome=AttemptOutcome.SKIPPED,
                started_at=self._started_at(started),
                duration_ms=0,
                retry_count=0,
                result_count=0,
                skip_reason=reason,
                error=None,
                budget_decision="unknown",
                circuit_state_before=CircuitState.CLOSED,
                circuit_state_after=CircuitState.CLOSED,
                endpoint_id=f"{context.provider}:{context.capability.value}",
                decision="skipped",
                tries=[],
            ),
        )

    def cancel_started(
        self,
        context: AttemptContext,
        *,
        started_at: float,
        duration_ms: int,
    ) -> AttemptExecution:
        """Describe a launched provider call whose caller stopped at its deadline."""
        started = int(started_at)
        elapsed_ms = max(0, int(duration_ms))
        error = classify_provider_error(
            TimeoutError("provider call exceeded the caller deadline"),
            provider=context.provider,
        )
        return AttemptExecution(
            None,
            ProviderAttemptV3(
                attempt_id=self._attempt_id(context, started),
                provider=context.provider,
                capability=context.capability,
                outcome=AttemptOutcome.CANCELLED,
                retry_count=0,
                result_count=0,
                started_at=self._started_at(started),
                duration_ms=elapsed_ms,
                error=error,
                budget_decision="unknown",
                circuit_state_before=CircuitState.UNKNOWN,
                circuit_state_after=CircuitState.UNKNOWN,
                endpoint_id=f"{context.provider}:{context.capability.value}",
                decision="attempted",
                tries=[
                    {
                        "try_number": 1,
                        "started_at": self._started_at(started),
                        "duration_ms": elapsed_ms,
                        "outcome": "error",
                        "error": self._try_error(error),
                    }
                ],
            ),
        )

    def execute(
        self,
        context: AttemptContext,
        operation: Callable[[], Dict],
        *,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> AttemptExecution:
        started = int(now())
        attempt_started_monotonic = time.monotonic()
        before = CircuitState.CLOSED
        last_error = None
        encountered: set[ErrorClass] = set()
        tries = []

        self.store.configure_budget(
            context.budget_scope,
            context.budget_window,
            limit_units=context.budget_limit_units,
        )

        for index in range(self.max_attempts):
            if (
                context.deadline_monotonic is not None
                and time.monotonic() >= context.deadline_monotonic
            ):
                return self._skipped(
                    context,
                    started=started,
                    retry_count=index,
                    state=CircuitState.CLOSED,
                    reason=SkipReason.DEADLINE_EXCEEDED,
                    budget_decision="not_reserved",
                )
            decision = self.store.admit(context.circuit_key, now=int(now()))
            if index == 0:
                before = decision.circuit_state
            if decision.allowed and decision.blocking_error_class is not None:
                encountered.add(decision.blocking_error_class)
            encountered.update(decision.failing_error_classes)
            if not decision.allowed:
                return self._skipped(
                    context,
                    started=started,
                    retry_count=index,
                    state=decision.circuit_state,
                    reason=decision.skip_reason or SkipReason.CIRCUIT_OPEN,
                    budget_decision="not_reserved",
                )

            reserved = decision.store_available
            budget_decision = "reserved" if reserved else "store_unavailable"
            daily_reserved = False
            if reserved:
                budget_allowed = self.store.reserve_budget(
                    context.budget_scope,
                    context.budget_window,
                    units=context.budget_units,
                )
                if not budget_allowed and self.store.available:
                    return self._skipped(
                        context,
                        started=started,
                        retry_count=index,
                        state=decision.circuit_state,
                        reason=SkipReason.BUDGET_BLOCKED,
                        budget_decision="blocked",
                    )
                if not budget_allowed:
                    reserved = False
                    budget_decision = "store_unavailable"
            if reserved and context.daily_budget_scope is not None:
                self.store.configure_budget(
                    context.daily_budget_scope,
                    context.daily_budget_window or "",
                    limit_units=int(context.daily_budget_limit_units or 0),
                )
                daily_allowed = self.store.reserve_budget(
                    context.daily_budget_scope,
                    context.daily_budget_window or "",
                    units=context.budget_units,
                )
                if not daily_allowed:
                    self.store.release_budget(
                        context.budget_scope,
                        context.budget_window,
                        units=context.budget_units,
                    )
                    return self._skipped(
                        context,
                        started=started,
                        retry_count=index,
                        state=decision.circuit_state,
                        reason=SkipReason.BUDGET_BLOCKED,
                        budget_decision=(
                            "blocked" if self.store.available else "store_unavailable"
                        ),
                    )
                daily_reserved = True

            try_started = int(now())
            call_started = time.monotonic()
            try:
                payload = operation()
            except BaseException as exc:
                if reserved:
                    reconciled = self.store.commit_budget(
                        context.budget_scope,
                        context.budget_window,
                        units=context.budget_units,
                    )
                    if not reconciled:
                        budget_decision = "store_unavailable"
                if daily_reserved:
                    reconciled_daily = self.store.commit_budget(
                        context.daily_budget_scope or "",
                        context.daily_budget_window or "",
                        units=context.budget_units,
                    )
                    if not reconciled_daily:
                        budget_decision = "store_unavailable"
                classified = classify_provider_error(exc, provider=context.provider)
                last_error = classified
                encountered.add(classified.error_class)
                tries.append(
                    {
                        "try_number": index + 1,
                        "started_at": self._started_at(try_started),
                        "duration_ms": max(
                            0, int((time.monotonic() - call_started) * 1000)
                        ),
                        "outcome": "error",
                        "error": self._try_error(classified),
                    }
                )
                retry_wait = (
                    self._retry_wait(context, classified, index)
                    if classified.retryable and index < self.max_attempts - 1
                    else None
                )
                if retry_wait is not None:
                    self.sleep(retry_wait)
                    continue
                after_record = self.store.record_failure(
                    context.circuit_key,
                    classified.error_class,
                    now=int(now()),
                    retry_after_seconds=classified.retry_after_seconds,
                )
                return AttemptExecution(
                    None,
                    ProviderAttemptV3(
                        attempt_id=self._attempt_id(context, started),
                        provider=context.provider,
                        capability=context.capability,
                        outcome=AttemptOutcome.FAILED,
                        retry_count=index,
                        result_count=0,
                        started_at=self._started_at(started),
                        duration_ms=max(
                            0,
                            int(
                                (time.monotonic() - attempt_started_monotonic) * 1000
                            ),
                        ),
                        error=classified,
                        budget_decision=budget_decision,
                        circuit_state_before=before,
                        circuit_state_after=after_record.state,
                        endpoint_id=f"{context.provider}:{context.capability.value}",
                        decision="attempted",
                        tries=tries,
                    ),
                )

            if reserved:
                reconciled = self.store.commit_budget(
                    context.budget_scope,
                    context.budget_window,
                    units=context.budget_units,
                )
                if not reconciled:
                    budget_decision = "store_unavailable"
            if daily_reserved:
                reconciled_daily = self.store.commit_budget(
                    context.daily_budget_scope or "",
                    context.daily_budget_window or "",
                    units=context.budget_units,
                )
                if not reconciled_daily:
                    budget_decision = "store_unavailable"
            for error_class in encountered:
                self.store.record_success(
                    context.circuit_key, error_class, now=int(now())
                )
            result_count = len(payload.get("results") or [])
            try_duration_ms = max(
                0, int((time.monotonic() - call_started) * 1000)
            )
            tries.append(
                {
                    "try_number": index + 1,
                    "started_at": self._started_at(try_started),
                    "duration_ms": try_duration_ms,
                    "outcome": "success",
                    "error": None,
                }
            )
            duration_ms = max(
                0, int((time.monotonic() - attempt_started_monotonic) * 1000)
            )
            endpoint_id = f"{context.provider}:{context.capability.value}"
            return AttemptExecution(
                payload,
                ProviderAttemptV3(
                    attempt_id=self._attempt_id(context, started),
                    provider=context.provider,
                    capability=context.capability,
                    outcome=AttemptOutcome.SUCCESS,
                    retry_count=index,
                    result_count=result_count,
                    started_at=self._started_at(started),
                    duration_ms=duration_ms,
                    budget_decision=budget_decision,
                    circuit_state_before=before,
                    circuit_state_after=(
                        CircuitState.CLOSED
                        if self.store.available
                        else CircuitState.UNKNOWN
                    ),
                    endpoint_id=endpoint_id,
                    decision="attempted",
                    tries=tries,
                ),
            )

        raise RuntimeError(last_error or "attempt loop exhausted")


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
    endpoint = str(
        provider_config.get("endpoint")
        or provider_config.get("base_url")
        or provider_config.get("url")
        or f"provider://{provider}/{capability.value}"
    )
    return AttemptContext(
        provider=provider,
        capability=capability,
        endpoint=endpoint,
        credential_fingerprint=store.fingerprint_credential(credential or f"keyless:{provider}"),
        budget_window="request",
        **budget,
    )
