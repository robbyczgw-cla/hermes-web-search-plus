from __future__ import annotations

import importlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from contract_v3 import (
    AttemptOutcome,
    Capability,
    ErrorClass,
    ErrorV3,
    ProviderAttemptV3,
    ResponseStatus,
    ResponseV3,
    SkipReason,
)
from compat_v3 import legacy_request_to_v3
from orchestrator_v3 import CapabilityAdapter, ProviderPlan, execute_v3_request


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "v3" / "ws3"
REASON_CODES = {
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


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def append_journal_in_process(args: tuple[str, int, dict[str, Any]]) -> bool:
    root, index, source = args
    fixture_now = float(source["timestamp"]) + 1.0
    journal_module = importlib.import_module("operator_receipts_v3")
    journal = journal_module.OperatorReceiptJournal(
        root,
        max_records=100,
        max_bytes=1_000_000,
        now=lambda: fixture_now,
    )
    return journal.append(dict(source, execution_id=f"exec_{index:032x}"))


def attempt(
    attempt_id: str,
    provider: str,
    outcome: AttemptOutcome,
    *,
    skip_reason: SkipReason | None = None,
) -> ProviderAttemptV3:
    return ProviderAttemptV3(
        attempt_id=attempt_id,
        provider=provider,
        capability=Capability.SEARCH,
        outcome=outcome,
        error=(
            ErrorV3(
                error_class=ErrorClass.TRANSIENT,
                code="wsp.provider.error",
                message="fixture failure",
                retryable=True,
            )
            if outcome is AttemptOutcome.FAILED
            else None
        ),
        skip_reason=skip_reason,
        decision="skipped" if outcome is AttemptOutcome.SKIPPED else "attempted",
        endpoint_id="endpoint-public-id" if outcome is not AttemptOutcome.SKIPPED else "",
        tries=(
            []
            if outcome is AttemptOutcome.SKIPPED
            else [
                {
                    "try_number": 1,
                    "outcome": "success" if outcome is AttemptOutcome.SUCCESS else "error",
                    "error": None
                    if outcome is AttemptOutcome.SUCCESS
                    else {
                        "error_class": "transient",
                        "code": "wsp.provider.error",
                        "message": "fixture failure",
                        "retryable": True,
                    },
                }
            ]
        ),
    )


def base_receipt(order: list[str], selected: str | None, fallback: str = "none") -> dict[str, Any]:
    return {
        "policy_id": "classic",
        "policy_revision": "v2.9.1",
        "mode": "classic",
        "candidate_order": order,
        "selected_provider": selected,
        "fallback_reason": fallback,
    }


def test_complete_direct_receipt_selects_first_candidate() -> None:
    contract = importlib.import_module("contract_v3")
    receipt = contract.complete_routing_receipt_v3(
        base_receipt(["serper", "linkup"], "serper"),
        [attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
    )

    assert receipt["authority"] == "classic"
    assert receipt["execution_scope"] == "current"
    assert receipt["candidate_decisions"] == [
        {
            "provider": "serper",
            "position": 1,
            "decision": "selected",
            "reason_code": "classic_selected",
            "attempt_id": "attempt_serper",
        },
        {
            "provider": "linkup",
            "position": 2,
            "decision": "not_attempted",
            "reason_code": "not_attempted_after_success",
            "attempt_id": None,
        },
    ]
    contract.validate_routing_receipt_v3(receipt, require_completed=True)


def test_complete_fallback_receipt_preserves_attempt_order() -> None:
    contract = importlib.import_module("contract_v3")
    receipt = contract.complete_routing_receipt_v3(
        base_receipt(["tavily", "serper"], "serper", "selected_failed"),
        [
            attempt("attempt_tavily", "tavily", AttemptOutcome.FAILED),
            attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS),
        ],
    )

    assert [item["reason_code"] for item in receipt["candidate_decisions"]] == [
        "attempt_failed",
        "fallback_selected",
    ]
    assert [item["attempt_id"] for item in receipt["candidate_decisions"]] == [
        "attempt_tavily",
        "attempt_serper",
    ]
    contract.validate_routing_receipt_v3(receipt, require_completed=True)


@pytest.mark.parametrize(
    ("skip_reason", "reason_code"),
    [
        (SkipReason.AUTH_BLOCKED, "blocked_auth"),
        (SkipReason.QUOTA_BLOCKED, "blocked_quota"),
        (SkipReason.CIRCUIT_OPEN, "circuit_open"),
        (SkipReason.BUDGET_BLOCKED, "budget_denied"),
        (SkipReason.NOT_CONFIGURED, "provider_unavailable"),
    ],
)
def test_complete_receipt_maps_typed_skip_reasons(
    skip_reason: SkipReason, reason_code: str
) -> None:
    contract = importlib.import_module("contract_v3")
    receipt = contract.complete_routing_receipt_v3(
        base_receipt(["tavily", "serper"], "serper", "selected_skipped"),
        [
            attempt(
                "attempt_tavily",
                "tavily",
                AttemptOutcome.SKIPPED,
                skip_reason=skip_reason,
            ),
            attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS),
        ],
    )
    assert receipt["candidate_decisions"][0] == {
        "provider": "tavily",
        "position": 1,
        "decision": "skipped",
        "reason_code": reason_code,
        "attempt_id": "attempt_tavily",
    }


def test_generated_schema_keeps_legacy_receipt_and_rejects_partial_completion() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "schemas" / "v3" / "response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    routing_schema = {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        **schema["$defs"]["RoutingReceipt"],
    }
    legacy = base_receipt(["serper"], "serper")
    completed = importlib.import_module("contract_v3").complete_routing_receipt_v3(
        legacy,
        [attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
    )
    jsonschema.validate(legacy, routing_schema)
    jsonschema.validate(completed, routing_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**legacy, "authority": "classic"}, routing_schema)
    impossible = json.loads(json.dumps(completed))
    impossible["candidate_decisions"][0]["reason_code"] = "blocked_auth"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(impossible, routing_schema)


def test_cache_hit_receipt_keeps_origin_separate_from_current_attempts() -> None:
    cache = importlib.import_module("cache_v3")
    origin = importlib.import_module("contract_v3").complete_routing_receipt_v3(
        base_receipt(["serper"], "serper"),
        [attempt("attempt_origin", "serper", AttemptOutcome.SUCCESS)],
    )
    material = {
        "origin_execution_id": "exec_origin",
        "capability": "search",
        "status": "ok",
        "routing_receipt": origin,
    }

    payload = cache.response_payload_from_cache_material(
        material,
        request_id="req_current",
        execution_id="exec_current",
        disposition="fresh_hit",
        entry_id="search_fixture",
        age_seconds=10,
        ttl_seconds=3600,
    )

    assert payload["provider_attempts"] == []
    routing = payload["routing_receipt"]
    assert routing["execution_scope"] == "current"
    assert routing["candidate_order"] == []
    assert routing["selected_provider"] is None
    assert routing["candidate_decisions"] == []
    assert routing["cache_origin"]["execution_id"] == "exec_origin"
    assert routing["cache_origin"]["candidate_decisions"][0]["attempt_id"] is None


def test_shadow_observation_is_typed_journaled_and_never_affects_execution(
    tmp_path: Path,
) -> None:
    contract = importlib.import_module("contract_v3")
    receipt = contract.complete_routing_receipt_v3(
        base_receipt(["serper"], "serper"),
        [attempt("attempt_aaaaaaaaaaaaaaaa", "serper", AttemptOutcome.SUCCESS)],
        shadow_observation={
            "observed": True,
            "policy_id": "shadow-quality",
            "policy_revision": "3.1",
            "selected_provider": "linkup",
            "shadow_provider": "serper",
            "agreement": False,
            "affected_execution": False,
        },
    )
    contract.validate_routing_receipt_v3(receipt, require_completed=True)
    assert receipt["selected_provider"] == "serper"
    assert receipt["shadow_observation"]["selected_provider"] == "linkup"
    assert receipt["shadow_observation"]["shadow_provider"] == "serper"
    assert receipt["shadow_observation"]["agreement"] is False
    assert receipt["shadow_observation"]["affected_execution"] is False
    record = {
        "schema_version": 1,
        "timestamp": 1_783_890_300.0,
        "execution_id": "exec_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "capability": "search",
        "status": "ok",
        "routing_receipt": receipt,
        "current_provider_attempts": ["attempt_aaaaaaaaaaaaaaaa"],
        "cache": {"disposition": "miss", "origin_execution_id": None},
        "limits_applied": {},
        "warning_codes": [],
        "error_code": None,
    }
    privacy = importlib.import_module("operator_privacy_v3")
    assert privacy.assert_operator_payload_safe(record) is None
    journal = importlib.import_module("operator_receipts_v3").OperatorReceiptJournal(
        tmp_path,
        now=lambda: 1_783_890_301.0,
    )
    assert journal.append(record) is True
    assert journal.load(limit=1)[0]["routing_receipt"]["shadow_observation"] == receipt[
        "shadow_observation"
    ]

    malformed = json.loads(json.dumps(receipt))
    malformed["shadow_observation"]["agreement"] = "false"
    with pytest.raises(ValueError, match="extended shadow observation"):
        contract.validate_routing_receipt_v3(malformed, require_completed=True)


def test_privacy_choke_accepts_fixtures_but_rejects_allowed_key_freetext() -> None:
    privacy = importlib.import_module("operator_privacy_v3")
    for name in (
        "overview.json",
        "receipts.json",
        "benchmark-history.json",
        "shadow-evaluation.json",
    ):
        assert privacy.assert_operator_payload_safe(fixture(name)) is None

    with pytest.raises(ValueError, match="known-safe"):
        privacy.assert_operator_payload_safe(
            {"schema_version": 1, "status": "raw provider said the private thing"}
        )
    with pytest.raises(ValueError, match="forbidden operator field"):
        privacy.assert_operator_payload_safe({"schema_version": 1, "apiKey": 1})
    with pytest.raises(ValueError, match="authorization material"):
        privacy.assert_operator_payload_safe(
            {"schema_version": 1, "display_name": "bearer privatevalue"}
        )
    with pytest.raises(ValueError, match="provider provenance"):
        privacy.assert_operator_payload_safe(
            {"schema_version": 1, "provider": "privatequery"}
        )
    with pytest.raises(ValueError, match="execution provenance"):
        privacy.assert_operator_payload_safe(
            {"schema_version": 1, "execution_id": "exec_privatequery"}
        )
    public_id = "exec_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(ValueError, match="configured secret"):
        privacy.assert_operator_payload_safe(
            {"schema_version": 1, "execution_id": public_id},
            configured_secrets=[public_id],
        )


def test_privacy_choke_tracks_providers_registered_after_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    privacy = importlib.import_module("operator_privacy_v3")
    registry = importlib.import_module("provider_registry")

    monkeypatch.setitem(
        registry.PROVIDER_SPECS,
        "sdk-fixture",
        SimpleNamespace(display_name="SDK fixture provider"),
    )

    assert privacy.assert_operator_payload_safe(
        {
            "schema_version": 1,
            "provider": "sdk-fixture",
            "display_name": "SDK fixture provider",
        }
    ) is None
    with pytest.raises(ValueError, match="provider provenance"):
        privacy.assert_operator_payload_safe(
            {"schema_version": 1, "provider": "unregistered-provider"}
        )


def test_completed_receipt_rejects_missing_candidate_and_outcome_drift() -> None:
    contract = importlib.import_module("contract_v3")
    good = contract.complete_routing_receipt_v3(
        base_receipt(["serper", "linkup"], "serper"),
        [attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
    )
    malformed = dict(good, candidate_decisions=good["candidate_decisions"][:-1])
    with pytest.raises(ValueError, match="cover every candidate"):
        contract.validate_routing_receipt_v3(malformed, require_completed=True)
    with pytest.raises(ValueError, match="attempt outcome"):
        contract.complete_routing_receipt_v3(
            base_receipt(["serper"], "serper"),
            [attempt("attempt_serper", "serper", AttemptOutcome.FAILED)],
        )
    no_selection_base = base_receipt(["serper"], "serper")
    no_selection_base["selected_provider"] = None
    no_selection = contract.complete_routing_receipt_v3(
        no_selection_base,
        [attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
    )
    assert no_selection["candidate_decisions"] == [
        {
            "provider": "serper",
            "position": 1,
            "decision": "attempted_no_selection",
            "reason_code": "insufficient_results",
            "attempt_id": "attempt_serper",
        }
    ]
    contract.validate_routing_receipt_v3(
        no_selection,
        [attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
        require_completed=True,
    )
    direct_with_fake_fallback = dict(good, fallback_reason="selected_failed")
    with pytest.raises(ValueError, match="direct selection"):
        contract.validate_routing_receipt_v3(
            direct_with_fake_fallback,
            [attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
            require_completed=True,
        )
    skipped_attempt = attempt(
        "attempt_tavily",
        "tavily",
        AttemptOutcome.SKIPPED,
        skip_reason=SkipReason.AUTH_BLOCKED,
    )
    skipped = contract.complete_routing_receipt_v3(
        base_receipt(["tavily", "serper"], "serper", "selected_skipped"),
        [skipped_attempt, attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
    )
    skipped["candidate_decisions"][0]["reason_code"] = "blocked_quota"
    with pytest.raises(ValueError, match="skip reason"):
        contract.validate_routing_receipt_v3(
            skipped,
            [skipped_attempt, attempt("attempt_serper", "serper", AttemptOutcome.SUCCESS)],
            require_completed=True,
        )


def test_journal_roundtrip_is_owned_bounded_and_newest_first(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    journal = journal_module.OperatorReceiptJournal(
        tmp_path,
        max_records=2,
        max_bytes=100_000,
        ttl_seconds=3600,
        now=lambda: 5000.0,
    )
    records = fixture("receipts.json")["receipts"]

    assert journal.append(records[1]) is True
    first = dict(
        records[0], execution_id="exec_00000000000000000000000000000001", timestamp=4999.0
    )
    second = dict(
        records[0], execution_id="exec_00000000000000000000000000000002", timestamp=5000.0
    )
    assert journal.append(first) is True
    assert journal.append(second) is True

    loaded = journal.load(limit=10)
    assert [item["execution_id"] for item in loaded] == [
        "exec_00000000000000000000000000000002",
        "exec_00000000000000000000000000000001",
    ]
    assert all("owner" not in item for item in loaded)


def test_journal_rejects_freetext_before_creating_file(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    journal = journal_module.OperatorReceiptJournal(tmp_path)
    unsafe = dict(fixture("receipts.json")["receipts"][0])
    unsafe["status"] = "provider returned private prose"

    assert journal.append(unsafe) is False
    assert not journal.path.exists()


def test_journal_preserves_unowned_collision_byte_identical(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    journal = journal_module.OperatorReceiptJournal(tmp_path)
    journal.path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"foreign":true}\n'
    journal.path.write_bytes(original)

    assert journal.append(fixture("receipts.json")["receipts"][0]) is False
    assert journal.path.read_bytes() == original


def test_journal_refuses_symlinked_data_and_lock_files(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    record = fixture("receipts.json")["receipts"][0]

    data_journal = journal_module.OperatorReceiptJournal(tmp_path / "data")
    data_journal.path.parent.mkdir(parents=True, exist_ok=True)
    data_target = tmp_path / "foreign-data.jsonl"
    data_target.write_bytes(b'{"foreign":true}\n')
    data_journal.path.symlink_to(data_target)
    assert data_journal.append(record) is False
    assert data_target.read_bytes() == b'{"foreign":true}\n'

    lock_journal = journal_module.OperatorReceiptJournal(tmp_path / "lock")
    lock_journal.path.parent.mkdir(parents=True, exist_ok=True)
    lock_target = tmp_path / "foreign-lock"
    lock_target.write_bytes(b"foreign-lock")
    (lock_journal.path.parent / ".receipts.lock").symlink_to(lock_target)
    assert lock_journal.append(record) is False
    assert lock_target.read_bytes() == b"foreign-lock"


def test_journal_refuses_symlinked_ancestors(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    record = fixture("receipts.json")["receipts"][0]

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    assert journal_module.OperatorReceiptJournal(linked_root).append(record) is False
    assert not (real_root / "operator").exists()

    cache_root = tmp_path / "cache-root"
    cache_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cache_root / "operator").symlink_to(outside, target_is_directory=True)
    assert journal_module.OperatorReceiptJournal(cache_root).append(record) is False
    assert not (outside / "v3").exists()


def test_failed_retention_rewrite_leaves_previous_journal_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    journal = journal_module.OperatorReceiptJournal(tmp_path)
    first = fixture("receipts.json")["receipts"][0]
    second = dict(first, execution_id="exec_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert journal.append(first) is True
    before = journal.path.read_bytes()

    def fail_rewrite(_records: list[dict[str, Any]], _directory_fd: int) -> None:
        raise OSError("injected rewrite failure")

    monkeypatch.setattr(journal, "_rewrite", fail_rewrite)
    assert journal.append(second) is False
    assert journal.path.read_bytes() == before


def test_journal_ttl_prunes_only_owned_records(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    journal = journal_module.OperatorReceiptJournal(
        tmp_path,
        ttl_seconds=100,
        max_records=10,
        max_bytes=100_000,
        now=lambda: 1000.0,
    )
    old = dict(fixture("receipts.json")["receipts"][0], timestamp=899.0)
    fresh = dict(fixture("receipts.json")["receipts"][0], timestamp=950.0)
    assert journal.append(old) is True
    assert journal.append(fresh) is True

    assert [item["timestamp"] for item in journal.load()] == [950.0]
    assert journal.path.exists()


def test_concurrent_journal_appends_do_not_lose_owned_records(tmp_path: Path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    source = fixture("receipts.json")["receipts"][0]
    fixture_now = float(source["timestamp"]) + 1.0

    def append(index: int) -> bool:
        journal = journal_module.OperatorReceiptJournal(
            tmp_path,
            max_records=100,
            max_bytes=1_000_000,
            now=lambda: fixture_now,
        )
        return journal.append(
            dict(source, execution_id=f"exec_{index:032x}")
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(24)))

    records = journal_module.OperatorReceiptJournal(
        tmp_path,
        now=lambda: fixture_now,
    ).load(limit=100)
    assert all(results)
    assert {item["execution_id"] for item in records} == {
        f"exec_{index:032x}" for index in range(24)
    }


def test_cross_process_journal_appends_do_not_lose_owned_records(
    tmp_path: Path,
) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    source = fixture("receipts.json")["receipts"][0]
    fixture_now = float(source["timestamp"]) + 1.0
    work = [(str(tmp_path), index, source) for index in range(12)]

    with ProcessPoolExecutor(
        max_workers=4,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        results = list(pool.map(append_journal_in_process, work))

    records = journal_module.OperatorReceiptJournal(
        tmp_path,
        now=lambda: fixture_now,
    ).load(limit=100)
    assert all(results)
    assert {item["execution_id"] for item in records} == {
        f"exec_{index:032x}" for index in range(12)
    }


def test_reason_enum_exactly_matches_task1_contract() -> None:
    contract = importlib.import_module("contract_v3")
    assert {item.value for item in contract.CandidateReasonCode} == REASON_CODES


def test_orchestrator_journals_direct_origin_and_cache_hit_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def normalize(request: Any, plan: ProviderPlan, _payload: dict[str, Any]) -> ResponseV3:
        return ResponseV3(
            request_id=request.request_id or plan.execution_id,
            capability=Capability.SEARCH,
            status=ResponseStatus.OK,
            results=[],
            provider_attempts=[],
            routing_receipt=base_receipt(list(plan.candidate_order), "serper"),
            cache_status={"disposition": "miss"},
        )

    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: ProviderPlan(("serper",), "serper"),
        execute=lambda *_: {"provider": "serper", "results": []},
        normalize=normalize,
    )
    config = {"v3": {"cache_dir": str(tmp_path)}}
    first = execute_v3_request(
        legacy_request_to_v3(
            "search", {"query": "fixture", "provider": "serper"}
        ),
        adapter,
        config,
    )
    second = execute_v3_request(
        legacy_request_to_v3(
            "search", {"query": "fixture", "provider": "serper"}
        ),
        adapter,
        config,
    )
    bad_journal_config = {
        "v3": {
            "cache_dir": str(tmp_path),
            "operator_receipt_max_records": "not-an-integer",
        }
    }
    third = execute_v3_request(
        legacy_request_to_v3(
            "search", {"query": "fixture", "provider": "serper"}
        ),
        adapter,
        bad_journal_config,
    )
    assert third.response.status is ResponseStatus.OK

    receipts_module = importlib.import_module("operator_receipts_v3")

    def explode_append(_journal: Any, _record: dict[str, Any]) -> bool:
        raise RuntimeError("injected unexpected journal failure")

    monkeypatch.setattr(
        receipts_module.OperatorReceiptJournal,
        "append",
        explode_append,
    )
    fourth = execute_v3_request(
        legacy_request_to_v3(
            "search", {"query": "fixture", "provider": "serper"}
        ),
        adapter,
        config,
    )
    assert fourth.response.status is ResponseStatus.OK

    journal = receipts_module.OperatorReceiptJournal(tmp_path)
    records = journal.load(limit=10)
    assert len(records) == 2
    direct = next(
        item for item in records if item["execution_id"] == first.response.execution_id
    )
    cached = next(
        item for item in records if item["execution_id"] == second.response.execution_id
    )
    assert direct["routing_receipt"]["cache_origin"] is None
    assert cached["current_provider_attempts"] == []
    assert cached["routing_receipt"]["candidate_decisions"] == []
    assert (
        cached["routing_receipt"]["cache_origin"]["execution_id"]
        == first.response.execution_id
    )


def test_shadow_interface_stub_receipt_survives_privacy_and_journals(tmp_path) -> None:
    journal_module = importlib.import_module("operator_receipts_v3")
    privacy = importlib.import_module("operator_privacy_v3")
    source = fixture("receipts.json")["receipts"][0]
    record = {
        "schema_version": source["schema_version"],
        "timestamp": source["timestamp"],
        "execution_id": source["execution_id"],
        "capability": source["capability"],
        "status": source["status"],
        "routing_receipt": dict(source["routing"]),
        "cache": dict(source["cache"]),
        "current_provider_attempts": list(source["current_provider_attempts"]),
        "limits_applied": dict(source["limits"]),
        "warning_codes": list(source["warning_codes"]),
        "error_code": source["error_code"],
    }
    record["routing_receipt"]["shadow_observation"] = {
        "observed": True,
        "policy_id": "shadow-interface",
        "policy_revision": "3.0",
        "selected_provider": "serper",
        "affected_execution": False,
    }

    privacy.assert_operator_payload_safe(record)
    journal = journal_module.OperatorReceiptJournal(tmp_path, now=lambda: 1783890301.0)
    assert journal.append(record) is True
    loaded = journal.load(limit=10)
    assert loaded and loaded[0]["routing_receipt"]["shadow_observation"]["policy_id"] == "shadow-interface"
