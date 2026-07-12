from __future__ import annotations

from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from orchestrator_v3 import CapabilityAdapter, ProviderPlan, execute_v3_request


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
            "mode": "classic",
            "candidate_order": list(plan.candidate_order),
            "selected_provider": "serper",
            "fallback_reason": "none",
        },
        cache_status={"disposition": "miss"},
    )


def _request(request_id: str, *, bypass: bool = False) -> RequestV3:
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "cache me", "provider": "serper", "count": 2},
        request_id=request_id,
    )
    if not bypass:
        return request
    return RequestV3.from_dict(
        {**request.to_dict(), "cache": {**request.cache, "mode": "bypass"}}
    )


def test_orchestrator_v3_cache_hit_skips_provider_and_rebinds_request_id(tmp_path):
    calls = []

    def execute(_request, _plan, _config):
        calls.append("provider")
        return {"provider": "serper", "results": [], "cached": False}

    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: ProviderPlan(("serper",), "serper"),
        execute=execute,
        normalize=_response,
    )
    config = {"v3": {"cache_dir": str(tmp_path)}}

    first = execute_v3_request(_request("first"), adapter, config)
    second = execute_v3_request(_request("second"), adapter, config)

    assert calls == ["provider"]
    assert first.response.cache_status["disposition"] == "miss"
    assert "cache_lookup" in first.stage_trace
    assert "cache_write" in first.stage_trace
    assert second.response.request_id == "second"
    assert second.response.cache_status["disposition"] == "fresh_hit"
    assert second.response.cache_status["source_contract_version"] == "3.0"
    assert second.response.provider_attempts == []
    assert second.legacy_payload["cached"] is True
    assert "provider_attempt" not in second.stage_trace
    assert "cache_write" not in second.stage_trace


def test_cache_bypass_neither_reads_nor_writes(tmp_path):
    calls = []
    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: ProviderPlan(("serper",), "serper"),
        execute=lambda *_: calls.append("provider") or {"provider": "serper", "results": []},
        normalize=_response,
    )
    config = {"v3": {"cache_dir": str(tmp_path)}}

    first = execute_v3_request(_request("one", bypass=True), adapter, config)
    second = execute_v3_request(_request("two", bypass=True), adapter, config)

    assert calls == ["provider", "provider"]
    assert first.response.cache_status["disposition"] == "bypassed"
    assert second.response.cache_status["disposition"] == "bypassed"
    assert "cache_lookup" not in first.stage_trace
    assert "cache_write" not in first.stage_trace
    assert not (tmp_path / "v3" / "response").exists()


def test_cache_only_miss_never_calls_provider(tmp_path):
    calls = []
    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: ProviderPlan(("serper",), "serper"),
        execute=lambda *_: calls.append("provider") or {},
        normalize=_response,
    )
    base = _request("only")
    request = RequestV3.from_dict(
        {**base.to_dict(), "cache": {**base.cache, "mode": "only"}}
    )

    execution = execute_v3_request(
        request, adapter, {"v3": {"cache_dir": str(tmp_path)}}
    )

    assert calls == []
    assert execution.response.status.value == "failed"
    assert execution.response.error is not None
    assert execution.response.error.code == "wsp.cache.miss"
    assert execution.response.cache_status == {"disposition": "miss"}
    assert "provider_attempt" not in execution.stage_trace
