from __future__ import annotations

import importlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "v3" / "ws3"
FIXTURE_NAMES = ("overview.json", "receipts.json", "benchmark-history.json")

FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "query",
        "url",
        "urls",
        "title",
        "text",
        "snippet",
        "content",
        "fulltext",
        "full_text",
        "api_key",
        "secret",
        "token",
        "authorization",
        "headers",
        "credential_fingerprint",
        "credential_slot",
        "endpoint_url",
        "path",
        "file",
        "filename",
        "cache_dir",
        "state_path",
    }
)
CANDIDATE_REASON_CODES = frozenset(
    {
        "classic_selected",
        "fallback_selected",
        "attempt_failed",
        "insufficient_results",
        "blocked_auth",
        "blocked_quota",
        "circuit_open",
        "budget_denied",
        "provider_unavailable",
        "not_attempted_after_success",
        "cache_origin_selected",
    }
)
ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_values() -> list[dict[str, Any]]:
    return [load_fixture(name) for name in FIXTURE_NAMES]


def assert_fixture_payload_safe(value: Any, location: str = "$") -> None:
    """Task-1 oracle only; production must use one shared fail-closed validator."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            assert normalized not in FORBIDDEN_FIELD_NAMES, f"{location}.{key} is forbidden"
            assert_fixture_payload_safe(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_fixture_payload_safe(child, f"{location}[{index}]")
    elif isinstance(value, str):
        assert not ABSOLUTE_PATH.match(value), f"{location} leaks an absolute path"
        assert "Bearer " not in value and "Basic " not in value


def test_all_three_endpoint_fixtures_are_recursively_secret_free() -> None:
    for payload in fixture_values():
        assert_fixture_payload_safe(payload)


def test_receipt_fixture_uses_only_closed_reason_codes() -> None:
    payload = load_fixture("receipts.json")
    seen: set[str] = set()
    for record in payload["receipts"]:
        routing = record["routing"]
        for decision in routing["candidate_decisions"]:
            seen.add(decision["reason_code"])
        origin = routing["cache_origin"]
        if origin:
            for decision in origin["candidate_decisions"]:
                seen.add(decision["reason_code"])
    assert seen
    assert seen <= CANDIDATE_REASON_CODES


def test_cache_hit_separates_origin_from_current_execution() -> None:
    cache_hit = load_fixture("receipts.json")["receipts"][0]
    routing = cache_hit["routing"]

    assert cache_hit["cache"]["disposition"] == "fresh_hit"
    assert cache_hit["current_provider_attempts"] == []
    assert routing["execution_scope"] == "current"
    assert routing["candidate_order"] == []
    assert routing["selected_provider"] is None
    assert routing["candidate_decisions"] == []
    assert routing["cache_origin"]["execution_id"] == "exec_origin"
    assert routing["cache_origin"]["selected_provider"] == "serper"
    assert routing["cache_origin"]["candidate_decisions"][0]["attempt_id"] is None


def test_shadow_observation_cannot_affect_execution() -> None:
    fallback = load_fixture("receipts.json")["receipts"][1]
    shadow = fallback["routing"]["shadow_observation"]
    assert shadow["observed"] is True
    assert shadow["affected_execution"] is False
    assert shadow["selected_provider"] != fallback["routing"]["selected_provider"]


def test_one_production_privacy_choke_point_guards_endpoints_and_journal() -> None:
    privacy = importlib.import_module("operator_privacy_v3")
    validator = privacy.assert_operator_payload_safe

    overview, receipts, history = fixture_values()
    journal_record = receipts["receipts"][0]
    for payload in (overview, receipts, history, journal_record):
        assert validator(payload) is None

    forbidden_key = "que" + "ry"
    with pytest.raises(ValueError, match="forbidden"):
        validator({"safe": {forbidden_key: "private terms"}})


def test_all_endpoint_serializers_and_journal_share_the_same_choke_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    privacy = importlib.import_module("operator_privacy_v3")
    console = importlib.import_module("operator_console_v3")
    journal = importlib.import_module("operator_receipts_v3")
    calls: list[int] = []

    def record_shared_validation(payload: Any) -> None:
        calls.append(id(payload))

    monkeypatch.setattr(privacy, "assert_operator_payload_safe", record_shared_validation)
    overview, receipts, history = fixture_values()
    journal_record = receipts["receipts"][0]

    console.serialize_endpoint_payload(overview)
    console.serialize_endpoint_payload(receipts)
    console.serialize_endpoint_payload(history)
    journal.encode_journal_record(journal_record)

    assert calls == [
        id(overview),
        id(receipts),
        id(history),
        id(journal_record),
    ]


def test_routing_receipt_contract_exports_closed_typed_reason_enum() -> None:
    contract = importlib.import_module("contract_v3")
    reason_enum = contract.CandidateReasonCode
    assert {member.value for member in reason_enum} == CANDIDATE_REASON_CODES
    assert callable(contract.validate_routing_receipt_v3)


def test_operator_snapshot_builders_match_all_frozen_dtos(tmp_path: Path) -> None:
    console = importlib.import_module("operator_console_v3")
    assert console.build_overview(cache_root=tmp_path) == load_fixture("overview.json")
    assert console.build_receipts(cache_root=tmp_path, limit=100) == load_fixture(
        "receipts.json"
    )
    assert console.build_benchmark_history(cache_root=tmp_path, limit=100) == load_fixture(
        "benchmark-history.json"
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "100.100.100.100"])
def test_ui_server_refuses_every_non_loopback_bind(host: str) -> None:
    ui_path = ROOT / "ui.py"
    assert ui_path.exists(), "WS-3 UI server is intentionally not implemented yet"
    spec = importlib.util.spec_from_file_location("wsp_ws3_ui", ui_path)
    assert spec is not None and spec.loader is not None
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    with pytest.raises(ValueError, match="127.0.0.1"):
        ui.create_server(host=host, port=0, token="task-1-test-token")
