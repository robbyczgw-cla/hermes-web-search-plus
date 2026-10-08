from __future__ import annotations

import importlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from config import DEFAULT_CONFIG


FIXTURES = Path(__file__).parent / "fixtures" / "v3" / "ws3"
BENCHMARK_OWNER = "web-search-plus:operator-benchmarks-v3"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def journal_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "timestamp": record["timestamp"],
        "execution_id": record["execution_id"],
        "capability": record["capability"],
        "status": record["status"],
        "routing_receipt": record["routing"],
        "current_provider_attempts": record["current_provider_attempts"],
        "cache": record["cache"],
        "limits_applied": record["limits"],
        "warning_codes": record["warning_codes"],
        "error_code": record["error_code"],
    }


def test_receipt_builder_reads_owned_journal_and_projects_frozen_dto(
    tmp_path: Path,
) -> None:
    console = importlib.import_module("operator_console_v3")
    receipts = importlib.import_module("operator_receipts_v3")
    expected = fixture("receipts.json")
    journal = receipts.OperatorReceiptJournal(tmp_path, now=lambda: 1783890301.0)
    for record in reversed(expected["receipts"]):
        assert journal.append(journal_record(record)) is True

    assert console.build_receipts(cache_root=tmp_path, limit=100) == expected


def test_benchmark_history_reads_only_marker_owned_records(tmp_path: Path) -> None:
    console = importlib.import_module("operator_console_v3")
    expected = fixture("benchmark-history.json")
    history_path = tmp_path / "operator" / "v3" / "benchmark-history.jsonl"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        json.dumps(
            {
                "owner": BENCHMARK_OWNER,
                "history_schema_version": 1,
                "payload": expected["runs"][0],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert console.build_benchmark_history(cache_root=tmp_path, limit=100) == expected

    foreign = tmp_path / "foreign-history.jsonl"
    foreign.write_text(history_path.read_text(encoding="utf-8"), encoding="utf-8")
    history_path.unlink()
    history_path.symlink_to(foreign)
    before = foreign.read_bytes()
    assert console.build_benchmark_history(cache_root=tmp_path, limit=100) == {
        "schema_version": 1,
        "runs": [],
        "availability": {"search": "not_collected", "extract": "not_collected"},
    }
    assert foreign.read_bytes() == before


def test_overview_is_truthful_when_owned_state_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = importlib.import_module("operator_console_v3")
    config = deepcopy(DEFAULT_CONFIG)
    config["serper"]["api_key"] = "fixture-provider-key"
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    payload = console.build_overview(
        cache_root=tmp_path,
        config=config,
        provider_ids=["serper"],
        state_path=tmp_path / "missing-state.sqlite3",
        plugin_version="3.0.0-dev",
        now=lambda: 1783890400.0,
    )

    assert payload["schema_version"] == 1
    assert payload["engine"] == {
        "contract_version": "3.0",
        "plugin_version": "3.0.0-dev",
        "state_available": False,
    }
    assert payload["providers"] == [
        {
            "provider": "serper",
            "display_name": "Serper",
            "capabilities": ["search", "extract"],
            "configured": True,
            "key_present": True,
            "disabled": False,
            "auto_allowed": True,
            "cooldown_active": False,
        }
    ]
    assert payload["cache"] == {
        "response_entries": 0,
        "response_bytes": 0,
        "full_text_entries": 0,
        "full_text_bytes": 0,
        "oldest_timestamp": None,
        "newest_timestamp": None,
    }
    assert payload["circuits"] == {
        "closed": 0,
        "open": 0,
        "blocked_auth": 0,
        "blocked_quota": 0,
        "unknown": 0,
    }
    assert payload["receipts_summary"] == {"count": 0, "latest_timestamp": None}
    assert payload["benchmark_summary"] == {
        "count": 0,
        "latest_timestamp": None,
        "kinds": [],
        "extract_collected": False,
    }
    assert list(tmp_path.iterdir()) == [], "read-only snapshots must not create storage"
    assert console.serialize_endpoint_payload(payload).endswith(b"\n")


def test_overview_refuses_symlinked_cache_and_state_ancestors(
    tmp_path: Path,
) -> None:
    console = importlib.import_module("operator_console_v3")
    real_root = tmp_path / "foreign"
    response = real_root / "v3" / "response" / "entry.json"
    response.parent.mkdir(parents=True)
    response.write_text(
        json.dumps({"owner": "web-search-plus:v3", "created_at": 1783890000.0}),
        encoding="utf-8",
    )
    state_path = real_root / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute("CREATE TABLE circuit_state (state TEXT NOT NULL)")
        connection.execute("INSERT INTO circuit_state(state) VALUES ('open')")

    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    payload = console.build_overview(
        cache_root=linked_root,
        config={},
        provider_ids=[],
        state_path=linked_root / "state.sqlite3",
    )

    assert payload["engine"]["state_available"] is False
    assert payload["cache"]["response_entries"] == 0
    assert payload["circuits"]["open"] == 0


def _state_store_with_samples(tmp_path: Path, samples):
    state_store = importlib.import_module("state_store_v3")
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    state_store.initialize_state_schema(connection)
    connection.executemany(
        """
        INSERT INTO adaptive_samples_v3
            (provider, source_index, sample_time, latency_ms, result_count,
             error, source_digest, migrated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'digest', 0)
        """,
        samples,
    )
    connection.commit()
    connection.close()
    return state_store.SQLiteStateStore.open_readonly(db_path)


def test_provider_health_aggregates_daily_samples_from_state_store(
    tmp_path: Path,
) -> None:
    console = importlib.import_module("operator_console_v3")
    day = 86400
    store = _state_store_with_samples(
        tmp_path,
        [
            ("serper", 0, 10 * day + 100, 200, 5, 0),
            ("serper", 1, 10 * day + 200, 400, 3, 1),
            ("serper", 2, 11 * day + 100, 300, 4, 0),
            ("brave", 0, 11 * day + 100, 150, 6, 0),
        ],
    )

    payload = console.build_provider_health(store, days=7)

    assert payload == {
        "schema_version": 1,
        "days": 7,
        "buckets": [
            {
                "provider": "brave",
                "day": 11 * day,
                "samples": 1,
                "errors": 0,
                "result_count_total": 6,
                "median_latency_ms": 150,
                "error_rate": 0.0,
            },
            {
                "provider": "serper",
                "day": 10 * day,
                "samples": 2,
                "errors": 1,
                "result_count_total": 8,
                "median_latency_ms": 400,
                "error_rate": 0.5,
            },
            {
                "provider": "serper",
                "day": 11 * day,
                "samples": 1,
                "errors": 0,
                "result_count_total": 4,
                "median_latency_ms": 300,
                "error_rate": 0.0,
            },
        ],
    }


def test_provider_health_bounds_days_and_windows_from_newest_sample(
    tmp_path: Path,
) -> None:
    console = importlib.import_module("operator_console_v3")
    day = 86400
    store = _state_store_with_samples(
        tmp_path,
        [
            ("serper", 0, 1 * day, 100, 1, 0),
            ("serper", 1, 40 * day, 100, 1, 0),
        ],
    )

    payload = console.build_provider_health(store, days=99999)

    assert payload["days"] == console.PROVIDER_HEALTH_MAX_DAYS
    assert [bucket["day"] for bucket in payload["buckets"]] == [40 * day]


def test_provider_health_handles_missing_state_database(tmp_path: Path) -> None:
    console = importlib.import_module("operator_console_v3")
    state_store = importlib.import_module("state_store_v3")
    store = state_store.SQLiteStateStore.open_readonly(tmp_path / "absent.sqlite3")

    payload = console.build_provider_health(store, days=7)

    assert payload == {"schema_version": 1, "days": 7, "buckets": []}


def test_provider_health_includes_live_rolling_stats(tmp_path: Path) -> None:
    console = importlib.import_module("operator_console_v3")
    day = 86400
    store = _state_store_with_samples(
        tmp_path, [("serper", 0, 10 * day + 100, 200, 5, 0)]
    )
    stats_path = tmp_path / "provider_stats.json"
    stats_path.write_text(
        json.dumps(
            {
                "serper": [{"t": 10 * day + 500, "lat": 0.4, "n": 3, "err": True}],
                "brave": [{"t": 10 * day + 600, "lat": 0.15, "n": 6, "err": False}],
                "not-a-provider": [{"t": 10 * day, "lat": 1.0, "n": 1, "err": False}],
            }
        ),
        encoding="utf-8",
    )

    payload = console.build_provider_health(store, days=7, stats_path=stats_path)

    buckets = {(b["provider"], b["day"]): b for b in payload["buckets"]}
    serper = buckets[("serper", 10 * day)]
    assert serper["samples"] == 2
    assert serper["errors"] == 1
    assert serper["result_count_total"] == 8
    brave = buckets[("brave", 10 * day)]
    assert brave["samples"] == 1
    assert brave["median_latency_ms"] == 150
    assert ("not-a-provider", 10 * day) not in buckets
