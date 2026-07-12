"""Canonical Web Search Plus v3 orchestration boundary.

The orchestrator owns the sole execution entrance. Capability adapters contain
provider-specific calls and normalization only; legacy callers are projected to
RequestV3 before they can reach this function.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Tuple

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
    mode: str = "classic"

    def __post_init__(self) -> None:
        if self.mode not in {"classic", "shadow"}:
            raise ValueError("provider plan mode must be classic or shadow")
        if not self.candidate_order:
            raise ValueError("provider plan requires candidates")
        if self.selected_provider not in self.candidate_order:
            raise ValueError("selected provider must be in candidate_order")


PlanFn = Callable[[RequestV3, Dict[str, Any]], ProviderPlan]
ExecuteFn = Callable[[RequestV3, ProviderPlan, Dict[str, Any]], Dict[str, Any]]
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
    legacy_payload = adapter.execute(request, plan, runtime_config)
    response = adapter.normalize(request, plan, legacy_payload)
    if response.capability is not request.capability:
        raise ValueError("adapter returned response for another capability")
    if tuple(response.routing_receipt["candidate_order"]) != plan.candidate_order:
        raise ValueError("response candidate_order drifted from authoritative plan")
    return ExecutedV3(
        response=response,
        plan=plan,
        legacy_payload=copy.deepcopy(legacy_payload),
        stage_trace=PIPELINE_STAGES,
    )
