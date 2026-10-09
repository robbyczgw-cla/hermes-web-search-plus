"""A provider blip must not lock users out; hostile tool numbers must not raise."""

import importlib
import time

import pytest

from wsp_core.contract_v3 import Capability, CircuitState, ErrorClass, SkipReason
from wsp_core.state_store_v3 import CircuitKey, SQLiteStateStore


def _key():
    return CircuitKey("brave", Capability.SEARCH, "provider://brave/search", "fp")


@pytest.mark.parametrize("error_class", [ErrorClass.TRANSIENT, ErrorClass.TIMEOUT])
def test_single_blip_does_not_block_the_next_request(tmp_path, error_class):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.record_failure(_key(), error_class, now=100)
    assert store.admit(_key(), now=101).allowed is True
    store.record_failure(_key(), error_class, now=102)
    assert store.admit(_key(), now=103).allowed is True


@pytest.mark.parametrize("error_class", [ErrorClass.TRANSIENT, ErrorClass.TIMEOUT])
def test_three_consecutive_failures_open_the_circuit(tmp_path, error_class):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    for now in (100, 101, 102):
        store.record_failure(_key(), error_class, now=now)
    decision = store.admit(_key(), now=103)
    assert decision.allowed is False
    assert decision.skip_reason is SkipReason.CIRCUIT_OPEN


def test_success_resets_the_consecutive_count(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.record_failure(_key(), ErrorClass.TRANSIENT, now=100)
    store.record_failure(_key(), ErrorClass.TRANSIENT, now=101)
    store.record_success(_key(), ErrorClass.TRANSIENT, now=102)
    store.record_failure(_key(), ErrorClass.TRANSIENT, now=103)
    assert store.admit(_key(), now=104).allowed is True


@pytest.mark.parametrize(
    ("error_class", "reason"),
    [
        (ErrorClass.AUTH, SkipReason.AUTH_BLOCKED),
        (ErrorClass.QUOTA, SkipReason.QUOTA_BLOCKED),
        (ErrorClass.RATE_LIMIT, SkipReason.RATE_LIMITED),
    ],
)
def test_auth_quota_and_rate_limit_still_block_on_first_failure(tmp_path, error_class, reason):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    store.record_failure(_key(), error_class, now=100, retry_after_seconds=30)
    decision = store.admit(_key(), now=101)
    assert decision.allowed is False
    assert decision.skip_reason is reason


def test_last_resort_probe_answers_when_every_candidate_is_circuit_open(tmp_path, monkeypatch):
    search = importlib.import_module("wsp_core.search")
    from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine

    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)
    contexts = {
        name: AttemptContext(
            provider=name,
            capability=Capability.SEARCH,
            endpoint=f"provider://{name}/search",
            credential_fingerprint="fp",
            budget_scope="req",
            budget_window="request",
        )
        for name in ("brave", "serper")
    }
    for ctx in contexts.values():
        for _ in range(3):
            store.record_failure(ctx.circuit_key, ErrorClass.TRANSIENT, now=int(time.time()), retry_after_seconds=3600)

    calls = []

    def operation_for(name):
        def op():
            calls.append(name)
            return {"results": [{"title": "t", "url": "https://example.com", "snippet": "s"}]}
        return op

    race = search._race_providers(engine, ["brave", "serper"], contexts, operation_for, lambda _p: 5.0, None)
    assert race.provider is None
    assert all(r.skip_reason is SkipReason.CIRCUIT_OPEN for r in race.receipts)

    race = search._last_resort_probe(engine, store, "brave", contexts, operation_for, race)
    assert race.provider == "brave"
    assert calls == ["brave"]
    assert race.receipts[0].decision == "attempted"
    assert race.receipts[1].skip_reason is SkipReason.CIRCUIT_OPEN
    assert store.admit(contexts["brave"].circuit_key, now=int(time.time())).circuit_state is CircuitState.CLOSED
