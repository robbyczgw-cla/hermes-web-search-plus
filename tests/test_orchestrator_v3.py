import json

import pytest

from compat_v3 import (
    legacy_request_to_v3,
    v3_response_to_legacy_extract,
    v3_response_to_legacy_search,
)
from contract_v3 import (
    AttemptOutcome,
    Capability,
    CircuitState,
    ProviderAttemptV3,
    RequestV3,
    ResponseStatus,
    ResponseV3,
)
from orchestrator_v3 import (
    CapabilityExecution,
    CapabilityAdapter,
    ExecutedV3,
    ProviderPlan,
    execute_v3_request,
)


def _response(request: RequestV3, plan: ProviderPlan, legacy: dict) -> ResponseV3:
    return ResponseV3(
        request_id=request.request_id or "generated",
        capability=request.capability,
        status=ResponseStatus.OK,
        results=[],
        provider_attempts=[],
        routing_receipt={
            "policy_id": "classic",
            "policy_revision": "v2.9.1",
            "mode": "classic",
            "candidate_order": list(plan.candidate_order),
            "selected_provider": plan.selected_provider,
            "fallback_reason": "none",
        },
        cache_status={"disposition": "miss"},
    )


def test_m1_search_projection_validates_against_request_schema():
    jsonschema = pytest.importorskip("jsonschema")
    request = legacy_request_to_v3(
        "search",
        {
            "query": "models",
            "depth": "normal",
            "mode": "research",
            "quality_report": True,
            "research_time_budget": 40,
        },
    )
    schema = json.loads(
        __import__("pathlib").Path("schemas/v3/request.schema.json").read_text()
    )
    jsonschema.validate(request.to_dict(), schema)


def test_legacy_search_request_projects_all_public_execution_inputs():
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "  Cafe\u0301 models  ",
            "provider": "auto",
            "count": 7,
            "depth": "deep",
            "time_range": "month",
            "freshness": "week",
            "search_type": "news",
            "include_domains": ["example.com"],
            "exclude_domains": ["spam.test"],
            "mode": "research",
            "quality_report": True,
            "research_time_budget": 42.5,
            "country": "at",
            "language": "de",
        },
        request_id="req-search",
    )

    assert request.input == {"query": "Café models"}
    assert request.options == {
        "max_results": 7,
        "depth": "deep",
        "time_range": "month",
        "freshness": "week",
        "search_type": "news",
        "include_domains": ["example.com"],
        "exclude_domains": ["spam.test"],
        "mode": "research",
        "quality_report": True,
        "research_time_budget": 42.5,
        "locale": {"country": "at", "language": "de"},
    }
    assert request.routing == {
        "mode": "auto",
        "provider": "auto",
        "allow_fallback": True,
        "policy_mode": "classic",
    }


def test_legacy_extract_request_projects_without_hidden_context():
    request = legacy_request_to_v3(
        "extract",
        {
            "urls": ["https://example.com/a"],
            "provider": "linkup",
            "format": "html",
            "include_images": True,
            "include_raw_html": True,
            "render_js": False,
        },
        request_id="req-extract",
    )

    assert request == RequestV3(
        capability=Capability.EXTRACT,
        input={"urls": ["https://example.com/a"]},
        request_id="req-extract",
        options={
            "output_format": "html",
            "include_images": True,
            "include_raw_html": True,
            "render_js": False,
        },
        cache={"mode": "prefer", "ttl_seconds": 3600},
        routing={
            "mode": "fixed",
            "provider": "linkup",
            "allow_fallback": True,
            "policy_mode": "classic",
        },
        client={"accept_contract_versions": ["3.0", "2.x"]},
    )


def test_legacy_projection_is_byte_compatible_and_side_effect_free():
    legacy = {
        "provider": "serper",
        "query": "q",
        "results": [{"title": "A", "url": "https://example.com", "snippet": "S"}],
        "routing": {"provider": "serper", "fallback_used": False},
        "cached": False,
    }
    before = json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()
    request = legacy_request_to_v3("search", {"query": "q", "provider": "serper"})
    plan = ProviderPlan(("serper",), "serper")
    execution = ExecutedV3(_response(request, plan, legacy), plan, legacy, ())

    first = v3_response_to_legacy_search(execution)
    first["results"][0]["title"] = "mutated"
    second = v3_response_to_legacy_search(execution)

    assert (
        json.dumps(second, ensure_ascii=False, separators=(",", ":")).encode() == before
    )
    assert legacy["results"][0]["title"] == "A"


def test_projection_rejects_wrong_capability():
    request = legacy_request_to_v3("search", {"query": "q"})
    plan = ProviderPlan(("serper",), "serper")
    execution = ExecutedV3(_response(request, plan, {}), plan, {}, ())

    with pytest.raises(ValueError):
        v3_response_to_legacy_extract(execution)


def test_b6_same_request_has_same_plan_and_one_execution_path():
    calls = {"plan": 0, "execute": 0, "normalize": 0}

    def plan(request, _config):
        calls["plan"] += 1
        provider = request.routing.get("provider", "auto")
        return ProviderPlan((provider, "serper"), provider)

    def execute(request, provider_plan, _config):
        calls["execute"] += 1
        return {"provider": provider_plan.selected_provider, "results": []}

    def normalize(request, provider_plan, legacy):
        calls["normalize"] += 1
        return _response(request, provider_plan, legacy)

    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=plan,
        execute=execute,
        normalize=normalize,
    )
    legacy_request = legacy_request_to_v3(
        "search", {"query": "same", "provider": "auto"}, request_id="same-id"
    )
    native_request = RequestV3.from_dict(legacy_request.to_dict())

    legacy_execution = execute_v3_request(legacy_request, adapter, {})
    native_execution = execute_v3_request(native_request, adapter, {})

    assert legacy_execution.plan == native_execution.plan
    assert (
        legacy_execution.response.routing_receipt
        == native_execution.response.routing_receipt
    )
    assert calls == {"plan": 2, "execute": 1, "normalize": 1}
    assert native_execution.response.cache_status["disposition"] == "fresh_hit"
    assert legacy_execution.stage_trace == (
        "normalize",
        "validate",
        "cache_lookup",
        "candidate_plan",
        "provider_attempt",
        "result_normalization",
        "cache_write",
        "response_v3",
    )
    assert native_execution.stage_trace == (
        "normalize",
        "validate",
        "cache_lookup",
        "candidate_plan",
        "response_v3",
    )


def test_orchestrator_uses_actual_attempt_receipts_and_stages():
    request = legacy_request_to_v3(
        "search", {"query": "q", "provider": "serper"}, request_id="req-1"
    )
    plan = ProviderPlan(("serper",), "serper")
    receipt = ProviderAttemptV3(
        attempt_id="attempt-1",
        provider="serper",
        capability=Capability.SEARCH,
        outcome=AttemptOutcome.SUCCESS,
        result_count=1,
        circuit_state_before=CircuitState.CLOSED,
        circuit_state_after=CircuitState.CLOSED,
    )
    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: plan,
        execute=lambda *_: CapabilityExecution(
            payload={"provider": "serper", "results": []},
            provider_attempts=(receipt,),
            stages=("admission", "provider_attempt", "retry_circuit_update"),
        ),
        normalize=_response,
    )

    execution = execute_v3_request(request, adapter, {})

    assert execution.response.provider_attempts == [receipt]
    assert execution.stage_trace == (
        "normalize",
        "validate",
        "cache_lookup",
        "candidate_plan",
        "admission",
        "provider_attempt",
        "retry_circuit_update",
        "result_normalization",
        "cache_write",
        "response_v3",
    )


def test_orchestrator_rejects_adapter_for_other_capability_before_execution():
    called = False

    def execute(*_args):
        nonlocal called
        called = True
        return {}

    adapter = CapabilityAdapter(
        capability=Capability.EXTRACT,
        plan=lambda *_: ProviderPlan(("linkup",), "linkup"),
        execute=execute,
        normalize=_response,
    )
    request = legacy_request_to_v3("search", {"query": "q"})

    with pytest.raises(ValueError, match="capability"):
        execute_v3_request(request, adapter, {})
    assert called is False
