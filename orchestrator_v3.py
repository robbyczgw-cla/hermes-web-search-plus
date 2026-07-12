"""Canonical Web Search Plus v3 orchestration boundary.

The orchestrator owns the sole execution entrance. Capability adapters contain
provider-specific calls and normalization only; legacy callers are projected to
RequestV3 before they can reach this function.
"""

from __future__ import annotations

import copy
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Tuple, Union

from contract_v3 import Capability, RequestV3, ResponseV3


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


@dataclass(frozen=True)
class CapabilityAdapter:
    capability: Capability
    plan: PlanFn
    execute: ExecuteFn
    normalize: NormalizeFn


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
    plan = adapter.plan(request, runtime_config)
    raw_execution = adapter.execute(request, plan, runtime_config)
    if isinstance(raw_execution, CapabilityExecution):
        legacy_payload = raw_execution.payload
        execution_stages = raw_execution.stages
        provider_attempts = raw_execution.provider_attempts
    else:
        legacy_payload = raw_execution
        execution_stages = ("provider_attempt",)
        provider_attempts = ()
    response = adapter.normalize(request, plan, legacy_payload)
    if provider_attempts:
        response = replace(response, provider_attempts=list(provider_attempts))
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
    stage_trace = tuple(
        stage for stage in PIPELINE_STAGES if stage in executed_stage_set
    )
    return ExecutedV3(
        response=response,
        plan=plan,
        legacy_payload=copy.deepcopy(legacy_payload),
        stage_trace=stage_trace,
    )
