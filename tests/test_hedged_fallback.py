"""Auto search falls back on empty or slow answers, quickly and only once.

v4.3.5 tried providers strictly one after another, two tries each with a 30 s
socket timeout, and accepted an empty result as success (no fallback). 5.0
starts the next provider when the current one returns nothing, fails, or is
slower than its usual (p75) latency, and takes the first non-empty answer.
"""

from __future__ import annotations

import threading
import time

import pytest

from wsp_core import http_client, providers, search
from wsp_core.config import _deepcopy_default_config
from wsp_core.http_client import ProviderRequestError

KEYS = {"SERPER_API_KEY": "serper-test-key-123456", "BRAVE_API_KEY": "brave-test-key-1234567",
        "EXA_API_KEY": "exa-test-key-123456789"}


def _config(**v3):
    config = _deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["serper", "brave", "exa"]
    config["v3"] = {"hedge_min_delay_seconds": 0.2, "attempt_timeout_seconds": 5, **v3}
    return config


def _route_to(monkeypatch, provider):
    monkeypatch.setattr(search, "auto_route_provider", lambda query, config: {
        "provider": provider, "confidence": 0.9, "confidence_level": "high", "reason": "test",
        "routing_policy": "routing-v3", "scores": {provider: 9.0}, "top_signals": [],
        "analysis_summary": {"routing_class": "general"},
    })


def _answer(name, url="https://example.com/a"):
    def search_fn(query, api_key, max_results=5, **_kwargs):
        return {"provider": name, "query": query, "results": [
            {"title": name, "url": url, "snippet": f"from {name}"}]}
    return search_fn


def _empty(name):
    def search_fn(query, api_key, max_results=5, **_kwargs):
        return {"provider": name, "query": query, "results": []}
    return search_fn


@pytest.fixture
def keyed(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)


def _search(config, **kwargs):
    return search.run_search_request(query="hedge test", no_cache=True, config=config, **kwargs)


def test_empty_answer_falls_back_to_the_next_provider(monkeypatch, keyed):
    _route_to(monkeypatch, "serper")
    monkeypatch.setattr(providers, "search_serper", _empty("serper"))
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))

    result = _search(_config())

    assert result["provider"] == "brave"
    assert result["results"][0]["snippet"] == "from brave"
    assert result["routing"]["fallback_used"] is True
    assert result["routing"]["original_provider"] == "serper"
    assert result["routing"]["fallback_reason"] == "insufficient_results"


def test_slow_provider_is_hedged_after_its_hedge_delay(monkeypatch, keyed):
    _route_to(monkeypatch, "serper")
    release = threading.Event()

    def slow_serper(query, api_key, max_results=5, **_kwargs):
        release.wait(3)
        return _answer("serper")(query, api_key, max_results)

    monkeypatch.setattr(providers, "search_serper", slow_serper)
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))

    started = time.monotonic()
    result = _search(_config())
    elapsed = time.monotonic() - started
    release.set()

    assert result["provider"] == "brave"
    assert elapsed < 1.5
    assert result["routing"]["fallback_reason"] == "selected_failed"


def test_failing_provider_gets_one_try_when_a_fallback_exists(monkeypatch, keyed):
    _route_to(monkeypatch, "serper")
    calls = []

    def flaky_serper(query, api_key, max_results=5, **_kwargs):
        calls.append(1)
        raise ProviderRequestError("unavailable", status_code=503, transient=True)

    monkeypatch.setattr(providers, "search_serper", flaky_serper)
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))

    result = _search(_config())

    assert result["provider"] == "brave"
    assert len(calls) == 1


def test_explicit_provider_without_fallback_keeps_its_retry(monkeypatch, keyed):
    calls = []

    def flaky_serper(query, api_key, max_results=5, **_kwargs):
        calls.append(1)
        raise ProviderRequestError("unavailable", status_code=503, transient=True)

    monkeypatch.setattr(providers, "search_serper", flaky_serper)

    result = _search(_config(), provider="serper")

    assert result.get("error")
    assert len(calls) == 2


def test_attempt_timeout_is_capped_only_when_a_fallback_exists(monkeypatch, keyed, tmp_path):
    seen = []

    def fake_urlopen(request, timeout=30):
        seen.append(timeout)
        raise ProviderRequestError("stop", status_code=500)

    monkeypatch.setattr(http_client, "urlopen", fake_urlopen)
    _route_to(monkeypatch, "serper")
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))

    # Separate state stores: the first call's failure opens serper's circuit.
    _search(_config(attempt_timeout_seconds=7, state_path=str(tmp_path / "a.sqlite3")))
    assert seen[0] == 7

    seen.clear()
    _search(_config(attempt_timeout_seconds=7, state_path=str(tmp_path / "b.sqlite3")), provider="serper")
    assert seen and all(timeout == 30 for timeout in seen)


def test_all_providers_empty_returns_a_truthful_empty_answer(monkeypatch, keyed):
    _route_to(monkeypatch, "serper")
    for name in ("serper", "brave", "exa"):
        monkeypatch.setattr(providers, f"search_{name}", _empty(name))

    result = _search(_config())

    assert not result.get("error")
    assert result["results"] == []
    assert result["provider"] == "serper"


def test_hedged_response_is_a_valid_v3_receipt(monkeypatch, keyed):
    _route_to(monkeypatch, "serper")
    release = threading.Event()

    def slow_serper(query, api_key, max_results=5, **_kwargs):
        release.wait(3)
        return _answer("serper")(query, api_key, max_results)

    monkeypatch.setattr(providers, "search_serper", slow_serper)
    monkeypatch.setattr(providers, "search_brave", _answer("brave"))
    request = search.legacy_request_to_v3("search", {"query": "hedge receipt", "no_cache": True})

    response = search.run_search_request_v3(request, config=_config())
    release.set()

    receipt = response.routing_receipt
    assert receipt["selected_provider"] == "brave"
    decisions = {item["provider"]: item for item in receipt["candidate_decisions"]}
    assert decisions["serper"]["decision"] == "attempted_failed"
    assert decisions["brave"]["decision"] == "selected"
    outcomes = {attempt.provider: attempt.outcome.value for attempt in response.provider_attempts}
    assert outcomes["serper"] == "cancelled"
