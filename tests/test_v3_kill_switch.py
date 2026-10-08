from __future__ import annotations

import json

from wsp_core.attempt_engine_v3 import AttemptContext, AttemptEngine
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.config import DEFAULT_CONFIG, _deepcopy_default_config, _validate_runtime_config, load_config
from wsp_core.contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from wsp_core.orchestrator_v3 import (
    CapabilityAdapter,
    CapabilityExecution,
    ProviderPlan,
    execute_v3_request,
)
from wsp_core.state_store_v3 import SQLiteStateStore


def _request(
    policy_mode: str,
    *,
    provider: str = "serper",
    no_cache: bool = True,
) -> RequestV3:
    base = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "same", "provider": provider, "no_cache": no_cache},
        request_id=f"request-{policy_mode}",
    )
    return RequestV3.from_dict(
        {
            **base.to_dict(),
            "routing": {**base.routing, "policy_mode": policy_mode},
        }
    )


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


def _adapter(seen: list[tuple[str, str]]) -> CapabilityAdapter:
    def plan(request: RequestV3, _config: dict) -> ProviderPlan:
        seen.append(("plan", str(request.routing["policy_mode"])))
        return ProviderPlan(("serper", "linkup"), "serper", mode="classic")

    def execute(_request: RequestV3, plan: ProviderPlan, _config: dict) -> dict:
        seen.extend(("dispatch", provider) for provider in plan.candidate_order)
        return {"provider": "serper", "results": []}

    return CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=plan,
        execute=execute,
        normalize=_response,
    )


def _config(tmp_path, policy_mode: str) -> dict:
    return {
        "routing": {"policy_mode": policy_mode},
        "v3": {
            "cache_dir": str(tmp_path),
            "state_path": str(tmp_path / "state.sqlite3"),
            "operator_receipt_journal": False,
        },
    }


def test_routing_policy_defaults_to_classic():
    assert DEFAULT_CONFIG["routing"] == {"policy_mode": "classic"}


def test_runtime_config_rejects_unknown_policy_mode():
    config = _deepcopy_default_config()
    config["routing"]["policy_mode"] = "canary"

    try:
        _validate_runtime_config(config)
    except ValueError as exc:
        assert str(exc) == "routing.policy_mode must be classic or shadow"
    else:
        raise AssertionError("invalid policy mode was accepted")


def test_runtime_config_accepts_shadow_as_classic():
    config = _deepcopy_default_config()
    config["routing"]["policy_mode"] = "shadow"
    validated = _validate_runtime_config(config)
    assert validated["routing"]["policy_mode"] == "classic"


def test_config_classic_forces_classic_before_planning(tmp_path, monkeypatch):
    monkeypatch.delenv("WSP_ROUTING_CLASSIC_ONLY", raising=False)
    seen: list[tuple[str, str]] = []

    execution = execute_v3_request(
        _request("shadow"), _adapter(seen), _config(tmp_path, "classic")
    )

    assert seen == [
        ("plan", "classic"),
        ("dispatch", "serper"),
        ("dispatch", "linkup"),
    ]
    assert execution.plan.mode == "classic"
    assert execution.response.routing_receipt["mode"] == "classic"
    assert execution.response.routing_receipt["shadow_observation"] is None


def test_env_classic_only_is_a_noop(tmp_path, monkeypatch):
    for value in ("1", "0", "unexpected"):
        monkeypatch.setenv("WSP_ROUTING_CLASSIC_ONLY", value)
        seen: list[tuple[str, str]] = []
        execution = execute_v3_request(
            _request("shadow", provider="auto"),
            _adapter(seen),
            _config(tmp_path / value, "shadow"),
        )
        assert seen[0] == ("plan", "classic")
        assert execution.plan.mode == "classic"
        assert execution.response.routing_receipt["mode"] == "classic"
        assert execution.response.routing_receipt["shadow_observation"] is None


def test_retired_shadow_request_stays_classic(tmp_path, monkeypatch):
    monkeypatch.delenv("WSP_ROUTING_CLASSIC_ONLY", raising=False)
    classic_seen: list[tuple[str, str]] = []
    shadow_seen: list[tuple[str, str]] = []

    classic = execute_v3_request(
        _request("classic", provider="auto"),
        _adapter(classic_seen),
        _config(tmp_path / "classic", "classic"),
    )
    shadow = execute_v3_request(
        _request("shadow", provider="auto"),
        _adapter(shadow_seen),
        _config(tmp_path / "shadow", "shadow"),
    )

    assert classic_seen[1:] == shadow_seen[1:] == [
        ("dispatch", "serper"),
        ("dispatch", "linkup"),
    ]
    assert classic.plan.candidate_order == shadow.plan.candidate_order
    assert classic.plan.mode == shadow.plan.mode == "classic"
    assert classic.response.routing_receipt["shadow_observation"] is None
    assert shadow.response.routing_receipt["mode"] == "classic"
    assert shadow.response.routing_receipt["shadow_observation"] is None


def test_shadow_cache_hit_observation_stays_null(tmp_path, monkeypatch):
    monkeypatch.delenv("WSP_ROUTING_CLASSIC_ONLY", raising=False)
    first_seen: list[tuple[str, str]] = []
    second_seen: list[tuple[str, str]] = []
    request = _request("shadow", provider="auto", no_cache=False)
    config = _config(tmp_path, "shadow")

    first = execute_v3_request(request, _adapter(first_seen), config)
    second = execute_v3_request(request, _adapter(second_seen), config)

    assert first.response.routing_receipt["shadow_observation"] is None
    assert second.response.cache_status["disposition"] == "fresh_hit"
    assert second.response.routing_receipt["mode"] == "classic"
    assert second.response.routing_receipt["shadow_observation"] is None
    assert second_seen == [("plan", "classic")]


def test_sqlite_down_still_executes_classic_when_switch_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("WSP_ROUTING_CLASSIC_ONLY", "1")
    corrupt = tmp_path / "state.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    calls: list[str] = []

    def plan(request: RequestV3, _config: dict) -> ProviderPlan:
        return ProviderPlan(("serper",), "serper", mode=request.routing["policy_mode"])

    def execute(_request: RequestV3, plan: ProviderPlan, _config: dict) -> CapabilityExecution:
        store = SQLiteStateStore(corrupt)
        engine = AttemptEngine(store, max_attempts=1)
        context = AttemptContext(
            provider="serper",
            capability=Capability.SEARCH,
            endpoint="provider://serper/search",
            credential_fingerprint=store.fingerprint_credential("fixture-credential"),
            budget_scope="sqlite-down",
            budget_window="request",
            budget_limit_units=1,
        )
        result = engine.execute(
            context,
            lambda: calls.append(plan.mode) or {"provider": "serper", "results": []},
        )
        return CapabilityExecution(
            payload=result.payload or {"provider": "serper", "results": []},
            provider_attempts=(result.receipt,),
            stages=("admission", "provider_attempt", "retry_circuit_update"),
        )

    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=plan,
        execute=execute,
        normalize=_response,
    )
    execution = execute_v3_request(
        _request("shadow"), adapter, _config(tmp_path / "cache", "shadow")
    )

    assert calls == ["classic"]
    assert execution.plan.mode == "classic"
    assert execution.response.provider_attempts[0].budget_decision == "store_unavailable"
    assert execution.response.routing_receipt["shadow_observation"] is None


def test_config_file_shadow_policy_mode_loads_and_search_stays_classic(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"routing": {"policy_mode": "shadow"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_SEARCH_PLUS_CONFIG", str(config_path))
    monkeypatch.delenv("WSP_ROUTING_CLASSIC_ONLY", raising=False)

    loaded = load_config()

    assert config_path.is_file()
    assert loaded["routing"]["policy_mode"] == "classic"
    seen: list[tuple[str, str]] = []
    execution = execute_v3_request(
        _request("shadow", provider="auto"),
        _adapter(seen),
        {
            "routing": loaded["routing"],
            "v3": {
                "cache_dir": str(tmp_path / "cache"),
                "state_path": str(tmp_path / "state.sqlite3"),
                "operator_receipt_journal": False,
            },
        },
    )
    assert seen[0] == ("plan", "classic")
    assert execution.response.routing_receipt["mode"] == "classic"
    assert execution.response.routing_receipt["shadow_observation"] is None
