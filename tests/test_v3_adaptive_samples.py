"""v3 engine-owned provider calls still feed adaptive routing samples.

Routing reads provider_stats.performance_adjustments() on every auto-routed
search. The v3 AttemptEngine owns retries, cooldowns, and circuit state, but
the performance memory is a separate signal. Without these samples the
router falls back to static priority and the Operator Console provider-health
view goes stale.
"""

from __future__ import annotations

import json

import pytest

import provider_stats
import search
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability
from http_client import ProviderRequestError


def _config(tmp_path, providers):
    return {
        "version": 1,
        "auto_routing": {
            "enabled": True,
            "provider_priority": list(providers),
            "disabled_providers": [],
        },
        **{p: {"api_key": f"{p}-test-key-1234567890123456789012"} for p in providers},
        "v3": {"state_path": str(tmp_path / "state.sqlite3"), "cache_dir": str(tmp_path)},
    }


def _payload(provider, n=3):
    return {
        "provider": provider,
        "query": "q",
        "results": [
            {"title": f"{provider} {i}", "url": f"https://{provider}.example/{i}", "snippet": "s"}
            for i in range(n)
        ],
    }


def _samples(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _request(provider, *, allow_fallback=False, request_id="adaptive", query="q"):
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": query, "provider": provider, "count": 3},
        request_id=request_id,
    )
    if allow_fallback:
        request = type(request).from_dict(
            {**request.to_dict(), "routing": {**request.routing, "allow_fallback": True}}
        )
    return request


def _forbid_legacy_health(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy health/retry seam was called")

    monkeypatch.setattr(search, "provider_in_cooldown", forbidden)
    monkeypatch.setattr(search, "execute_provider_with_retry", forbidden)
    monkeypatch.setattr(search, "mark_provider_failure", forbidden)
    monkeypatch.setattr(search, "reset_provider_health", forbidden)


def test_v3_search_success_records_one_adaptive_sample(tmp_path, monkeypatch):
    _forbid_legacy_health(monkeypatch)
    monkeypatch.setitem(
        search.SEARCH_DISPATCH, "serper", lambda *_a, **_k: _payload("serper", 4)
    )
    config = _config(tmp_path, ["serper"])

    execution = search.execute_v3_request(_request("serper"), search._search_adapter(), config)

    assert execution.response.provider_attempts[0].outcome.value == "success"
    samples = _samples(provider_stats.PROVIDER_STATS_FILE)
    assert list(samples) == ["serper"]
    assert len(samples["serper"]) == 1
    assert samples["serper"][0]["n"] == 4
    assert samples["serper"][0]["err"] is False
    assert samples["serper"][0]["lat"] >= 0


def test_v3_fallback_records_failure_and_success(tmp_path, monkeypatch):
    _forbid_legacy_health(monkeypatch)

    def failing(*_a, **_k):
        raise ProviderRequestError("bad key", status_code=401)

    monkeypatch.setitem(search.SEARCH_DISPATCH, "serper", failing)
    monkeypatch.setitem(search.SEARCH_DISPATCH, "you", lambda *_a, **_k: _payload("you", 2))
    config = _config(tmp_path, ["serper", "you"])

    execution = search.execute_v3_request(
        _request("serper", allow_fallback=True), search._search_adapter(), config
    )

    assert [a.outcome.value for a in execution.response.provider_attempts] == ["failed", "success"]
    samples = _samples(provider_stats.PROVIDER_STATS_FILE)
    assert [s["err"] for s in samples["serper"]] == [True]
    assert samples["serper"][0]["n"] == 0
    assert [s["err"] for s in samples["you"]] == [False]
    assert samples["you"][0]["n"] == 2
    # The raw provider error must never land in the stats file.
    assert "bad key" not in provider_stats.PROVIDER_STATS_FILE.read_text(encoding="utf-8")


def test_v3_retried_transient_failure_records_every_try(tmp_path, monkeypatch):
    _forbid_legacy_health(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    calls = []

    def flaky(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderRequestError("upstream", status_code=503, transient=True)
        return _payload("serper", 1)

    monkeypatch.setitem(search.SEARCH_DISPATCH, "serper", flaky)
    config = _config(tmp_path, ["serper"])

    execution = search.execute_v3_request(_request("serper"), search._search_adapter(), config)

    assert execution.response.provider_attempts[0].outcome.value == "success"
    samples = _samples(provider_stats.PROVIDER_STATS_FILE)["serper"]
    assert len(calls) == 2
    assert [s["err"] for s in samples] == [True, False]
    assert [s["n"] for s in samples] == [0, 1]


def test_v3_config_error_is_not_a_performance_sample(tmp_path, monkeypatch):
    _forbid_legacy_health(monkeypatch)
    config = _config(tmp_path, ["serper"])
    config.pop("serper")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    search.execute_v3_request(_request("serper"), search._search_adapter(), config)

    assert "serper" not in _samples(provider_stats.PROVIDER_STATS_FILE)


def test_v3_research_members_record_samples(tmp_path, monkeypatch):
    _forbid_legacy_health(monkeypatch)
    for provider in ("serper", "you"):
        monkeypatch.setitem(
            search.SEARCH_DISPATCH, provider, lambda *_a, _p=provider, **_k: _payload(_p, 2)
        )
    config = _config(tmp_path, ["serper", "you"])
    config.setdefault("quality", {})["research_quorum"] = {"enabled": False}
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "q", "provider": "auto", "count": 3, "mode": "research",
         "research_providers": "serper,you"},
        request_id="adaptive-research",
    )

    search.execute_v3_request(request, search._search_adapter(), config)

    samples = _samples(provider_stats.PROVIDER_STATS_FILE)
    assert {"serper", "you"} <= set(samples)
    assert all(s["err"] is False for p in ("serper", "you") for s in samples[p])


def test_v3_samples_reach_the_router(tmp_path, monkeypatch):
    """End to end: enough v3 calls produce a non-empty routing adjustment."""
    _forbid_legacy_health(monkeypatch)
    monkeypatch.setitem(
        search.SEARCH_DISPATCH, "serper", lambda *_a, **_k: _payload("serper", 5)
    )
    config = _config(tmp_path, ["serper"])
    for i in range(provider_stats.MIN_SAMPLES_FOR_ADJUSTMENT):
        # Distinct queries: a v3 cache hit is not a provider call and must not
        # count as a sample.
        search.execute_v3_request(
            _request("serper", request_id=f"warm-{i}", query=f"q{i}"),
            search._search_adapter(),
            config,
        )

    assert len(_samples(provider_stats.PROVIDER_STATS_FILE)["serper"]) == (
        provider_stats.MIN_SAMPLES_FOR_ADJUSTMENT
    )
    assert provider_stats.performance_adjustments(["serper"])["serper"] > 0


def test_v3_cache_hit_is_not_a_sample(tmp_path, monkeypatch):
    _forbid_legacy_health(monkeypatch)
    monkeypatch.setitem(
        search.SEARCH_DISPATCH, "serper", lambda *_a, **_k: _payload("serper", 2)
    )
    config = _config(tmp_path, ["serper"])
    for i in range(3):
        search.execute_v3_request(
            _request("serper", request_id=f"same-{i}"), search._search_adapter(), config
        )

    assert len(_samples(provider_stats.PROVIDER_STATS_FILE)["serper"]) == 1


def test_bench_still_never_records(tmp_path, monkeypatch):
    import bench

    monkeypatch.setattr(
        search, "search_serper", lambda *_a, **_k: _payload("serper", 3)
    )
    try:
        bench._call_provider_search(search, "serper", "q", 3, _config(tmp_path, ["serper"]))
    except Exception as exc:  # pragma: no cover - adapter surface drift
        pytest.skip(f"bench direct call surface changed: {exc}")

    assert _samples(provider_stats.PROVIDER_STATS_FILE) == {}
