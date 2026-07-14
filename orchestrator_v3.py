"""Canonical Web Search Plus v3 orchestration boundary.

The orchestrator owns the sole execution entrance. Capability adapters contain
provider-specific calls and normalization only; legacy callers are projected to
RequestV3 before they can reach this function.
"""

from __future__ import annotations

import copy
import os
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Optional, Tuple, Union

import cache as legacy_cache
from cache_v3 import ResponseCacheV3, response_payload_from_cache_material
from contract_v3 import (
    Capability,
    DegradedReason,
    ErrorClass,
    ErrorV3,
    RequestV3,
    ResponseStatus,
    ResponseV3,
    complete_routing_receipt_v3,
)
from operator_receipts_v3 import OperatorReceiptJournal, receipt_record_from_response


PIPELINE_STAGES: Tuple[str, ...] = (
    "normalize",
    "validate",
    "cache_lookup",
    "candidate_plan",
    "admission",
    "provider_attempt",
    "error_classification",
    "retry_circuit_update",
    "fallback",
    "result_normalization",
    "dedup_fingerprint",
    "cache_write",
    "response_v3",
)


@dataclass(frozen=True)
class ProviderPlan:
    candidate_order: Tuple[str, ...]
    selected_provider: str
    routing_metadata: Dict[str, Any] = field(default_factory=dict)
    mode: str = "classic"
    execution_id: str = field(
        default_factory=lambda: str(uuid.uuid4()), compare=False
    )

    def __post_init__(self) -> None:
        if self.mode not in {"classic", "shadow"}:
            raise ValueError("provider plan mode must be classic or shadow")
        if not self.candidate_order:
            raise ValueError("provider plan requires candidates")
        if self.selected_provider not in self.candidate_order:
            raise ValueError("selected provider must be in candidate_order")


PlanFn = Callable[[RequestV3, Dict[str, Any]], ProviderPlan]
@dataclass(frozen=True)
class CapabilityExecution:
    payload: Dict[str, Any]
    provider_attempts: Tuple[Any, ...] = ()
    stages: Tuple[str, ...] = ("provider_attempt",)

    def __post_init__(self) -> None:
        if len(set(self.stages)) != len(self.stages):
            raise ValueError("execution stages must be unique")
        unknown = set(self.stages) - set(PIPELINE_STAGES)
        if unknown:
            raise ValueError(f"unknown execution stages: {sorted(unknown)}")
        positions = [PIPELINE_STAGES.index(stage) for stage in self.stages]
        if positions != sorted(positions):
            raise ValueError("execution stages must follow canonical order")


ExecuteFn = Callable[
    [RequestV3, ProviderPlan, Dict[str, Any]],
    Union[Dict[str, Any], CapabilityExecution],
]
NormalizeFn = Callable[[RequestV3, ProviderPlan, Dict[str, Any]], ResponseV3]
FinalizeResponseFn = Callable[
    [RequestV3, ProviderPlan, ResponseV3, Dict[str, Any]], ResponseV3
]
LegacyCacheLookupFn = Callable[
    [RequestV3, ProviderPlan, Dict[str, Any]], Optional[CapabilityExecution]
]


@dataclass(frozen=True)
class CapabilityAdapter:
    capability: Capability
    plan: PlanFn
    execute: ExecuteFn
    normalize: NormalizeFn
    legacy_cache_lookup: LegacyCacheLookupFn | None = None
    finalize_response: FinalizeResponseFn | None = None


@dataclass(frozen=True)
class ExecutedV3:
    response: ResponseV3
    plan: ProviderPlan
    legacy_payload: Dict[str, Any]
    stage_trace: Tuple[str, ...] = PIPELINE_STAGES

    def legacy_copy(self) -> Dict[str, Any]:
        return copy.deepcopy(self.legacy_payload)


def _normalize_request(request: RequestV3) -> RequestV3:
    if request.capability is not Capability.SEARCH:
        return request
    normalized_query = unicodedata.normalize("NFC", request.input["query"]).strip()
    if normalized_query == request.input["query"]:
        return request
    return replace(request, input={**request.input, "query": normalized_query})


_EXPLICIT_FALSE_VALUES = {"", "0", "false", "no", "off"}


def _effective_policy_mode(request: RequestV3, config: Dict[str, Any]) -> str:
    """Resolve the two-level policy switch, failing closed to Classic."""
    raw_override = os.environ.get("WSP_ROUTING_CLASSIC_ONLY")
    if raw_override is not None:
        normalized = raw_override.strip().strip('"').strip("'").lower()
        if normalized not in _EXPLICIT_FALSE_VALUES:
            return "classic"

    routing_config = config.get("routing")
    if not isinstance(routing_config, dict):
        return "classic"
    if routing_config.get("policy_mode") != "shadow":
        return "classic"
    return "shadow" if request.routing.get("policy_mode") == "shadow" else "classic"


def _shadow_intent_observation(selected_provider: str | None) -> Dict[str, Any]:
    """Describe Shadow intent without evaluating or changing the Classic plan."""
    return {
        "observed": True,
        "policy_id": "shadow-interface",
        "policy_revision": "3.0",
        "selected_provider": selected_provider,
        "affected_execution": False,
    }


def _append_operator_receipt(
    response: ResponseV3,
    cache_root: Any,
    v3_config: Dict[str, Any],
) -> None:
    """Best-effort journal append; never changes the execution response."""
    try:
        if v3_config.get("operator_receipt_journal", True) is False:
            return
        journal = OperatorReceiptJournal(
            cache_root,
            ttl_seconds=int(v3_config.get("operator_receipt_ttl_seconds", 604800)),
            max_records=int(v3_config.get("operator_receipt_max_records", 1000)),
            max_bytes=int(v3_config.get("operator_receipt_max_bytes", 8 * 1024 * 1024)),
        )
        journal.append(receipt_record_from_response(response))
    except Exception:
        # Operator evidence is strictly best-effort and must never alter execution.
        return


def execute_v3_request(
    request: RequestV3,
    adapter: CapabilityAdapter,
    config: Dict[str, Any] | None = None,
) -> ExecutedV3:
    """Execute one RequestV3 through the canonical orchestration entrance."""
    request = _normalize_request(request)
    if request.capability is not adapter.capability:
        raise ValueError("request and adapter capability differ")
    runtime_config: Dict[str, Any] = config or {}
    policy_mode = _effective_policy_mode(request, runtime_config)
    if request.routing.get("policy_mode") != policy_mode:
        request = replace(
            request,
            routing={**request.routing, "policy_mode": policy_mode},
        )
    plan = adapter.plan(request, runtime_config)
    if plan.mode != policy_mode:
        plan = replace(plan, mode=policy_mode)
    cache_mode = str(request.cache.get("mode") or "prefer")
    cache_enabled = cache_mode != "bypass"
    v3_config = runtime_config.get("v3") or {}
    response_cache = ResponseCacheV3(
        v3_config.get("cache_dir") or legacy_cache.CACHE_DIR
    )
    if cache_enabled:
        lookup = response_cache.get(
            request,
            ttl_seconds=int(request.cache.get("ttl_seconds", 3600)),
            allow_stale_seconds=int(request.cache.get("allow_stale_seconds", 0)),
            now=int(time.time()),
        )
        if lookup.payload is not None:
            cached_order = tuple(
                (lookup.payload.get("routing_receipt") or {}).get("candidate_order")
                or ()
            )
            if cached_order == plan.candidate_order:
                cache_status = {
                    "disposition": lookup.disposition,
                    "entry_id": lookup.entry_id,
                    "age_seconds": lookup.age_seconds,
                    "ttl_seconds": int(request.cache.get("ttl_seconds", 3600)),
                    "served_stale": lookup.disposition == "stale_hit",
                    "source_contract_version": "3.0",
                    "origin_execution_id": lookup.payload.get("origin_execution_id"),
                }
                cached_payload = response_payload_from_cache_material(
                    lookup.payload,
                    request_id=request.request_id or plan.execution_id,
                    execution_id=plan.execution_id,
                    disposition=lookup.disposition,
                    entry_id=str(lookup.entry_id or ""),
                    age_seconds=int(lookup.age_seconds or 0),
                    ttl_seconds=int(request.cache.get("ttl_seconds", 3600)),
                )
                cached_response = ResponseV3.from_dict(cached_payload)
                cached_routing = {
                    **cached_response.routing_receipt,
                    "mode": policy_mode,
                }
                if policy_mode == "classic":
                    cached_routing["shadow_observation"] = None
                else:
                    cached_routing["shadow_observation"] = _shadow_intent_observation(
                        cached_routing.get("selected_provider")
                    )
                warnings = list(cached_response.warnings)
                status = cached_response.status
                if lookup.disposition == "stale_hit":
                    status = ResponseStatus.DEGRADED
                    warnings.append(
                        {
                            "code": DegradedReason.SERVED_STALE.value,
                            "message": "Served stale cached response.",
                        }
                    )
                cached_response = replace(
                    cached_response,
                    status=status,
                    provider_attempts=[],
                    routing_receipt=cached_routing,
                    cache_status=cache_status,
                    warnings=warnings,
                )
                if adapter.finalize_response is not None:
                    cached_response = adapter.finalize_response(
                        request, plan, cached_response, runtime_config
                    )
                legacy_payload = copy.deepcopy(lookup.legacy_payload or {})
                legacy_payload["cached"] = True
                legacy_payload["cache_age_seconds"] = lookup.age_seconds or 0
                stage_set = {
                    "normalize",
                    "validate",
                    "cache_lookup",
                    "candidate_plan",
                    "response_v3",
                }
                _append_operator_receipt(
                    cached_response,
                    response_cache.root,
                    v3_config,
                )
                return ExecutedV3(
                    response=cached_response,
                    plan=plan,
                    legacy_payload=legacy_payload,
                    stage_trace=tuple(
                        stage for stage in PIPELINE_STAGES if stage in stage_set
                    ),
                )
    legacy_execution = (
        adapter.legacy_cache_lookup(request, plan, runtime_config)
        if cache_enabled and adapter.legacy_cache_lookup is not None
        else None
    )
    if cache_mode == "only" and legacy_execution is None:
        response = ResponseV3(
            request_id=request.request_id or plan.execution_id,
            capability=request.capability,
            status=ResponseStatus.FAILED,
            results=[],
            provider_attempts=[],
            routing_receipt={
                "policy_id": "classic",
                "policy_revision": "v2.9.1",
                "mode": plan.mode,
                "candidate_order": list(plan.candidate_order),
                "selected_provider": None,
                "fallback_reason": "none",
            },
            cache_status={"disposition": "miss"},
            error=ErrorV3(
                error_class=ErrorClass.CONFIG,
                code="wsp.cache.miss",
                message="Cache-only request missed.",
                retryable=False,
            ),
        )
        response = replace(
            response,
            routing_receipt=complete_routing_receipt_v3(
                response.routing_receipt,
                [],
                shadow_observation=(
                    _shadow_intent_observation(None)
                    if policy_mode == "shadow"
                    else None
                ),
            ),
        )
        stage_set = {
            "normalize",
            "validate",
            "cache_lookup",
            "candidate_plan",
            "response_v3",
        }
        _append_operator_receipt(response, response_cache.root, v3_config)
        return ExecutedV3(
            response=response,
            plan=plan,
            legacy_payload={
                "error": "Cache-only request missed",
                "provider": plan.selected_provider,
                "results": [],
            },
            stage_trace=tuple(
                stage for stage in PIPELINE_STAGES if stage in stage_set
            ),
        )
    raw_execution = legacy_execution or adapter.execute(request, plan, runtime_config)
    if isinstance(raw_execution, CapabilityExecution):
        legacy_payload = raw_execution.payload
        execution_stages = raw_execution.stages
        provider_attempts = raw_execution.provider_attempts
        attempts_authoritative = True
    else:
        legacy_payload = raw_execution
        execution_stages = ("provider_attempt",)
        provider_attempts = ()
        attempts_authoritative = False
    normalization_payload = legacy_payload
    if attempts_authoritative:
        normalization_payload = {
            **legacy_payload,
            "_v3_provider_attempts": list(provider_attempts),
        }
    response = adapter.normalize(request, plan, normalization_payload)
    if attempts_authoritative:
        response = replace(response, provider_attempts=list(provider_attempts))
    routing_receipt = {**response.routing_receipt, "mode": policy_mode}
    if policy_mode == "shadow":
        shadow_observation = _shadow_intent_observation(
            routing_receipt.get("selected_provider")
        )
    else:
        shadow_observation = None
        if "shadow_observation" in routing_receipt:
            routing_receipt["shadow_observation"] = None
    response = replace(response, routing_receipt=routing_receipt)
    response = replace(
        response,
        routing_receipt=complete_routing_receipt_v3(
            response.routing_receipt,
            list(response.provider_attempts),
            shadow_observation=shadow_observation,
        ),
    )
    if cache_mode == "bypass":
        response = replace(response, cache_status={"disposition": "bypassed"})
    if adapter.finalize_response is not None:
        response = adapter.finalize_response(request, plan, response, runtime_config)
    if response.capability is not request.capability:
        raise ValueError("adapter returned response for another capability")
    if tuple(response.routing_receipt["candidate_order"]) != plan.candidate_order:
        raise ValueError("response candidate_order drifted from authoritative plan")
    executed_stage_set = {
        "normalize",
        "validate",
        "candidate_plan",
        *execution_stages,
        "result_normalization",
        "response_v3",
    }
    if cache_enabled:
        executed_stage_set.add("cache_lookup")
        if response.status is not ResponseStatus.FAILED:
            try:
                response_cache.put(
                    request,
                    response.to_dict(),
                    now=int(time.time()),
                    legacy_payload=legacy_payload,
                )
                executed_stage_set.add("cache_write")
            except OSError:
                response = replace(
                    response,
                    cache_status={
                        **response.cache_status,
                        "write_error": "write_failed",
                    },
                )
    stage_trace = tuple(
        stage for stage in PIPELINE_STAGES if stage in executed_stage_set
    )
    _append_operator_receipt(response, response_cache.root, v3_config)
    return ExecutedV3(
        response=response,
        plan=plan,
        legacy_payload=copy.deepcopy(legacy_payload),
        stage_trace=stage_trace,
    )
