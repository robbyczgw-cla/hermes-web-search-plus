"""Engine-owned admission, retry, circuit and budget handling for v3 attempts."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from contract_v3 import (
    AttemptOutcome,
    Capability,
    CircuitState,
    ErrorClass,
    ProviderAttemptV3,
    SkipReason,
)
from errors_v3 import classify_provider_error
from state_store_v3 import CircuitKey, SQLiteStateStore


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

    def __post_init__(self) -> None:
        if self.budget_units < 0 or self.budget_limit_units < 0:
            raise ValueError("budget units must be non-negative")

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
        if error.retry_after_seconds is not None:
            retry_after_ms = max(0, int(error.retry_after_seconds * 1000))
        return {
            "error_class": error.error_class.value,
            "code": error.code,
            "http_status": error.http_status,
            "retryable": error.retryable,
            "retry_after_ms": retry_after_ms,
        }

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
                budget_decision="not_evaluated",
                circuit_state_before=CircuitState.CLOSED,
                circuit_state_after=CircuitState.CLOSED,
                endpoint_id=f"{context.provider}:{context.capability.value}",
                decision="skipped",
                tries=[],
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
            decision = self.store.admit(context.circuit_key, now=int(now()))
            if index == 0:
                before = decision.circuit_state
            if decision.allowed and decision.blocking_error_class is not None:
                encountered.add(decision.blocking_error_class)
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
            if reserved and not self.store.reserve_budget(
                context.budget_scope,
                context.budget_window,
                units=context.budget_units,
            ):
                return self._skipped(
                    context,
                    started=started,
                    retry_count=index,
                    state=decision.circuit_state,
                    reason=SkipReason.BUDGET_BLOCKED,
                    budget_decision="blocked",
                )
            budget_decision = "reserved" if reserved else "store_unavailable"

            try_started = int(now())
            call_started = time.monotonic()
            try:
                payload = operation()
            except BaseException as exc:
                if reserved:
                    self.store.commit_budget(
                        context.budget_scope,
                        context.budget_window,
                        units=context.budget_units,
                    )
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
                should_retry = classified.retryable and index < self.max_attempts - 1
                if should_retry:
                    self.sleep(classified.retry_after_seconds or 0.0)
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
                self.store.commit_budget(
                    context.budget_scope,
                    context.budget_window,
                    units=context.budget_units,
                )
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
