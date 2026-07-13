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
