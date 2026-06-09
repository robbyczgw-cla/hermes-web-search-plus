"""Rolling performance stats and adaptive routing adjustments."""

import os
import time
from unittest import mock

import pytest

import provider_stats
import routing


@pytest.fixture()
def stats_file(tmp_path, monkeypatch):
    path = tmp_path / "provider_stats.json"
    monkeypatch.setattr(provider_stats, "PROVIDER_STATS_FILE", path)
    return path


def _seed(provider, samples, latency, result_count, error=False, now=None):
    now = now if now is not None else time.time()
    for _ in range(samples):
        provider_stats.record_provider_outcome(
            provider, latency_seconds=latency, result_count=result_count, error=error, now=now,
        )


def test_rolling_window_keeps_most_recent_samples(stats_file):
    for i in range(provider_stats.MAX_SAMPLES_PER_PROVIDER + 10):
        provider_stats.record_provider_outcome("serper", latency_seconds=float(i), result_count=5, error=False)

    perf = provider_stats.get_provider_performance("serper")
    assert perf["samples"] == provider_stats.MAX_SAMPLES_PER_PROVIDER


def test_no_adjustment_below_min_samples(stats_file):
    _seed("serper", provider_stats.MIN_SAMPLES_FOR_ADJUSTMENT - 1, latency=0.5, result_count=5)

    assert provider_stats.performance_adjustment("serper") == 0.0


def test_stale_samples_are_ignored(stats_file):
    stale = time.time() - provider_stats.SAMPLE_MAX_AGE_SECONDS - 60
    _seed("serper", 20, latency=0.5, result_count=5, now=stale)

    assert provider_stats.get_provider_performance("serper") is None
    assert provider_stats.performance_adjustment("serper") == 0.0


def test_fast_reliable_provider_gets_bounded_positive_adjustment(stats_file):
    _seed("serper", 10, latency=0.4, result_count=8)

    adjustment = provider_stats.performance_adjustment("serper")
    assert 0.0 < adjustment <= provider_stats.MAX_SCORE_ADJUSTMENT


def test_slow_flaky_provider_gets_bounded_negative_adjustment(stats_file):
    _seed("brave", 5, latency=7.5, result_count=0)
    _seed("brave", 5, latency=7.5, result_count=0, error=True)

    adjustment = provider_stats.performance_adjustment("brave")
    assert -provider_stats.MAX_SCORE_ADJUSTMENT <= adjustment < 0.0


def test_performance_adjustments_omits_unknown_providers(stats_file):
    _seed("serper", 10, latency=0.4, result_count=8)

    adjustments = provider_stats.performance_adjustments(["serper", "tavily"])
    assert "serper" in adjustments
    assert "tavily" not in adjustments


def test_record_outcome_is_best_effort(stats_file, monkeypatch):
    def broken_save(state):
        raise IOError("disk full")

    monkeypatch.setattr(provider_stats, "_save_stats", broken_save)
    # Must not raise: stats persistence can never break a search.
    provider_stats.record_provider_outcome("serper", latency_seconds=0.5, result_count=5, error=False)


class TestAdaptiveRouting:
    QUERY = "find credible sources and citations to verify this claim"
    ENV = {"LINKUP_API_KEY": "linkup-test-key", "TAVILY_API_KEY": "tavily-test-key"}

    def test_adaptive_adjustments_blend_into_routing_scores(self, stats_file):
        _seed("tavily", 10, latency=0.4, result_count=8)
        _seed("linkup", 5, latency=7.5, result_count=0)
        _seed("linkup", 5, latency=7.5, result_count=0, error=True)

        config = {"auto_routing": {"provider_priority": ["linkup", "tavily"]}}
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            result = routing.auto_route_provider(self.QUERY, config)

        adjustments = result["adaptive_adjustments"]
        assert adjustments["tavily"] > 0.0
        assert adjustments["linkup"] < 0.0

    def test_adaptive_adjustment_can_flip_a_close_call(self, stats_file):
        config_off = {"auto_routing": {"provider_priority": ["linkup", "tavily"], "adaptive_routing": False}}
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            baseline = routing.auto_route_provider(self.QUERY, config_off)
        assert baseline["provider"] == "linkup"
        margin = baseline["scores"]["linkup"] - baseline["scores"].get("tavily", 0.0)

        config_on = {"auto_routing": {"provider_priority": ["linkup", "tavily"]}}
        with mock.patch.object(
            routing, "performance_adjustments",
            lambda providers, now=None: {"tavily": margin + 0.5},
        ):
            with mock.patch.dict(os.environ, self.ENV, clear=False):
                result = routing.auto_route_provider(self.QUERY, config_on)

        assert result["provider"] == "tavily"

    def test_adaptive_routing_can_be_disabled_via_config(self, stats_file):
        _seed("tavily", 10, latency=0.4, result_count=8)

        config = {"auto_routing": {"provider_priority": ["linkup", "tavily"], "adaptive_routing": False}}
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            result = routing.auto_route_provider(self.QUERY, config)

        assert result["adaptive_adjustments"] == {}

    def test_zero_signal_queries_are_not_steered_by_performance(self, stats_file):
        _seed("tavily", 10, latency=0.4, result_count=8)

        def must_not_be_called(providers, now=None):
            raise AssertionError("adaptive adjustments must not apply without query signals")

        config = {"auto_routing": {"provider_priority": ["linkup", "tavily"]}}
        with mock.patch.object(routing, "performance_adjustments", must_not_be_called):
            with mock.patch.dict(os.environ, self.ENV, clear=False):
                result = routing.auto_route_provider("zzqx", config)

        assert result["adaptive_adjustments"] == {}
