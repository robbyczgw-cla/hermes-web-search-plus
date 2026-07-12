"""Engine-owned admission, retry, circuit and budget handling for v3 attempts."""

from __future__ import annotations

import hashlib
import time
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
        material = "\x1f".join(
            (
                context.provider,
                context.capability.value,
                context.endpoint,
                context.credential_fingerprint,
                context.budget_scope,
                str(started),
            )
        )
        return "attempt_" + hashlib.sha256(material.encode()).hexdigest()[:16]

    @staticmethod
    def _started_at(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

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
        before = CircuitState.CLOSED
        last_error = None
        encountered: set[ErrorClass] = set()

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

            if not self.store.reserve_budget(
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

            call_started = time.monotonic()
            try:
                payload = operation()
            except BaseException as exc:
                self.store.commit_budget(
                    context.budget_scope,
                    context.budget_window,
                    units=context.budget_units,
                )
                classified = classify_provider_error(exc, provider=context.provider)
                last_error = classified
                encountered.add(classified.error_class)
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
                            0, int((time.monotonic() - call_started) * 1000)
                        ),
                        error=classified,
                        budget_decision="reserved",
                        circuit_state_before=before,
                        circuit_state_after=after_record.state,
                    ),
                )

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
                    duration_ms=max(
                        0, int((time.monotonic() - call_started) * 1000)
                    ),
                    budget_decision="reserved",
                    circuit_state_before=before,
                    circuit_state_after=CircuitState.CLOSED,
                ),
            )

        raise RuntimeError(last_error or "attempt loop exhausted")
