"""Extraction orchestrator for Web Search Plus."""

import ipaddress
import os
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config import ProviderConfigError, get_api_key, keyless_public_allowed, load_config
from cache import CACHE_DIR
from bounded_context_v3 import (
    DEFAULT_FULL_TEXT_MAX_BYTES,
    DEFAULT_FULL_TEXT_TTL_SECONDS,
    FullTextStore,
    apply_bounded_context,
    prepare_extract_request,
)
from attempt_engine_v3 import AttemptContext, AttemptEngine
from errors_v3 import ProviderContractFailure
from http_client import ProviderRequestError
from provider_health import (
    execute_provider_with_retry,
    mark_provider_failure,
    provider_in_cooldown,
    reset_provider_health,
)
# These imports stay module-level attributes on purpose: search.py's
# _sync_extract_dependencies() overwrites them for monkeypatch compatibility,
# and provider_dispatch adapters resolve them late through this module.
from providers import (  # noqa: F401 - resolved late via EXTRACT_DISPATCH/monkeypatch seams
    extract_exa,
    extract_firecrawl,
    extract_keenable,
    extract_linkup,
    extract_parallel,
    extract_serper,
    extract_tavily,
    extract_you,
)
from provider_adapter_protocol import validate_adapter_result
from provider_dispatch import EXTRACT_DISPATCH
from provider_registry import EXTRACT_PROVIDER_IDS
from compat_v3 import legacy_request_to_v3, v3_response_to_legacy_extract
from contract_v3 import Capability, RequestV3, ResponseV3, SkipReason
from orchestrator_v3 import (
    CapabilityAdapter,
    CapabilityExecution,
    ProviderPlan,
    execute_v3_request,
)
from runtime_v3 import response_from_legacy
from state_store_v3 import SQLiteStateStore


EXTRACT_PROVIDER_PRIORITY = list(EXTRACT_PROVIDER_IDS)


def resolve_extract_provider_priority(config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return the configured extract order, completed with registry defaults.

    Runtime callers may pass hand-built config dictionaries, so invalid,
    duplicate, and search-only entries are ignored defensively here. Persisted
    config is validated more strictly by config.py and setup.py.
    """
    auto_config = config.get("auto_routing", {}) if isinstance(config, dict) else {}
    if not isinstance(auto_config, dict):
        auto_config = {}
    raw_priority = auto_config.get("extract_provider_priority")
    if isinstance(raw_priority, str):
        raw_values = raw_priority.split(",")
    elif isinstance(raw_priority, (list, tuple)):
        raw_values = raw_priority
    else:
        raw_values = []

    providers: List[str] = []
    seen = set()
    allowed = set(EXTRACT_PROVIDER_PRIORITY)
    for raw_provider in raw_values:
        provider = str(raw_provider).strip().lower()
        if provider not in allowed or provider in seen:
            continue
        seen.add(provider)
        providers.append(provider)
    for provider in EXTRACT_PROVIDER_PRIORITY:
        if provider not in seen:
            providers.append(provider)
    return providers


class ExtractUrlSecurityError(ValueError):
    """Raised when an extraction target URL points at an internal resource."""


_BLOCKED_EXTRACT_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.internal",
}


def _extract_allows_private_urls(config: Dict[str, Any]) -> bool:
    extract_config = config.get("extract", {}) if isinstance(config, dict) else {}
    if not isinstance(extract_config, dict):
        return False
    return extract_config.get("allow_private_urls") is True


def _is_private_or_internal_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return (not ip.is_global) or ip.is_multicast


def _validate_extract_urls(urls: List[str], config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Validate extraction target URLs before handing them to remote/local fetchers.

    Provider endpoint URLs are operator-controlled config and are intentionally
    not checked here. This guard only covers user/agent-controlled target URLs.
    """
    config = config or {}
    invalid = [u for u in urls if not (isinstance(u, str) and u.startswith(("http://", "https://")))]
    if invalid:
        raise ValueError(f"Invalid URL(s) — must start with http:// or https://: {invalid}")
    if _extract_allows_private_urls(config):
        return urls

    for url in urls:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            raise ValueError(f"Invalid URL — hostname is required: {url}")
        if hostname in _BLOCKED_EXTRACT_HOSTS:
            raise ExtractUrlSecurityError(f"Extraction URL blocked: {hostname} is private/internal")

        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if _is_private_or_internal_ip(hostname):
                raise ExtractUrlSecurityError(f"Extraction URL blocked: {hostname} is private/internal")
            continue

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            resolved_ips = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ExtractUrlSecurityError(f"Extraction URL blocked: cannot resolve hostname {hostname}") from exc
        for _family, _type, _proto, _canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(sockaddr[0])
            if _is_private_or_internal_ip(str(ip)):
                raise ExtractUrlSecurityError(
                    f"Extraction URL blocked: {hostname} resolves to private/internal IP {ip}"
                )
    return urls


def _extract_plus_core(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    config: Optional[Dict[str, Any]] = None,
    engine_owned_attempt: bool = False,
) -> dict:
    """Extract URL content with provider fallback."""
    config = config or load_config()
    selected = provider or "auto"
    if not urls:
        return {"provider": selected, "results": [], "error": "No URLs provided", "requested_provider": selected}
    try:
        urls = _validate_extract_urls(urls, config)
    except (ValueError, ExtractUrlSecurityError) as exc:
        return {
            "provider": selected,
            "results": [],
            "error": str(exc),
            "requested_provider": selected,
        }
    auto_config = config.get("auto_routing", {})
    disabled_providers = set(auto_config.get("disabled_providers", []))
    base_providers = (
        [selected]
        if engine_owned_attempt
        else resolve_extract_provider_priority(config)
        if selected == "auto"
        else [selected] + [p for p in EXTRACT_PROVIDER_PRIORITY if p != selected]
    )
    providers = [p for p in base_providers if p == selected or p not in disabled_providers]
    errors = []
    cooldown_skips = []
    for prov in providers:
        if prov not in EXTRACT_PROVIDER_PRIORITY:
            errors.append({"provider": prov, "error": f"Provider {prov} does not support extraction"})
            continue
        key = get_api_key(prov, config)
        keyless_allowed = keyless_public_allowed(prov, config)
        if not key and not keyless_allowed:
            if engine_owned_attempt:
                raise ProviderConfigError(f"missing API key for {prov}")
            errors.append({"provider": prov, "error": "missing_api_key"})
            continue
        if not engine_owned_attempt:
            in_cooldown, remaining = provider_in_cooldown(prov)
            if in_cooldown and not (selected != "auto" and prov == selected):
                cooldown_skips.append({"provider": prov, "cooldown_remaining_seconds": remaining})
                continue
        try:
            def execute_extract() -> Dict[str, Any]:
                # Provider-specific kwargs-building lives in
                # provider_dispatch.EXTRACT_DISPATCH; the caller namespace
                # (globals()) is passed so adapters resolve extract_<provider>
                # late and honour monkeypatches synced onto this module.
                adapter = EXTRACT_DISPATCH.get(prov)
                if adapter is None:
                    raise ValueError(f"Unknown extract provider: {prov}")
                return validate_adapter_result(
                    prov,
                    "extract",
                    adapter(
                        globals(),
                        prov,
                        urls,
                        key,
                        output_format,
                        include_images,
                        include_raw_html,
                        render_js,
                        config,
                        keyless_allowed,
                    ),
                )

            result = (
                execute_extract()
                if engine_owned_attempt
                else execute_provider_with_retry(prov, execute_extract)
            )
            res_list = result.get("results") or []
            all_failed = bool(res_list) and all(r.get("error") for r in res_list)
            if all_failed:
                if engine_owned_attempt:
                    raise ProviderContractFailure("all_urls_failed")
                errors.append({
                    "provider": prov,
                    "error": "all_urls_failed",
                    "details": [r.get("error") for r in res_list],
                })
                continue
            if not engine_owned_attempt:
                reset_provider_health(prov)
            result["routing"] = {"provider": prov, "requested_provider": selected, "fallback_used": bool(errors) or bool(cooldown_skips), "fallback_errors": errors}
            if cooldown_skips:
                result["routing"]["cooldown_skips"] = cooldown_skips
            return result
        except Exception as e:
            if engine_owned_attempt:
                raise
            error_msg = str(e)
            cooldown_info = mark_provider_failure(prov, error_msg, retry_after=getattr(e, "retry_after", None))
            errors.append({"provider": prov, "error": error_msg, "cooldown_seconds": cooldown_info.get("cooldown_seconds")})
            continue
    error_result = {"provider": selected, "results": [], "error": "All extraction providers failed", "fallback_errors": errors}
    if cooldown_skips:
        error_result["cooldown_skips"] = cooldown_skips
    return error_result


def _plan_extract_v3(request: RequestV3, config: Dict[str, Any]) -> ProviderPlan:
    selected = str(request.routing.get("provider") or "auto")
    disabled = set((config.get("auto_routing") or {}).get("disabled_providers", []))
    configured = [
        provider
        for provider in resolve_extract_provider_priority(config)
        if provider not in disabled
        and (get_api_key(provider, config) or keyless_public_allowed(provider, config))
    ]
    if selected == "auto":
        candidates = configured
        chosen = candidates[0] if candidates else EXTRACT_PROVIDER_PRIORITY[0]
    else:
        candidates = [selected] + [
            provider for provider in configured if provider != selected
        ]
        chosen = selected
    if not request.routing.get("allow_fallback", True):
        candidates = [chosen]
    return ProviderPlan(tuple(candidates or [chosen]), chosen)


def _execute_extract_v3(
    request: RequestV3, plan: ProviderPlan, config: Dict[str, Any]
) -> CapabilityExecution:
    options = request.options
    try:
        urls = _validate_extract_urls(list(request.input["urls"]), config)
    except (ValueError, ExtractUrlSecurityError) as exc:
        requested = str(request.routing.get("provider") or "auto")
        return CapabilityExecution(
            payload={
                "provider": requested,
                "results": [],
                "error": str(exc),
                "requested_provider": requested,
            },
            stages=(),
        )
    v3_config = config.get("v3") or {}
    state_path = v3_config.get("state_path") or os.path.join(
        str(CACHE_DIR), "v3", "state.sqlite3"
    )
    store = SQLiteStateStore(state_path)
    engine = AttemptEngine(
        store,
        max_attempts=int(v3_config.get("max_attempts_per_provider", 2)),
    )
    budget_limit = int(
        request.budget.get(
            "max_provider_attempts",
            v3_config.get("default_max_provider_attempts", 3),
        )
    )
    scope = request.request_id or plan.execution_id
    receipts = []
    fallback_errors = []
    payload = None
    successful_provider = None

    for provider in plan.candidate_order:
        provider_config = config.get(provider) or {}
        endpoint = str(
            provider_config.get("endpoint")
            or provider_config.get("base_url")
            or provider_config.get("url")
            or f"provider://{provider}/extract"
        )
        credential = get_api_key(provider, config) or f"keyless:{provider}"
        context = AttemptContext(
            provider=provider,
            capability=Capability.EXTRACT,
            endpoint=endpoint,
            credential_fingerprint=store.fingerprint_credential(credential),
            budget_scope=scope,
            budget_window="request",
            budget_limit_units=budget_limit,
        )
        if payload is not None:
            receipts.append(
                engine.skip(context, SkipReason.POLICY_EXCLUDED).receipt
            )
            continue

        def operation(current_provider=provider):
            result = _extract_plus_core(
                urls=urls,
                provider=current_provider,
                output_format=str(options.get("output_format", "markdown")),
                include_images=bool(options.get("include_images", False)),
                include_raw_html=bool(options.get("include_raw_html", False)),
                render_js=bool(options.get("render_js", False)),
                config=dict(config),
                engine_owned_attempt=True,
            )
            if result.get("error"):
                raise ProviderRequestError(str(result["error"]), transient=False)
            return result

        attempted = engine.execute(context, operation)
        receipts.append(attempted.receipt)
        if attempted.payload is not None:
            payload = attempted.payload
            successful_provider = provider
            continue
        error_code = (
            "all_urls_failed"
            if attempted.receipt.error is not None
            and attempted.receipt.error.error_class.value == "provider_contract"
            else attempted.receipt.error.error_class.value
            if attempted.receipt.error is not None
            else attempted.receipt.skip_reason.value
            if attempted.receipt.skip_reason is not None
            else "provider_failed"
        )
        fallback_errors.append({"provider": provider, "error": error_code})

    if payload is None:
        payload = {
            "provider": plan.selected_provider,
            "results": [],
            "error": "All extraction providers failed",
            "fallback_errors": fallback_errors,
        }
    else:
        routing = payload.setdefault("routing", {})
        routing["requested_provider"] = str(
            request.routing.get("provider") or "auto"
        )
        routing["provider"] = successful_provider
        routing["fallback_used"] = successful_provider != plan.selected_provider
        routing["fallback_errors"] = fallback_errors

    stages = ["admission", "provider_attempt"]
    if any(receipt.error is not None for receipt in receipts):
        stages.append("error_classification")
    stages.append("retry_circuit_update")
    if sum(receipt.decision == "attempted" for receipt in receipts) > 1:
        stages.append("fallback")
    stages.append("dedup_fingerprint")
    return CapabilityExecution(
        payload=payload,
        provider_attempts=tuple(receipts),
        stages=tuple(stages),
    )


def _extract_adapter() -> CapabilityAdapter:
    return CapabilityAdapter(
        capability=Capability.EXTRACT,
        plan=_plan_extract_v3,
        execute=_execute_extract_v3,
        normalize=response_from_legacy,
    )


def run_extract_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3:
    """Execute a native extract RequestV3 through the canonical orchestrator."""
    runtime_config = config or load_config()
    bounded_plan = prepare_extract_request(request, runtime_config)
    response = execute_v3_request(
        bounded_plan.request, _extract_adapter(), runtime_config
    ).response
    policy = runtime_config.get("bounded_context") or {}
    if not isinstance(policy, dict):
        policy = {}
    cache_root = Path(policy.get("cache_root") or CACHE_DIR)
    store = FullTextStore(
        cache_root,
        ttl_seconds=int(
            policy.get("full_text_ttl_seconds", DEFAULT_FULL_TEXT_TTL_SECONDS)
        ),
        max_bytes=int(
            policy.get("full_text_max_bytes", DEFAULT_FULL_TEXT_MAX_BYTES)
        ),
    )
    return apply_bounded_context(response, request, bounded_plan, store=store)


def extract_plus(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> dict:
    """Legacy extract projection over the sole native v3 execution path."""
    selected = provider or "auto"
    if not urls:
        return {"provider": selected, "results": [], "error": "No URLs provided", "requested_provider": selected}
    runtime_config = config or load_config()
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {
            "urls": urls,
            "provider": provider,
            "output_format": output_format,
            "include_images": include_images,
            "include_raw_html": include_raw_html,
            "render_js": render_js,
        },
    )
    bounded_plan = prepare_extract_request(request, runtime_config)
    execution = execute_v3_request(
        bounded_plan.request, _extract_adapter(), runtime_config
    )
    return v3_response_to_legacy_extract(execution)
