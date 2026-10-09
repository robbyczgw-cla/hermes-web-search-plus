"""Rolling provider latency stats: window, freshness, latency quantile, locking."""

import time

import pytest

from wsp_core import provider_stats


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


def test_stale_samples_are_ignored(stats_file):
    stale = time.time() - provider_stats.SAMPLE_MAX_AGE_SECONDS - 60
    _seed("serper", 20, latency=0.5, result_count=5, now=stale)

    assert provider_stats.get_provider_performance("serper") is None
    assert provider_stats.latency_quantile("serper") is None


def test_performance_summary_reports_rates_and_median_latency(stats_file):
    _seed("serper", 6, latency=0.4, result_count=8)
    _seed("serper", 2, latency=0.8, result_count=0)
    _seed("serper", 2, latency=0.0, result_count=0, error=True)

    perf = provider_stats.get_provider_performance("serper")

    assert perf["samples"] == 10
    assert perf["success_rate"] == 0.8
    assert perf["empty_rate"] == 0.25
    assert perf["median_latency_seconds"] == 0.4


def test_latency_quantile_needs_enough_fresh_successful_samples(stats_file):
    _seed("serper", provider_stats.MIN_SAMPLES_FOR_ADJUSTMENT - 1, latency=0.5, result_count=5)
    _seed("serper", 10, latency=9.0, result_count=0, error=True)

    assert provider_stats.latency_quantile("serper") is None
    assert provider_stats.latency_quantile("tavily") is None


def test_latency_quantile_ignores_errors_and_returns_the_requested_share(stats_file):
    for latency in (1.0, 2.0, 3.0, 4.0, 5.0):
        _seed("serper", 1, latency=latency, result_count=5)
    _seed("serper", 3, latency=60.0, result_count=0, error=True)

    assert provider_stats.latency_quantile("serper", 0.0) == 1.0
    assert provider_stats.latency_quantile("serper", 0.5) == 3.0
    assert provider_stats.latency_quantile("serper") == 4.0
    assert provider_stats.latency_quantile("serper", 1.0) == 5.0


def test_record_outcome_is_best_effort(stats_file, monkeypatch):
    def broken_save(state):
        raise IOError("disk full")

    monkeypatch.setattr(provider_stats, "_save_stats", broken_save)
    # Must not raise: stats persistence can never break a search.
    provider_stats.record_provider_outcome("serper", latency_seconds=0.5, result_count=5, error=False)


def _record_many(stats_file, count):
    from wsp_core import provider_stats as ps

    ps.PROVIDER_STATS_FILE = stats_file
    for _ in range(count):
        ps.record_provider_outcome("serper", latency_seconds=0.1, result_count=1, error=False)


def test_concurrent_processes_do_not_lose_samples(tmp_path, monkeypatch):
    import json
    import multiprocessing

    from wsp_core import provider_stats as ps

    if getattr(ps, "fcntl", object()) is None:  # pragma: no cover - Windows
        pytest.skip("cross-process lock needs fcntl")
    stats_file = tmp_path / "provider_stats.json"
    monkeypatch.setattr(ps, "PROVIDER_STATS_FILE", stats_file)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_record_many, args=(stats_file, 10)) for _ in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(30)
        assert proc.exitcode == 0

    samples = json.loads(stats_file.read_text(encoding="utf-8"))["serper"]
    assert len(samples) == 40
