"""Provider bench (bakeoff) coverage.

Locks down the guarantees the bench feature makes: ranking follows the
success/latency/quality score, a failing provider never aborts the run (it
just ranks last), the recommendation is advisory (config_key + apply hint,
never a config write), and bench traffic never touches provider_health
cooldowns or provider_stats adaptive-routing memory. All providers are mocked;
no network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path

import bench
import provider_health
import provider_stats
import search


PROVIDER_ENV_VARS = [
    "SERPER_API_KEY",
    "SERPBASE_API_KEY",
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "QUERIT_API_KEY",
    "LINKUP_API_KEY",
    "EXA_API_KEY",
    "YOU_API_KEY",
    "PARALLEL_API_KEY",
    "PERPLEXITY_API_KEY",
    "KILOCODE_API_KEY",
    "FIRECRAWL_API_KEY",
    "KEENABLE_API_KEY",
    "KEENABLE_ALLOW_PUBLIC",
    "SEARXNG_INSTANCE_URL",
]


def _clear_provider_env(monkeypatch):
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("WEB_SEARCH_PLUS_CONFIG", raising=False)


def _payload(provider, results):
    return {
        "provider": provider,
        "query": "q",
        "results": results,
        "images": [],
        "answer": "",
        "metadata": {},
    }


def _rich_results(count=3, domain="example.test"):
    return [
        {
            "url": "https://{}/page-{}".format(domain, idx),
            "title": "Title {}".format(idx),
            "snippet": "a substantial informative snippet with plenty of characters " * 2,
        }
        for idx in range(count)
    ]


def _thin_duplicate_results():
    return [
        {"url": "https://dupe.test/a", "title": "A", "snippet": ""},
        {"url": "https://dupe.test/a", "title": "A again", "snippet": ""},
        {"url": "https://dupe.test/b", "title": "B", "snippet": "x"},
    ]


def test_bench_ranks_higher_quality_provider_first(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))
    monkeypatch.setattr(search, "search_serper", lambda **kw: _payload("serper", _thin_duplicate_results()))

    report = search.run_provider_bench({"auto_routing": {}})

    ranked = [row["provider"] for row in report["providers"]]
    assert ranked == ["you", "serper"]
    assert report["ok"] is True
    rows = {row["provider"]: row for row in report["providers"]}
    assert rows["you"]["score"] > rows["serper"]["score"]
    assert rows["you"]["success_rate"] == 1.0
    assert rows["you"]["unique_url_ratio"] == 1.0
    assert rows["you"]["snippet_coverage"] == 1.0
    # Serper's fixture has one duplicate URL and only thin snippets.
    assert rows["serper"]["unique_url_ratio"] < 1.0
    assert rows["serper"]["snippet_coverage"] == 0.0
    assert report["recommendation"]["provider_priority"] == ["you", "serper"]
    # Every configured provider ran the whole suite.
    assert rows["you"]["query_count"] == len(bench.BENCH_QUERIES)
    assert 0.0 <= rows["serper"]["score"] <= rows["you"]["score"] <= 1.0


def test_bench_survives_provider_errors_and_ranks_failures_last(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))

    def broken_serper(**kwargs):
        raise search.ProviderRequestError("HTTP 500: upstream exploded", status_code=500, transient=True)

    monkeypatch.setattr(search, "search_serper", broken_serper)

    report = search.run_provider_bench({"auto_routing": {}})

    ranked = [row["provider"] for row in report["providers"]]
    assert ranked == ["you", "serper"]
    rows = {row["provider"]: row for row in report["providers"]}
    assert rows["serper"]["success_rate"] == 0.0
    assert rows["serper"]["success_count"] == 0
    assert rows["serper"]["median_latency_seconds"] is None
    assert len(rows["serper"]["errors"]) == len(bench.BENCH_QUERIES)
    assert "HTTP 500" in rows["serper"]["errors"][0]["error"]
    # The healthy provider still makes the whole report usable.
    assert report["ok"] is True


def test_bench_never_touches_provider_health_or_stats(monkeypatch, tmp_path):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key")
    calls = []
    for module, name in (
        (provider_health, "mark_provider_failure"),
        (provider_health, "reset_provider_health"),
        (provider_stats, "record_provider_outcome"),
        (search, "mark_provider_failure"),
        (search, "reset_provider_health"),
        (search, "record_provider_outcome"),
    ):
        monkeypatch.setattr(module, name, lambda *a, _name=name, **k: calls.append(_name))
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))

    def broken_serper(**kwargs):
        raise search.ProviderRequestError("HTTP 429: slow down", status_code=429, retry_after=60)

    monkeypatch.setattr(search, "search_serper", broken_serper)

    report = search.run_provider_bench({"auto_routing": {}})

    assert report["ok"] is True
    assert calls == []
    # conftest points PROVIDER_STATS_FILE at tmp_path; a clean bench leaves it unwritten.
    assert not provider_stats.PROVIDER_STATS_FILE.exists()


def test_bench_recommendation_is_advisory_with_apply_hint(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))
    monkeypatch.setattr(search, "search_serper", lambda **kw: _payload("serper", _thin_duplicate_results()))

    report = search.run_provider_bench({"auto_routing": {"provider_priority": ["serper", "you"]}})

    recommendation = report["recommendation"]
    assert recommendation["config_key"] == "auto_routing.provider_priority"
    assert recommendation["provider_priority"] == ["you", "serper"]
    assert recommendation["current_provider_priority"] == ["serper", "you"]
    assert recommendation["apply_hint"].endswith("config set-priority you,serper")
    assert "no configuration was written" in recommendation["note"].lower()

    text = bench.format_bench_text(report)
    assert "Web Search Plus Bench" in text
    assert "Recommended auto_routing.provider_priority:" in text
    assert "you, serper" in text
    assert "config set-priority you,serper" in text


def test_bench_skips_disabled_and_unknown_providers(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))

    report = search.run_provider_bench({"auto_routing": {"disabled_providers": ["serper"]}})
    assert [row["provider"] for row in report["providers"]] == ["you"]

    explicit = search.run_provider_bench({"auto_routing": {}}, providers=["you", "not-a-provider"])
    assert [row["provider"] for row in explicit["providers"]] == ["you"]
    assert explicit["skipped_providers"] == [
        {"provider": "not-a-provider", "reason": "unknown_or_not_search_capable"}
    ]


def test_bench_time_budget_skips_remaining_providers(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))

    report = search.run_provider_bench({"auto_routing": {}}, timeout_budget=0)

    assert report["providers"] == []
    assert report["ok"] is False
    assert {"provider": "you", "reason": "time_budget_exhausted"} in report["skipped_providers"]
    assert "configure at least one search provider" in report["recommendation"]["apply_hint"]


def test_compute_provider_score_orders_fast_reliable_quality():
    fast, fast_components = bench.compute_provider_score(1.0, 0.4, 1.0, 1.0)
    slow, _ = bench.compute_provider_score(1.0, 7.9, 1.0, 1.0)
    flaky, _ = bench.compute_provider_score(0.5, 0.4, 1.0, 1.0)
    failing, _ = bench.compute_provider_score(0.0, None, 0.0, 0.0)

    assert fast > slow > failing
    assert fast > flaky > failing
    assert failing == 0.0
    assert 0.0 < fast <= 1.0
    assert set(fast_components) == {"success_rate", "latency", "quality"}


def test_cli_bench_flag_emits_json_report(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))
    monkeypatch.setattr(sys, "argv", ["search.py", "--bench", "--json"])

    search.main()

    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["recommendation"]["provider_priority"] == ["you"]


def test_cli_bench_command_prints_human_readable_table(monkeypatch, tmp_path, capsys):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))
    monkeypatch.setattr(sys, "argv", ["search.py", "bench"])

    search.main()

    stdout = capsys.readouterr().out
    assert "Web Search Plus Bench" in stdout
    assert "you" in stdout
    assert "Apply with:" in stdout


def test_setup_cli_bench_dispatches_through_in_process_engine(monkeypatch, capsys):
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("wsp_plugin_bench_under_test", plugin_path)
    wsp = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(wsp)

    stub_engine = types.SimpleNamespace(
        load_config=lambda: {"auto_routing": {}},
        run_provider_bench=lambda config, **kwargs: {"canned": True},
        format_bench_text=lambda report: "Web Search Plus Bench (stub)",
    )
    monkeypatch.setattr(wsp, "_load_search_module", lambda: stub_engine)

    parser = argparse.ArgumentParser()
    wsp._web_search_plus_cli_setup(parser)
    args = parser.parse_args(["bench", "--json"])
    args.func(args)
    assert json.loads(capsys.readouterr().out) == {"canned": True}

    args = parser.parse_args(["bench"])
    args.func(args)
    assert "Web Search Plus Bench (stub)" in capsys.readouterr().out
