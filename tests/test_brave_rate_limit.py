"""Brave per-second pacing, one short 429 retry, and placeholder keys."""
import os
from unittest import mock

import pytest

from wsp_core import config as config_module
from wsp_core import env_loader, providers
from wsp_core.http_client import ProviderRequestError

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


def test_429_on_per_second_window_retries_once_after_reset():
    fake = FakeBrave([(429, POLICY), (200, POLICY)])
    sleeps = []
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep", sleeps.append):
        result = providers.search_brave("q", "brave-test-key")
    assert fake.calls == 2
    assert result["results"][0]["url"] == "https://a.example/"
    assert sleeps and sleeps[0] == pytest.approx(1.0)


def test_spent_monthly_quota_fails_fast_for_fallback():
    monthly = dict(POLICY, **{"X-RateLimit-Remaining": "1, 0"})
    fake = FakeBrave([(429, monthly)])
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep") as sleep:
        with pytest.raises(ProviderRequestError):
            providers.search_brave("q", "brave-test-key")
    assert fake.calls == 1
    sleep.assert_not_called()


def test_second_429_is_raised_not_retried_forever():
    fake = FakeBrave([(429, POLICY), (429, POLICY)])
    with mock.patch.object(providers, "make_get_request", fake), \
         mock.patch.object(providers.time, "sleep"):
        with pytest.raises(ProviderRequestError):
            providers.search_brave("q", "brave-test-key")
    assert fake.calls == 2


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
    assert env_loader.is_placeholder_env_value(value)
    with mock.patch.dict(os.environ, {"BRAVE_API_KEY": value}):
        assert config_module.get_api_key("brave", {}) is None
        assert not config_module.provider_configured("brave", {})


@pytest.mark.parametrize("value", [
    "BSA1a2b3c4d5e6f7g8h9", "tvly-dev-abc123", "sk-proj-abcdef", "4f1c9e0b2a7d",
    "yourcompany-key-77ab",
])
def test_real_looking_keys_are_kept(value):
    assert not env_loader.is_placeholder_env_value(value)
