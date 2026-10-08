from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.budget_preflight_v3 import run_budget_preflight
from wsp_core.config import _deepcopy_default_config, _validate_runtime_config
from wsp_core.contract_v3 import (
    Capability,
    ErrorClass,
    RequestV3,
    ResponseStatus,
    ResponseV3,
    SkipReason,
    validate_routing_receipt_v3,
)
from wsp_core.operator_privacy_v3 import assert_operator_payload_safe
from wsp_core.operator_receipts_v3 import receipt_record_from_response
from wsp_core.orchestrator_v3 import (
    _DAILY_PROVIDER_CALL_SCOPE,
    CapabilityAdapter,
    ProviderPlan,
    execute_v3_request,
)
from wsp_core.state_store_v3 import SQLiteStateStore


def _request(*, capability: Capability = Capability.SEARCH, **kwargs) -> RequestV3:
    if capability is Capability.EXTRACT:
        return RequestV3(
            capability,
            {"urls": ["https://example.test/a"]},
            options={"max_context_chars": kwargs.get("context", 60_000)},
            cache={"mode": "bypass"},
            budget=kwargs.get("budget", {}),
        )
    return RequestV3(
        capability,
        {"query": "preflight test"},
        options=kwargs.get("options", {}),
        cache={"mode": "bypass"},
        budget=kwargs.get("budget", {}),
    )


def _plan(count: int = 3) -> ProviderPlan:
    providers = ("serper", "linkup", "tavily")[:count]
    return ProviderPlan(providers, providers[0])


def _config(**limits: int | str | bool | None) -> dict:
    return {
        "budget_preflight": {
            "enabled": True,
            "max_provider_calls_per_request": limits.get(
                "max_provider_calls_per_request"
            ),
            "max_daily_provider_calls": limits.get("max_daily_provider_calls"),
            "max_timeout_seconds": limits.get("max_timeout_seconds"),
            "max_context_chars": limits.get("max_context_chars"),
            "on_exceed": limits.get("on_exceed", "degrade"),
        }
    }


@pytest.mark.parametrize(
    ("config", "budget_request", "ledger", "check_name"),
    [
        (_config(max_provider_calls_per_request=2), _request(), {}, "provider_call_cap"),
        (_config(max_daily_provider_calls=3), _request(), {"used_units": 2}, "daily_quota"),
        (_config(max_timeout_seconds=5), _request(budget={"max_wall_time_ms": 6000}), {}, "timeout_budget"),
        (_config(max_context_chars=5000), _request(capability=Capability.EXTRACT, context=6000), {}, "context_budget"),
    ],
)
def test_pure_preflight_evaluates_each_budget_check(
    config: dict, budget_request: RequestV3, ledger: dict, check_name: str
) -> None:
    decision = run_budget_preflight(budget_request, _plan(), config, ledger)

    assert decision.action == "degrade"
    assert {check.check for check in decision.checks} == {
        "provider_call_cap",
        "daily_quota",
        "timeout_budget",
        "context_budget",
    }
    assert next(check for check in decision.checks if check.check == check_name).verdict == "exceeded"


def test_preflight_degrades_to_the_smallest_compatible_provider_call_limit() -> None:
    decision = run_budget_preflight(
        _request(),
        _plan(),
        _config(max_provider_calls_per_request=2, max_daily_provider_calls=5),
        {"used_units": 3, "reserved_units": 0},
    )

    assert decision.action == "degrade"
    assert decision.adjustments == {"max_provider_calls": 2}


def test_preflight_aborts_when_daily_quota_is_exhausted() -> None:
    decision = run_budget_preflight(
        _request(), _plan(), _config(max_daily_provider_calls=3), {"used_units": 3}
    )

    assert decision.action == "abort"
    assert decision.reason == "daily_quota_exhausted"


def test_preflight_disabled_or_unlimited_returns_without_evaluating() -> None:
    assert run_budget_preflight(_request(), _plan(), {}, None).to_dict() == {
        "action": "proceed",
        "checks": [],
    }
    assert run_budget_preflight(
        _request(), _plan(), _config(), {"used_units": 99}
    ).to_dict() == {"action": "proceed", "checks": []}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("enabled", "yes"),
        ("max_provider_calls_per_request", 0),
        ("max_daily_provider_calls", True),
        ("max_timeout_seconds", 1.5),
        ("max_context_chars", 999),
        ("on_exceed", "ignore"),
    ],
)
def test_config_rejects_invalid_budget_preflight_values(key: str, value: object) -> None:
    config = _deepcopy_default_config()
    config["budget_preflight"][key] = value

    with pytest.raises(ValueError, match="budget_preflight"):
        _validate_runtime_config(config)


def _response(request: RequestV3, plan: ProviderPlan, _payload: dict) -> ResponseV3:
    return ResponseV3(
        request_id=request.request_id or plan.execution_id,
        capability=request.capability,
        status=ResponseStatus.OK,
        results=[],
        provider_attempts=[],
        routing_receipt={
            "policy_id": "classic",
            "policy_revision": "v2.9.1",
            "mode": plan.mode,
            "candidate_order": list(plan.candidate_order),
            "selected_provider": plan.selected_provider,
            "fallback_reason": "none",
        },
        cache_status={"disposition": "bypassed"},
    )


def _adapter(seen: list[tuple]) -> CapabilityAdapter:
    def plan(_request: RequestV3, _config: dict) -> ProviderPlan:
        return _plan(2)

    def execute(request: RequestV3, plan: ProviderPlan, _config: dict) -> dict:
        seen.append((plan.candidate_order, dict(request.budget), dict(request.options)))
        return {"provider": plan.selected_provider, "results": []}

    return CapabilityAdapter(Capability.SEARCH, plan, execute, _response)


def _runtime_config(tmp_path, **limits: int | str | bool | None) -> dict:
    config = _config(**limits)
    config["v3"] = {
        "cache_dir": str(tmp_path),
        "state_path": str(tmp_path / "state.sqlite3"),
        "operator_receipt_journal": False,
    }
    return config


def test_orchestrator_degrade_caps_fanout_and_records_policy_action(tmp_path) -> None:
    seen: list[tuple] = []
    execution = execute_v3_request(
        _request(), _adapter(seen), _runtime_config(tmp_path, max_provider_calls_per_request=1)
    )

    assert seen == [(("serper",), {"max_provider_attempts": 1}, {})]
    assert execution.plan.candidate_order == ("serper",)
    assert execution.response.routing_receipt["budget_preflight"]["action"] == "degrade"
    assert execution.response.policy_actions[-1] == {
        "action": "budget_preflight",
        "observation_id": None,
        "reason": "degraded",
    }
    assert_operator_payload_safe(receipt_record_from_response(execution.response))


def test_orchestrator_abort_returns_typed_failure_without_provider_attempt(tmp_path) -> None:
    config = _runtime_config(tmp_path, max_daily_provider_calls=1)
    store = SQLiteStateStore(config["v3"]["state_path"])
    today = datetime.now(timezone.utc).date().isoformat()
    store.configure_budget(_DAILY_PROVIDER_CALL_SCOPE, today, limit_units=1)
    assert store.reserve_budget(_DAILY_PROVIDER_CALL_SCOPE, today, units=1)
    assert store.commit_budget(_DAILY_PROVIDER_CALL_SCOPE, today, units=1)
    seen: list[tuple] = []

    execution = execute_v3_request(_request(), _adapter(seen), config)

    assert seen == []
    assert execution.response.status is ResponseStatus.FAILED
    assert execution.response.error is not None
    assert execution.response.error.error_class is ErrorClass.BUDGET
    assert execution.response.error.code == "wsp.budget.preflight"
    assert execution.response.provider_attempts == []
    assert execution.response.routing_receipt["budget_preflight"]["reason"] == "daily_quota_exhausted"
    assert {item["reason_code"] for item in execution.response.routing_receipt["candidate_decisions"]} == {"budget_denied"}
    assert_operator_payload_safe(receipt_record_from_response(execution.response))


def test_daily_ledger_is_updated_after_provider_calls(tmp_path) -> None:
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    engine = AttemptEngine(store, max_attempts=1)
    today = datetime.now(timezone.utc).date().isoformat()
    first = AttemptContext(
        provider="serper",
        capability=Capability.SEARCH,
        endpoint="provider://serper/search",
        credential_fingerprint=store.fingerprint_credential("fixture"),
        budget_scope="one",
        budget_window="request",
        budget_limit_units=1,
        daily_budget_scope=_DAILY_PROVIDER_CALL_SCOPE,
        daily_budget_window=today,
        daily_budget_limit_units=1,
    )
    second = AttemptContext(
        provider="linkup",
        capability=Capability.SEARCH,
        endpoint="provider://linkup/search",
        credential_fingerprint=store.fingerprint_credential("fixture"),
        budget_scope="two",
        budget_window="request",
        budget_limit_units=1,
        daily_budget_scope=_DAILY_PROVIDER_CALL_SCOPE,
        daily_budget_window=today,
        daily_budget_limit_units=1,
    )

    assert engine.execute(first, lambda: {"results": []}).payload == {"results": []}
    blocked = engine.execute(second, lambda: pytest.fail("must not call provider"))

    assert blocked.receipt.skip_reason is SkipReason.BUDGET_BLOCKED
    assert store.get_budget(_DAILY_PROVIDER_CALL_SCOPE, today).used_units == 1


def test_readonly_ledger_snapshot_never_mutates_existing_state(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    store.configure_budget("snapshot", "window", limit_units=4)
    assert store.reserve_budget("snapshot", "window", units=2)
    before = path.read_bytes()

    snapshot = SQLiteStateStore.open_readonly(path).read_budget_snapshot(
        "snapshot", "window"
    )

    assert snapshot is not None
    assert (snapshot.limit_units, snapshot.used_units, snapshot.reserved_units) == (4, 0, 2)
    assert path.read_bytes() == before


def test_default_config_has_no_preflight_receipt_or_behavior_change(tmp_path) -> None:
    seen: list[tuple] = []
    execution = execute_v3_request(
        _request(), _adapter(seen), {"v3": {"cache_dir": str(tmp_path), "operator_receipt_journal": False}}
    )

    assert seen == [(("serper", "linkup"), {}, {})]
    assert "budget_preflight" not in execution.response.routing_receipt
    assert execution.response.policy_actions == []


def test_budget_preflight_kill_switch_disables_enabled_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WSP_BUDGET_PREFLIGHT_OFF", "1")
    seen: list[tuple] = []

    execution = execute_v3_request(
        _request(), _adapter(seen), _runtime_config(tmp_path, max_provider_calls_per_request=1)
    )

    assert seen == [(("serper", "linkup"), {}, {})]
    assert "budget_preflight" not in execution.response.routing_receipt


def test_contract_rejects_untyped_preflight_receipt_extension(tmp_path) -> None:
    execution = execute_v3_request(
        _request(), _adapter([]), _runtime_config(tmp_path, max_provider_calls_per_request=1)
    )
    receipt = dict(execution.response.routing_receipt)
    receipt["budget_preflight"] = {
        **receipt["budget_preflight"],
        "reason": "untyped detail",
    }

    with pytest.raises(ValueError, match="budget_preflight"):
        validate_routing_receipt_v3(receipt, require_completed=True)


def test_preflight_receipt_satisfies_response_json_schema(tmp_path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    execution = execute_v3_request(
        _request(), _adapter([]), _runtime_config(tmp_path, max_provider_calls_per_request=1)
    )
    schema = json.loads(
        (Path("schemas") / "v3" / "response.schema.json").read_text(encoding="utf-8")
    )

    jsonschema.validate(execution.response.to_dict(), schema)
