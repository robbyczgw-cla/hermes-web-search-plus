"""Bench history coverage (persistence, CLI, and web UI panel).

Locks down the bench-history guarantees: every bench run appends one compact
JSONL record (opt-out via --no-history), history writing is best-effort and
never breaks a bench, readers skip corrupt lines and honour the limit, and the
read-only UI serves the records behind the same token auth as every other
endpoint. All providers are mocked; no network. The conftest autouse fixture
points bench.BENCH_HISTORY_FILE at a per-test tmp_path.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

import bench
import search

from test_bench import _clear_provider_env, _payload, _rich_results


def _run_mocked_bench(monkeypatch, **kwargs):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))
    return search.run_provider_bench({"auto_routing": {}}, **kwargs)


def _history_lines():
    return bench.BENCH_HISTORY_FILE.read_text(encoding="utf-8").splitlines()


def _record(timestamp, provider="you", score=0.9):
    return {
        "schema_version": bench.BENCH_HISTORY_SCHEMA_VERSION,
        "timestamp": timestamp,
        "ok": True,
        "providers": [
            {
                "provider": provider,
                "score": score,
                "success_rate": 1.0,
                "median_latency_seconds": 0.2,
                "error_count": 0,
            }
        ],
        "recommended_priority": [provider],
    }


def _write_history(records):
    lines = [json.dumps(record) for record in records]
    bench.BENCH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    bench.BENCH_HISTORY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----- persistence -----------------------------------------------------------


def test_run_bench_appends_compact_history_record(monkeypatch):
    report = _run_mocked_bench(monkeypatch)

    lines = _history_lines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema_version"] == bench.BENCH_HISTORY_SCHEMA_VERSION
    assert isinstance(record["timestamp"], float)
    assert record["ok"] is True
    assert record["recommended_priority"] == ["you"]
    (row,) = record["providers"]
    report_row = report["providers"][0]
    assert row == {
        "provider": "you",
        "score": report_row["score"],
        "success_rate": 1.0,
        "median_latency_seconds": report_row["median_latency_seconds"],
        "error_count": 0,
    }


def test_run_bench_record_history_false_writes_nothing(monkeypatch):
    _run_mocked_bench(monkeypatch, record_history=False)
    assert not bench.BENCH_HISTORY_FILE.exists()


def test_history_write_failure_never_breaks_the_bench(monkeypatch, tmp_path):
    # Parent path is a regular file, so mkdir/open must fail inside append.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(bench, "BENCH_HISTORY_FILE", blocker / "bench_history.jsonl")

    report = _run_mocked_bench(monkeypatch)

    assert report["ok"] is True
    assert bench.append_bench_history(report) is False


def test_repeated_runs_accumulate_records(monkeypatch):
    _run_mocked_bench(monkeypatch)
    _run_mocked_bench(monkeypatch)
    assert len(_history_lines()) == 2


# ----- reading ---------------------------------------------------------------


def test_load_bench_history_missing_file_returns_empty_list():
    assert bench.load_bench_history() == []


def test_load_bench_history_skips_corrupt_and_foreign_lines():
    _write_history([_record(100.0), _record(200.0)])
    with open(bench.BENCH_HISTORY_FILE, "a", encoding="utf-8") as handle:
        handle.write("{truncated json\n")
        handle.write("\n")
        handle.write('"a bare string, not a record"\n')
        handle.write('{"providers": "not-a-list"}\n')
        handle.write(json.dumps(_record(300.0)) + "\n")

    records = bench.load_bench_history()

    assert [record["timestamp"] for record in records] == [300.0, 200.0, 100.0]


def test_load_bench_history_returns_last_n_most_recent_first():
    _write_history([_record(float(idx)) for idx in range(30)])

    records = bench.load_bench_history(limit=5)

    assert [record["timestamp"] for record in records] == [29.0, 28.0, 27.0, 26.0, 25.0]
    assert len(bench.load_bench_history()) == bench.DEFAULT_BENCH_HISTORY_LIMIT


# ----- CLI -------------------------------------------------------------------


def _run_cli(monkeypatch, tmp_path, argv):
    _clear_provider_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"version": 1}))
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.setenv("YOU_API_KEY", "you-test-key")
    monkeypatch.setattr(search, "search_you", lambda **kw: _payload("you", _rich_results()))
    monkeypatch.setattr(sys, "argv", ["search.py"] + argv)
    search.main()


def test_cli_bench_records_history_and_no_history_opts_out(monkeypatch, tmp_path, capsys):
    _run_cli(monkeypatch, tmp_path, ["--bench", "--json"])
    assert len(_history_lines()) == 1

    _run_cli(monkeypatch, tmp_path, ["--bench", "--no-history", "--json"])
    assert len(_history_lines()) == 1  # unchanged


def test_cli_bench_history_prints_compact_table(monkeypatch, tmp_path, capsys):
    _run_cli(monkeypatch, tmp_path, ["--bench", "--json"])
    capsys.readouterr()

    _run_cli(monkeypatch, tmp_path, ["--bench-history"])

    stdout = capsys.readouterr().out
    assert "Web Search Plus Bench History" in stdout
    assert "you (" in stdout
    assert "ago" in stdout


def test_cli_bench_history_empty_state_points_at_bench(monkeypatch, tmp_path, capsys):
    _run_cli(monkeypatch, tmp_path, ["--bench-history"])
    stdout = capsys.readouterr().out
    assert "No bench runs yet" in stdout
    assert "python3 search.py --bench" in stdout


# ----- web UI ----------------------------------------------------------------

UI_PATH = Path(__file__).resolve().parents[1] / "ui.py"
ui_spec = importlib.util.spec_from_file_location("wsp_ui_bench_history_under_test", UI_PATH)
ui = importlib.util.module_from_spec(ui_spec)
assert ui_spec.loader is not None
ui_spec.loader.exec_module(ui)

TOKEN = "test-ui-token"


@pytest.fixture()
def ui_server():
    server = ui.create_server(port=0, token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(server, path, token=TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers = {"X-WSP-Token": token} if token is not None else {}
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response, raw


def test_ui_bench_history_requires_token(ui_server):
    response, _ = _get(ui_server, "/api/bench-history", token=None)
    assert response.status == 401
    response, _ = _get(ui_server, "/api/bench-history", token="wrong-token")
    assert response.status == 401


def test_ui_bench_history_returns_recorded_runs(ui_server):
    _write_history([_record(100.0, score=0.8), _record(200.0, score=0.9)])

    response, raw = _get(ui_server, "/api/bench-history")

    assert response.status == 200
    payload = json.loads(raw)
    assert [run["timestamp"] for run in payload["runs"]] == [200.0, 100.0]
    assert payload["runs"][0]["providers"][0]["score"] == 0.9


def test_ui_bench_history_empty_state_is_empty_list(ui_server):
    response, raw = _get(ui_server, "/api/bench-history")
    assert response.status == 200
    assert json.loads(raw) == {"runs": []}
