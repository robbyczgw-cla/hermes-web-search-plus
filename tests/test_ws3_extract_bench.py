"""WS-3 real extraction benchmark and typed operator-history gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import extract_bench_v3 as bench
import operator_console_v3 as console


URLS = ["https://example.com/docs", "https://example.org/release"]


def runtime_config() -> dict:
    return {
        "auto_routing": {
            "disabled_providers": ["exa"],
            "extract_provider_priority": ["linkup", "tavily", "exa"],
        },
        "linkup": {"api_key": "linkup-secret", "fetch_url": "https://fetch.example/v1"},
        "tavily": {"api_key": "tavily-secret"},
        "exa": {"api_key": "exa-secret"},
    }


def test_extract_bench_eligible_providers_are_configured_enabled_and_extract_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from provider_registry import PROVIDER_SPECS

    for spec in PROVIDER_SPECS.values():
        monkeypatch.delenv(spec.env_var, raising=False)
    assert bench.extract_bench_eligible_providers(runtime_config()) == ["linkup", "tavily"]


def test_run_extract_bench_calls_direct_adapters_and_isolates_failures() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def extract_linkup(urls, api_key, *_args, **kwargs):
        assert api_key == "linkup-secret"
        assert kwargs["api_url"] == "https://fetch.example/v1"
        calls.append(("linkup", tuple(urls)))
        return {
            "provider": "linkup",
            "results": [
                {"url": urls[0], "content": "a" * 800},
                {"url": urls[1], "content": "b" * 400},
            ],
        }

    def extract_tavily(*_args, **_kwargs):
        calls.append(("tavily", tuple(URLS)))
        raise RuntimeError("provider exploded at https://secret.invalid with tavily-secret")

    fake = SimpleNamespace(extract_linkup=extract_linkup, extract_tavily=extract_tavily)
    ticks = iter([10.0, 10.4, 20.0, 20.3])
    report = bench.run_extract_bench(
        runtime_config(),
        urls=URLS,
        providers=["linkup", "tavily"],
        extract_module=fake,
        monotonic=lambda: next(ticks),
        generated_at=1_720_000_000.0,
    )

    assert calls == [("linkup", tuple(URLS)), ("tavily", tuple(URLS))]
    assert report["kind"] == "extract"
    assert report["ok"] is True
    assert report["url_count"] == 2
    assert report["recommended_priority"] == ["linkup", "tavily"]
    by_provider = {row["provider"]: row for row in report["providers"]}
    assert by_provider["linkup"]["success_rate"] == 1.0
    assert by_provider["linkup"]["returned_character_count"] == 1200
    assert by_provider["linkup"]["median_latency_seconds"] == 0.4
    assert by_provider["tavily"]["success_rate"] == 0.0
    assert by_provider["tavily"]["error_count"] == 2
    assert by_provider["tavily"]["error_codes"] == ["provider_error"]

    encoded = json.dumps(report, sort_keys=True)
    for forbidden in [*URLS, "linkup-secret", "tavily-secret", "secret.invalid", "a" * 200]:
        assert forbidden not in encoded


def test_extract_bench_treats_per_url_errors_and_empty_content_truthfully() -> None:
    def extract_linkup(urls, *_args, **_kwargs):
        return {
            "provider": "linkup",
            "results": [
                {"url": urls[0], "content": ""},
                {"url": urls[1], "content": "", "error": "blocked"},
            ],
        }

    report = bench.run_extract_bench(
        runtime_config(),
        urls=URLS,
        providers=["linkup"],
        extract_module=SimpleNamespace(extract_linkup=extract_linkup),
        monotonic=iter([1.0, 1.1]).__next__,
        generated_at=123.0,
    )
    row = report["providers"][0]
    assert row["success_count"] == 0
    assert row["success_rate"] == 0.0
    assert row["error_count"] == 2
    assert row["error_codes"] == ["empty_content", "url_error"]
    assert report["ok"] is False


def test_safe_provider_error_codes_do_not_retain_exception_text() -> None:
    from http_client import ProviderRequestError

    cases = [
        (ProviderRequestError("secret", status_code=401), "auth_error"),
        (ProviderRequestError("secret", status_code=429, transient=True), "rate_limited"),
        (ProviderRequestError("secret", status_code=503, transient=True), "provider_unavailable"),
        (ProviderRequestError("secret", transient=True), "transient_provider_error"),
        (TimeoutError("secret"), "timeout"),
        (RuntimeError("secret"), "provider_error"),
    ]
    assert [bench._safe_provider_error_code(exc) for exc, _code in cases] == [
        code for _exc, code in cases
    ]


def test_history_record_is_compact_typed_and_privacy_safe() -> None:
    report = {
        "kind": "extract",
        "generated_at": 123.5,
        "ok": True,
        "recommended_priority": ["linkup"],
        "providers": [
            {
                "provider": "linkup",
                "score": 0.91,
                "success_rate": 1.0,
                "median_latency_seconds": 0.4,
                "error_count": 0,
                "returned_character_count": 9999,
                "error_codes": [],
            }
        ],
    }
    record = bench.benchmark_history_record(report)
    assert record == {
        "schema_version": 1,
        "kind": "extract",
        "timestamp": 123.5,
        "ok": True,
        "providers": [
            {
                "provider": "linkup",
                "score": 0.91,
                "success_rate": 1.0,
                "median_latency_seconds": 0.4,
                "error_count": 0,
            }
        ],
        "recommended_priority": ["linkup"],
    }


def test_history_journal_round_trips_through_console_reader(tmp_path: Path) -> None:
    journal = bench.BenchmarkHistoryJournal(tmp_path, now=lambda: 200.0)
    record = {
        "schema_version": 1,
        "kind": "extract",
        "timestamp": 200.0,
        "ok": True,
        "providers": [
            {
                "provider": "linkup",
                "score": 0.8,
                "success_rate": 1.0,
                "median_latency_seconds": 0.25,
                "error_count": 0,
            }
        ],
        "recommended_priority": ["linkup"],
    }
    assert journal.append(record) is True

    payload = console.build_benchmark_history(cache_root=tmp_path)
    assert payload["availability"]["extract"] == "collected"
    assert payload["availability"]["search"] == "not_collected"
    assert payload["runs"][0]["kind"] == "extract"
    assert payload["runs"][0]["providers"][0]["provider"] == "linkup"

    history = tmp_path / "operator" / "v3" / "benchmark-history.jsonl"
    envelope = json.loads(history.read_text(encoding="utf-8"))
    assert envelope["owner"] == console.BENCHMARK_OWNER
    assert envelope["history_schema_version"] == console.BENCHMARK_HISTORY_SCHEMA_VERSION
    assert history.stat().st_mode & 0o077 == 0


def test_history_journal_refuses_symlinked_storage(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    operator = tmp_path / "operator"
    operator.symlink_to(outside, target_is_directory=True)
    journal = bench.BenchmarkHistoryJournal(tmp_path)
    assert journal.append(
        {
            "schema_version": 1,
            "kind": "extract",
            "timestamp": 1.0,
            "ok": False,
            "providers": [],
            "recommended_priority": [],
        }
    ) is False
    assert list(outside.iterdir()) == []


def test_format_extract_bench_text_has_no_targets_or_content() -> None:
    report = {
        "ok": True,
        "url_count": 2,
        "providers": [
            {
                "provider": "linkup",
                "score": 0.9,
                "success_count": 2,
                "url_count": 2,
                "success_rate": 1.0,
                "median_latency_seconds": 0.2,
                "returned_character_count": 1000,
                "error_count": 0,
                "error_codes": [],
            }
        ],
        "recommended_priority": ["linkup"],
        "skipped_providers": [],
    }
    text = bench.format_extract_bench_text(report)
    assert "linkup" in text
    assert "2/2" in text
    assert "https://" not in text


def test_cli_exposes_explicit_extract_bench_without_running_on_plain_extract() -> None:
    import search

    parser = search.build_parser(runtime_config())
    args = parser.parse_args(
        [
            "extract-bench",
            "--extract-urls",
            URLS[0],
            "--bench-providers",
            "linkup",
            "tavily",
            "--no-history",
        ]
    )
    assert args.command == "extract-bench"
    assert args.extract_urls == [URLS[0]]
    assert args.bench_providers == ["linkup", "tavily"]
    assert args.no_history is True

    plain = parser.parse_args(["--extract-urls", URLS[0], "--provider", "linkup"])
    assert plain.command is None
    assert plain.no_history is False


def test_cli_extract_bench_persists_history_and_no_history_opts_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import search

    def fake_report(*_args, **_kwargs):
        return {
            "schema_version": 1,
            "kind": "extract",
            "generated_at": 321.0,
            "ok": True,
            "url_count": 1,
            "providers": [
                {
                    "provider": "linkup",
                    "score": 0.9,
                    "url_count": 1,
                    "success_count": 1,
                    "success_rate": 1.0,
                    "median_latency_seconds": 0.2,
                    "elapsed_seconds": 0.2,
                    "returned_character_count": 900,
                    "error_count": 0,
                    "error_codes": [],
                }
            ],
            "skipped_providers": [],
            "recommended_priority": ["linkup"],
        }

    monkeypatch.setattr(search, "load_config", runtime_config)
    monkeypatch.setattr(search._extract_bench, "run_extract_bench", fake_report)
    monkeypatch.setattr(search, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        search.sys,
        "argv",
        ["search.py", "extract-bench", "--extract-urls", URLS[0], "--json"],
    )
    search.main()
    output = json.loads(capsys.readouterr().out)
    assert output["history_written"] is True
    assert console.build_benchmark_history(cache_root=tmp_path)["availability"]["extract"] == "collected"

    no_history_root = tmp_path / "opt-out"
    monkeypatch.setattr(search, "CACHE_DIR", no_history_root)
    monkeypatch.setattr(
        search.sys,
        "argv",
        [
            "search.py",
            "extract-bench",
            "--extract-urls",
            URLS[0],
            "--no-history",
            "--json",
        ],
    )
    search.main()
    output = json.loads(capsys.readouterr().out)
    assert output["history_written"] is False
    assert not (no_history_root / "operator").exists()
