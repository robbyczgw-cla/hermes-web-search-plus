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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple, Union

import cache as legacy_cache
from budget_preflight_v3 import PreflightDecision, run_budget_preflight
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
from shadow_policy_v3 import evaluate_shadow_policy
from state_store_v3 import SQLiteStateStore


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
CacheEligibilityFn = Callable[
    [RequestV3, ProviderPlan, Dict[str, Any]], bool
]
CacheIdentityFn = Callable[
    [RequestV3, ProviderPlan, Dict[str, Any]], RequestV3
]
CacheVaryFn = Callable[
    [RequestV3, ProviderPlan, Dict[str, Any]], Dict[str, Any]
]
CacheWriteEligibilityFn = Callable[
    [RequestV3, ProviderPlan, ResponseV3, Dict[str, Any], Dict[str, Any]], bool
]


@dataclass(frozen=True)
class CapabilityAdapter:
    capability: Capability
    plan: PlanFn
    execute: ExecuteFn
    normalize: NormalizeFn
    legacy_cache_lookup: LegacyCacheLookupFn | None = None
    finalize_response: FinalizeResponseFn | None = None
    cache_eligible: CacheEligibilityFn | None = None
    cache_identity: CacheIdentityFn | None = None
    cache_vary: CacheVaryFn | None = None
    cache_write_eligible: CacheWriteEligibilityFn | None = None


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
_DAILY_PROVIDER_CALL_SCOPE = "daily_provider_calls"


def _budget_preflight_off(config: Dict[str, Any]) -> bool:
    """Resolve the preflight kill switch with Classic-only precedence rules."""
    raw_override = os.environ.get("WSP_BUDGET_PREFLIGHT_OFF")
    if raw_override is not None:
        normalized = raw_override.strip().strip('"').strip("'").lower()
        if normalized not in _EXPLICIT_FALSE_VALUES:
            return True
    section = config.get("budget_preflight") or {}
    return not isinstance(section, dict) or section.get("enabled") is not True


def _daily_provider_ledger_snapshot(config: Dict[str, Any]) -> Any:
    """Read today's provider-call usage through the no-write state accessor."""
    section = config.get("budget_preflight") or {}
    if not isinstance(section, dict) or section.get("max_daily_provider_calls") is None:
        return {}
    v3_config = config.get("v3") or {}
    state_path = v3_config.get("state_path") or os.path.join(
        str(legacy_cache.CACHE_DIR), "v3", "state.sqlite3"
    )
    store = SQLiteStateStore.open_readonly(state_path)
    if not store.available:
        return None
    record = store.read_budget_snapshot(
        _DAILY_PROVIDER_CALL_SCOPE,
        datetime.now(timezone.utc).date().isoformat(),
    )
    return record or {"used_units": 0, "reserved_units": 0}


def _apply_budget_preflight(
    request: RequestV3, plan: ProviderPlan, decision: PreflightDecision
) -> tuple[RequestV3, ProviderPlan]:
    """Apply only the concrete, deterministic reductions from preflight."""
    if decision.action != "degrade":
        return request, plan
    adjustments = decision.adjustments
    max_calls = adjustments.get("max_provider_calls")
    if max_calls is not None:
        plan = replace(plan, candidate_order=plan.candidate_order[:max_calls])
        budget = dict(request.budget)
        existing = budget.get("max_provider_attempts")
        budget["max_provider_attempts"] = min(
            max_calls,
            existing
            if isinstance(existing, int) and not isinstance(existing, bool) and existing > 0
            else max_calls,
        )
        request = replace(request, budget=budget)
    timeout_seconds = adjustments.get("timeout_seconds")
    if timeout_seconds is not None:
        budget = dict(request.budget)
        deadline_ms = timeout_seconds * 1000
        existing = budget.get("max_wall_time_ms")
        budget["max_wall_time_ms"] = min(
            deadline_ms,
            existing
            if isinstance(existing, int) and not isinstance(existing, bool) and existing > 0
            else deadline_ms,
        )
        options = dict(request.options)
        if str(options.get("mode") or "normal") == "research":
            current = options.get("research_time_budget", 55)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                options["research_time_budget"] = min(current, timeout_seconds)
        request = replace(request, budget=budget, options=options)
    context_limit = adjustments.get("context_limit")
    if context_limit is not None:
        options = dict(request.options)
        current = options.get("max_context_chars")
        options["max_context_chars"] = min(
            context_limit,
            current
            if isinstance(current, int) and not isinstance(current, bool) and current > 0
            else context_limit,
        )
        request = replace(request, options=options)
    return request, plan


def _with_budget_preflight(
    response: ResponseV3, decision: PreflightDecision
) -> ResponseV3:
    """Attach typed preflight evidence only when it changed execution."""
    if decision.action == "proceed":
        return response
    routing_receipt = {
        **response.routing_receipt,
        "budget_preflight": decision.to_dict(),
    }
    return replace(
        response,
        routing_receipt=routing_receipt,
        policy_actions=[
            *response.policy_actions,
            {
                "action": "budget_preflight",
                "observation_id": None,
                "reason": "degraded" if decision.action == "degrade" else "aborted",
            },
        ],
    )


def _budget_preflight_failure(
    request: RequestV3,
    plan: ProviderPlan,
    decision: PreflightDecision,
    response_cache: ResponseCacheV3,
    v3_config: Dict[str, Any],
) -> ExecutedV3:
    """Return an honest, zero-attempt budget rejection before adapter execution."""
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
        cache_status={"disposition": "bypassed"},
        error=ErrorV3(
            error_class=ErrorClass.BUDGET,
            code="wsp.budget.preflight",
            message="Request exceeds the configured execution budget.",
            retryable=False,
        ),
    )
    response = _with_budget_preflight(response, decision)
    response = replace(
        response,
        routing_receipt=complete_routing_receipt_v3(
            response.routing_receipt, []
        ),
    )
    _append_operator_receipt(response, response_cache.root, v3_config)
    return ExecutedV3(
        response=response,
        plan=plan,
        legacy_payload={
            "error": "Request exceeds the configured execution budget",
            "provider": plan.selected_provider,
            "results": [],
        },
        stage_trace=("normalize", "validate", "candidate_plan", "response_v3"),
    )


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


def _is_auto_search_plan(request: RequestV3, plan: ProviderPlan) -> bool:
    """Return whether Classic selected the provider at the search plan boundary."""
    return (
        request.capability is Capability.SEARCH
        and str(request.routing.get("provider") or "auto") == "auto"
        and plan.routing_metadata.get("auto_routed") is not False
    )


def _shadow_observation(
    request: RequestV3,
    plan: ProviderPlan,
    config: Dict[str, Any],
    *,
    selected_provider: str | None,
) -> Dict[str, Any]:
    """Evaluate auto-routed searches, preserving the 3.0 stub on any failure."""
    if not _is_auto_search_plan(request, plan):
        return _shadow_intent_observation(selected_provider)
    try:
        return evaluate_shadow_policy(request, plan, config)
    except Exception:
        # Shadow observation is strictly observational and cannot break Classic.
        return _shadow_intent_observation(selected_provider)


def _record_shadow_observation(
    observation: Dict[str, Any] | None,
    plan: ProviderPlan,
    v3_config: Dict[str, Any],
) -> None:
    """Persist an extended shadow observation without changing the response."""
    extended_fields = {
        "observed",
        "policy_id",
        "policy_revision",
        "selected_provider",
        "shadow_provider",
        "agreement",
        "affected_execution",
    }
    if not isinstance(observation, dict) or set(observation) != extended_fields:
        return
    try:
        state_path = v3_config.get("state_path") or os.path.join(
            str(legacy_cache.CACHE_DIR), "v3", "state.sqlite3"
        )
        routing_summary = plan.routing_metadata.get("analysis_summary") or {}
        routing_class = str(routing_summary.get("routing_class") or "general")
        SQLiteStateStore(state_path).record_shadow_evaluation(
            routing_class=routing_class,
            classic_provider=observation["selected_provider"],
            shadow_provider=observation["shadow_provider"],
            agreement=observation["agreement"],
            policy_id=observation["policy_id"],
            policy_revision=observation["policy_revision"],
        )
    except Exception:
        # Operator evidence is strictly best-effort and must never alter execution.
        return


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
    if request.capability is Capability.SEARCH:
        from jev_optional import maybe_search_type

        resolved, _jev_meta = maybe_search_type(
            str(request.input.get("query") or ""),
            request.options.get("search_type"),
            config=runtime_config,
        )
        if resolved != request.options.get("search_type"):
            request = replace(
                request,
                options={**request.options, "search_type": resolved},
            )
    policy_mode = _effective_policy_mode(request, runtime_config)
    if request.routing.get("policy_mode") != policy_mode:
        request = replace(
            request,
            routing={**request.routing, "policy_mode": policy_mode},
        )
    plan = adapter.plan(request, runtime_config)
    if plan.mode != policy_mode:
        plan = replace(plan, mode=policy_mode)
    preflight = PreflightDecision("proceed")
    if not _budget_preflight_off(runtime_config):
        preflight = run_budget_preflight(
            request,
            plan,
            runtime_config,
            _daily_provider_ledger_snapshot(runtime_config),
        )
        v3_config = runtime_config.get("v3") or {}
        response_cache = ResponseCacheV3(
            v3_config.get("cache_dir") or legacy_cache.CACHE_DIR
        )
        if preflight.action == "abort":
            return _budget_preflight_failure(
                request, plan, preflight, response_cache, v3_config
            )
        request, plan = _apply_budget_preflight(request, plan, preflight)
    cache_mode = str(request.cache.get("mode") or "prefer")
    cache_allowed = True
    if cache_mode != "bypass" and adapter.cache_eligible is not None:
        cache_allowed = adapter.cache_eligible(request, plan, runtime_config)
    cache_enabled = cache_mode != "bypass" and cache_allowed
    cache_request = (
        adapter.cache_identity(request, plan, runtime_config)
        if cache_enabled and adapter.cache_identity is not None
        else request
    )
    if cache_request.capability is not request.capability:
        raise ValueError("cache identity capability differs from execution request")
    cache_vary = (
        adapter.cache_vary(request, plan, runtime_config)
        if cache_enabled and adapter.cache_vary is not None
        else {}
    )
    if not isinstance(cache_vary, dict):
        raise ValueError("cache vary material must be an object")
    v3_config = runtime_config.get("v3") or {}
    response_cache = ResponseCacheV3(
        v3_config.get("cache_dir") or legacy_cache.CACHE_DIR
    )
    if cache_enabled:
        lookup = response_cache.get(
            cache_request,
            ttl_seconds=int(request.cache.get("ttl_seconds", 3600)),
            allow_stale_seconds=int(request.cache.get("allow_stale_seconds", 0)),
            now=int(time.time()),
            vary=cache_vary,
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
                    # A cache hit has no current routing decision to observe.
                    cached_routing["shadow_observation"] = None
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
                cached_response = _with_budget_preflight(cached_response, preflight)
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
                    _shadow_observation(
                        request,
                        plan,
                        runtime_config,
                        selected_provider=None,
                    )
                    if policy_mode == "shadow"
                    else None
                ),
            ),
        )
        response = _with_budget_preflight(response, preflight)
        _record_shadow_observation(
            response.routing_receipt["shadow_observation"], plan, v3_config
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
        shadow_observation = _shadow_observation(
            request,
            plan,
            runtime_config,
            selected_provider=routing_receipt.get("selected_provider"),
        )
    else:
        shadow_observation = None
        if "shadow_observation" in routing_receipt:
            routing_receipt["shadow_observation"] = None
    response = replace(response, routing_receipt=routing_receipt)
    response = _with_budget_preflight(response, preflight)
    response = replace(
        response,
        routing_receipt=complete_routing_receipt_v3(
            response.routing_receipt,
            list(response.provider_attempts),
            shadow_observation=shadow_observation,
        ),
    )
    _record_shadow_observation(shadow_observation, plan, v3_config)
    if cache_mode == "bypass" or (cache_mode == "prefer" and not cache_allowed):
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
        cache_write_allowed = (
            adapter.cache_write_eligible(
                request, plan, response, legacy_payload, runtime_config
            )
            if adapter.cache_write_eligible is not None
            else True
        )
        if response.status is not ResponseStatus.FAILED and cache_write_allowed:
            try:
                response_cache.put(
                    cache_request,
                    response.to_dict(),
                    now=int(time.time()),
                    legacy_payload=legacy_payload,
                    vary=cache_vary,
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
