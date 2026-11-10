"""Brave per-second pacing, the engine-owned 429 retry, and placeholder keys."""
import os
import time
from unittest import mock

import pytest

from wsp_core import config as config_module
from wsp_core import env_loader, providers
from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.contract_v3 import Capability
from wsp_core.http_client import ProviderRequestError
from wsp_core.state_store_v3 import SQLiteStateStore

POLICY = {
    "X-RateLimit-Policy": "1;w=1, 2000;w=2678400",
    "X-RateLimit-Remaining": "0, 1916",
    "X-RateLimit-Reset": "1, 2014658",
}
OK = {"web": {"results": [{"title": "T", "url": "https://a.example/", "description": "d"}]}}


@pytest.fixture(autouse=True)
def fresh_pacer(monkeypatch):
    monkeypatch.setattr(providers, "_BRAVE_PACER", providers._CallPacer())


class FakeBrave:
    """make_get_request stand-in: scripted outcomes plus captured headers."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, url, headers, timeout=30, *, capture_headers=(), response_headers=None):
        self.calls += 1
        status, hdrs = self.outcomes.pop(0)
        if response_headers is not None:
            response_headers.update({k: v for k, v in hdrs.items() if k in capture_headers})
        if status == 200:
            return OK
        raise ProviderRequestError("Rate limit exceeded (HTTP 429)", status_code=429, transient=True)


def test_policy_parsing():
    assert providers.brave_min_interval(POLICY) == 1.0
    assert providers.brave_min_interval({"X-RateLimit-Policy": "20;w=1, 20000000;w=2678400"}) == 0.05
    assert providers.brave_min_interval({"X-RateLimit-Policy": "2000;w=2678400"}) is None
    assert providers.brave_retry_wait(POLICY) == 1.0
    assert providers.brave_retry_wait({"Retry-After": "0.4"}) == 0.4
    monthly = {"X-RateLimit-Remaining": "5, 0", "X-RateLimit-Reset": "1, 2014658"}
    assert providers.brave_retry_wait(monthly) == 2014658


def _brave_engine(tmp_path, *, max_attempts=2, deadline=None, sleeps=None):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(
        store,
        max_attempts=max_attempts,
        sleep=(sleeps.append if sleeps is not None else (lambda _seconds: None)),
    )
    context = AttemptContext(
        "brave", Capability.SEARCH, "provider://brave/search", "fingerprint",
        "brave-request", "request", deadline_monotonic=deadline,
    )
    return engine, context


def test_429_on_per_second_window_is_one_call_carrying_the_reset_wait():
    fake = FakeBrave([(429, POLICY)])
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep") as sleep:
        with pytest.raises(ProviderRequestError) as caught:
            providers.search_brave("q", "brave-test-key")
    # No adapter-internal retry: the engine retries, visibly and within the deadline.
    assert fake.calls == 1
    sleep.assert_not_called()
    assert caught.value.retry_after == pytest.approx(1.0)


def test_engine_retries_brave_429_after_reset_as_a_second_try(tmp_path):
    fake = FakeBrave([(429, POLICY), (200, POLICY)])
    sleeps = []
    engine, context = _brave_engine(tmp_path, sleeps=sleeps)
    with mock.patch.object(providers, "make_get_request", fake):
        execution = engine.execute(context, lambda: providers.search_brave("q", "brave-test-key"))
    assert fake.calls == 2
    assert execution.payload["results"][0]["url"] == "https://a.example/"
    assert sleeps == [pytest.approx(1.0)]
    assert [t["outcome"] for t in execution.receipt.tries] == ["error", "success"]
    assert execution.receipt.tries[0]["error"]["error_class"] == "rate_limit"
    # Each dispatch is a reserved budget unit of the request.
    assert engine.store.get_budget("brave-request", "request").used_units == 2


def test_brave_429_retry_never_starts_after_the_deadline(tmp_path):
    fake = FakeBrave([(429, POLICY), (200, POLICY)])
    # The 1 s reset wait would end past the deadline: no second request.
    engine, context = _brave_engine(tmp_path, deadline=time.monotonic() + 0.5)
    with mock.patch.object(providers, "make_get_request", fake):
        execution = engine.execute(context, lambda: providers.search_brave("q", "brave-test-key"))
    assert fake.calls == 1
    assert execution.payload is None
    assert len(execution.receipt.tries) == 1


def test_spent_monthly_quota_fails_fast_for_fallback():
    monthly = dict(POLICY, **{"X-RateLimit-Remaining": "1, 0"})
    fake = FakeBrave([(429, monthly)])
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep") as sleep:
        with pytest.raises(ProviderRequestError):
            providers.search_brave("q", "brave-test-key")
    assert fake.calls == 1
    sleep.assert_not_called()


def test_second_429_is_raised_not_retried_forever(tmp_path):
    fake = FakeBrave([(429, POLICY), (429, POLICY)])
    engine, context = _brave_engine(tmp_path)
    with mock.patch.object(providers, "make_get_request", fake):
        execution = engine.execute(context, lambda: providers.search_brave("q", "brave-test-key"))
    assert fake.calls == 2
    assert execution.payload is None


def test_pacer_spaces_calls_after_learning_the_policy():
    clock = [100.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    fake = FakeBrave([(200, POLICY), (200, POLICY)])
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep", fake_sleep), \
         mock.patch.object(providers.time, "monotonic", lambda: clock[0]):
        providers.search_brave("q1", "brave-test-key")
        providers.search_brave("q2", "brave-test-key")
    assert sleeps == [pytest.approx(1.0)]


def test_pacer_never_waits_without_a_per_second_policy():
    fake = FakeBrave([(200, {}), (200, {}), (200, {})])
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep") as sleep:
        for i in range(3):
            providers.search_brave(f"q{i}", "brave-test-key")
    sleep.assert_not_called()


@pytest.mark.parametrize("value", [
    "your-brave-api-key-here", "your_api_key", "<BRAVE_API_KEY>", "xxxxxxxx",
    "changeme", "REPLACE_ME", "${BRAVE_API_KEY}", "api-key-here", "****",
])
def test_template_placeholders_are_not_credentials(value):
    assert env_loader.clean_env_value(value) is None
    with mock.patch.dict(os.environ, {"BRAVE_API_KEY": value}):
        assert config_module.get_api_key("brave", {}) is None
        assert not config_module.provider_configured("brave", {})


@pytest.mark.parametrize("value", [
    "BSA1a2b3c4d5e6f7g8h9", "tvly-dev-abc123", "sk-proj-abcdef", "4f1c9e0b2a7d",
    "yourcompany-key-77ab",
])
def test_real_looking_keys_are_kept(value):
    assert env_loader.clean_env_value(value) == value
