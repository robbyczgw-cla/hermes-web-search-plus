"""Extraction orchestrator for Web Search Plus."""

import hashlib
import ipaddress
import os
import re
import socket
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from budget_preflight_v3 import daily_preflight_budget as _daily_preflight_budget

from config import (
    ProviderConfigError,
    SELF_HOSTED_EXTRACT_PROVIDER_IDS,
    get_api_key,
    is_self_hosted_profile,
    keyless_public_allowed,
    load_config,
)
from cache import CACHE_DIR
from cache_identity_v3 import ExtractionCacheIdentityV3
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
from provider_registry import (
    DEFAULT_AUTO_ALLOW,
    EXTRACT_PROVIDER_IDS,
    PROVIDER_SPECS,
)
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


def _extract_provider_auto_allowed(provider: str, auto_config: Dict[str, Any]) -> bool:
    """Gate automatic extraction and fallback without blocking explicit calls."""

    auto_allow = auto_config.get("auto_allow", {}) if isinstance(auto_config, dict) else {}
    default_allowed = bool(DEFAULT_AUTO_ALLOW.get(provider, True))
    if not isinstance(auto_allow, dict):
        return default_allowed
    return bool(auto_allow.get(provider, default_allowed))


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
    if is_self_hosted_profile(config or {}):
        # Profile-owned automatic extraction must not be re-expanded with the
        # normal priority list. Explicit provider= requests are assembled by
        # the caller and remain available when their credentials exist.
        return [
            provider
            for provider in SELF_HOSTED_EXTRACT_PROVIDER_IDS
            if provider in providers or provider in allowed
        ]
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
    profile_deviation = (
        is_self_hosted_profile(config)
        and selected != "auto"
        and selected not in SELF_HOSTED_EXTRACT_PROVIDER_IDS
    )
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
    if not isinstance(auto_config, dict):
        auto_config = {}
    disabled_providers = set(auto_config.get("disabled_providers", []))
    if engine_owned_attempt:
        base_providers = [selected]
    else:
        priority = (
            resolve_extract_provider_priority(config)
            if selected == "auto"
            else EXTRACT_PROVIDER_PRIORITY
        )
        automatic = [
            candidate
            for candidate in priority
            if _extract_provider_auto_allowed(candidate, auto_config)
        ]
        base_providers = (
            automatic
            if selected == "auto"
            else [selected] + [candidate for candidate in automatic if candidate != selected]
        )
    providers = [
        candidate
        for candidate in base_providers
        if candidate == selected or candidate not in disabled_providers
    ]
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
            if profile_deviation:
                result.setdefault("metadata", {})["profile_deviation"] = True
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
    auto_config = config.get("auto_routing") or {}
    if not isinstance(auto_config, dict):
        auto_config = {}
    disabled = set(auto_config.get("disabled_providers", []))
    priority = resolve_extract_provider_priority(config)
    configured = [
        provider
        for provider in priority
        if provider not in disabled
        and (get_api_key(provider, config) or keyless_public_allowed(provider, config))
    ]
    automatic = [
        provider
        for provider in configured
        if _extract_provider_auto_allowed(provider, auto_config)
    ]
    if selected == "auto":
        candidates = automatic
        if not candidates:
            candidates = [
                provider
                for provider in priority
                if provider not in disabled
                and _extract_provider_auto_allowed(provider, auto_config)
            ][:1]
        chosen = candidates[0] if candidates else "auto"
    else:
        candidates = [selected] + [
            provider for provider in automatic if provider != selected
        ]
        chosen = selected
    if not request.routing.get("allow_fallback", True):
        candidates = [chosen] if chosen != "auto" else []
    return ProviderPlan(tuple(candidates), chosen)


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
    max_wall_time_ms = request.budget.get("max_wall_time_ms")
    deadline = (
        time.monotonic() + (max_wall_time_ms / 1000)
        if isinstance(max_wall_time_ms, int)
        and not isinstance(max_wall_time_ms, bool)
        and max_wall_time_ms > 0
        else None
    )
    daily_budget = _daily_preflight_budget(config)

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
            deadline_monotonic=deadline,
            **daily_budget,
        )
        if payload is not None:
            receipts.append(
                engine.skip(context, SkipReason.POLICY_EXCLUDED).receipt
            )
            continue
        if deadline is not None and time.monotonic() >= deadline:
            receipts.append(
                engine.skip(context, SkipReason.DEADLINE_EXCEEDED).receipt
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


def _finalize_extract_response(
    request: RequestV3,
    response: ResponseV3,
    config: Dict[str, Any],
    *,
    original_request: RequestV3 | None = None,
    context_plan=None,
) -> ResponseV3:
    """Apply the extract envelope before cache write, receipts, and projection."""
    policy = config.get("bounded_context") or {}
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
    if isinstance(response.limits_applied.get("extract"), dict):
        if response.cache_status.get("disposition") not in {
            "fresh_hit",
            "stale_hit",
        }:
            return response
        store.enforce_retention()
        stored_content = []
        unavailable_count = 0
        for item in response.stored_content:
            current = dict(item)
            if current.get("storage_succeeded") is True:
                reference = current.get("reference") or {}
                key = reference.get("key") if isinstance(reference, dict) else None
                text = store.lookup(str(key)) if isinstance(key, str) else None
                digest = (
                    hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if isinstance(text, str)
                    else None
                )
                if (
                    digest != current.get("full_text_sha256")
                    or len(text or "") != current.get("full_text_chars")
                ):
                    unavailable_count += 1
                    current.update(
                        {
                            "storage_succeeded": False,
                            "reference": None,
                            "full_text_sha256": None,
                            "full_text_chars": None,
                        }
                    )
            stored_content.append(current)
        if not unavailable_count:
            return response
        warnings = list(response.warnings)
        if not any(
            warning.get("code") == "wsp.storage.full_text_unavailable"
            for warning in warnings
        ):
            warnings.append(
                {
                    "code": "wsp.storage.full_text_unavailable",
                    "message": "Cached full extracted content is no longer available.",
                    "details": {"unavailable_count": unavailable_count},
                }
            )
        return replace(response, stored_content=stored_content, warnings=warnings)
    source_request = original_request or request
    bounded_plan = context_plan or prepare_extract_request(source_request, config)
    return apply_bounded_context(
        response,
        source_request,
        bounded_plan,
        store=store,
    )


def _extract_cache_eligible(
    request: RequestV3, _provider_plan: ProviderPlan, _config: Dict[str, Any]
) -> bool:
    """Only cache extract shapes reconstructible from canonical source evidence."""
    return not (
        request.options.get("include_images")
        or request.options.get("include_raw_html")
    )


def _extract_cache_identity(
    request: RequestV3, _provider_plan: ProviderPlan, config: Dict[str, Any]
) -> RequestV3:
    """Key on the full URL request plus the effective operator bounds."""
    prepared = prepare_extract_request(request, config)
    return replace(
        prepared.request,
        input={**request.input, "urls": list(request.input["urls"])},
    )


def _identity_requested_provider(request) -> str:
    return str(request.routing.get("provider") or "auto")


def _identity_candidate_basis(request, config) -> list:
    """Config-derived candidate list for cache identity.

    Deliberately health-independent: an explicit provider is its own basis;
    auto requests use the configured extraction priority so transient
    cooldowns never change the cache key.
    """
    requested = _identity_requested_provider(request)
    if requested != "auto":
        return [requested]
    return list(resolve_extract_provider_priority(config))


# Config keys that may carry credentials must never enter the cache identity:
# the identity is persisted alongside cached evidence on disk.
_SECRET_SETTING_KEY_PATTERN = re.compile(
    r"key|token|secret|password|credential|auth", re.IGNORECASE
)


def _extract_provider_endpoint_config(
    provider: str, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Return every non-secret extraction adapter setting that affects output."""
    section = config.get(provider) or {}
    if not isinstance(section, dict):
        section = {}
    if provider == "firecrawl":
        return {
            "scrape_url": section.get(
                "scrape_url", "https://api.firecrawl.dev/v2/scrape"
            ),
            "extract_timeout": int(section.get("extract_timeout", 60)),
        }
    if provider == "linkup":
        return {
            "fetch_url": section.get("fetch_url", "https://api.linkup.so/v1/fetch"),
            "timeout": int(section.get("timeout", 30)),
        }
    if provider == "tavily":
        return {
            "extract_url": section.get(
                "extract_url", "https://api.tavily.com/extract"
            ),
            "timeout": int(section.get("timeout", 30)),
        }
    if provider == "exa":
        return {
            "contents_url": section.get("contents_url", "https://api.exa.ai/contents"),
            "timeout": int(section.get("timeout", 30)),
        }
    if provider == "parallel":
        return {
            "extract_url": section.get(
                "extract_url", "https://api.parallel.ai/v1/extract"
            ),
            "extract_timeout": int(
                section.get("extract_timeout", section.get("timeout", 60))
            ),
            "client_model": section.get("client_model"),
            "max_chars_total": int(section.get("max_chars_total", 120000)),
            "max_chars_per_result": int(section.get("max_chars_per_result", 60000)),
        }
    if provider == "keenable":
        return {
            "fetch_url": section.get("fetch_url", "https://api.keenable.ai/v1/fetch"),
            "timeout": int(section.get("timeout", 30)),
            "keyless_public": keyless_public_allowed(provider, config),
        }
    if provider == "serper":
        return {
            "scrape_url": section.get("scrape_url", "https://scrape.serper.dev"),
            "extract_timeout": int(
                section.get("extract_timeout", section.get("timeout", 30))
            ),
        }
    if provider == "you":
        return {
            "contents_url": section.get("contents_url", "https://ydc-index.io/v1/contents"),
            "timeout": int(section.get("timeout", 30)),
        }
    spec = PROVIDER_SPECS.get(provider)
    if spec is not None and spec.supports_extract:
        # Discovered SDK providers get a deterministic identity derived from
        # their spec and the non-secret scalars of their config section, so
        # caching works without enumerating third-party endpoint knobs here.
        section = config.get(spec.config_section) or {}
        if not isinstance(section, dict):
            section = {}
        settings = {
            key: value
            for key, value in sorted(section.items())
            if not _SECRET_SETTING_KEY_PATTERN.search(key)
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        return {
            "sdk_provider": provider,
            "config_section": spec.config_section,
            "settings": settings,
        }
    # The registry is the authoritative provider boundary. An unknown provider
    # must not be silently collapsed into a shared cache identity.
    raise ValueError(f"unknown extraction provider in cache identity: {provider}")


def _extract_cache_vary(
    request: RequestV3, provider_plan: ProviderPlan, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the complete typed identity for request-exact extraction evidence."""
    prepared = prepare_extract_request(request, config)
    policy = config.get("bounded_context") or {}
    if not isinstance(policy, dict):
        policy = {}
    cache_root = Path(policy.get("cache_root") or CACHE_DIR)
    v3_config = config.get("v3") or {}
    if not isinstance(v3_config, dict):
        v3_config = {}
    storage_root = os.path.abspath(os.fspath(cache_root))
    identity = ExtractionCacheIdentityV3(
        requested_urls=tuple(request.input["urls"]),
        attempt_budget={
            "requested": dict(request.budget),
            "effective_max_provider_attempts": int(
                request.budget.get(
                    "max_provider_attempts",
                    v3_config.get("default_max_provider_attempts", 3),
                )
            ),
            "max_attempts_per_provider": int(
                v3_config.get("max_attempts_per_provider", 2)
            ),
        },
        effective_context_limits={
            "max_urls": prepared.max_urls,
            "max_context_chars": prepared.max_context_chars,
        },
        output_format=str(request.options.get("output_format", "markdown")),
        include_images=bool(request.options.get("include_images", False)),
        include_raw_html=bool(request.options.get("include_raw_html", False)),
        render_js=bool(request.options.get("render_js", False)),
        semantic_spans={
            "enabled": request.options.get("spans") is True,
            "query": request.options.get("spans_query"),
            "span_contract_version": 1,
        },
        # Identity captures the request and configuration, never the transient
        # provider plan: cooldown/health changes between two otherwise
        # identical calls must not vary the cache key. The provider that
        # actually served remains recorded in the cached evidence itself.
        provider_selection={
            "requested_provider": _identity_requested_provider(request),
            "allow_fallback": bool(request.routing.get("allow_fallback", True)),
            "candidate_basis": _identity_candidate_basis(request, config),
        },
        provider_endpoint_config={
            provider: _extract_provider_endpoint_config(provider, config)
            for provider in _identity_candidate_basis(request, config)
        },
        url_policy={
            "allow_private_urls": _extract_allows_private_urls(config),
        },
        storage_policy={
            # Retained content references are local to this store. Keep the
            # location opaque even inside the cache envelope.
            "cache_root_fingerprint": hashlib.sha256(
                storage_root.encode("utf-8")
            ).hexdigest(),
            "ttl_seconds": max(
                0,
                int(
                    policy.get(
                        "full_text_ttl_seconds", DEFAULT_FULL_TEXT_TTL_SECONDS
                    )
                ),
            ),
            "max_bytes": max(
                0,
                int(policy.get("full_text_max_bytes", DEFAULT_FULL_TEXT_MAX_BYTES)),
            ),
        },
    )
    return {"extraction_cache_identity": identity.canonical_form()}


def _extract_cache_write_eligible(
    _request: RequestV3,
    _provider_plan: ProviderPlan,
    _response: ResponseV3,
    legacy_payload: Dict[str, Any],
    _config: Dict[str, Any],
) -> bool:
    """Avoid lossy cache projections for partial or provider-specific payloads."""
    # Per-execution provider metadata (upstream request ids, cost accounting,
    # upstream cache statuses) describes ONE live execution. It is never part
    # of the cached evidence and never reproduced on hits, so its presence
    # must not disqualify a write. Everything else unknown stays a blocker.
    execution_metadata_fields = {"request_id", "cost_dollars", "statuses"}
    if set(legacy_payload) - {"provider", "results", "routing"} - execution_metadata_fields:
        return False
    if "request_id" in legacy_payload and not isinstance(
        legacy_payload.get("request_id"), str
    ):
        return False
    if "cost_dollars" in legacy_payload and not isinstance(
        legacy_payload.get("cost_dollars"), dict
    ):
        return False
    if "statuses" in legacy_payload and not isinstance(
        legacy_payload.get("statuses"), list
    ):
        return False
    routing = legacy_payload.get("routing")
    if routing is not None:
        if not isinstance(routing, dict) or set(routing) - {
            "provider",
            "requested_provider",
            "fallback_used",
            "fallback_errors",
        }:
            return False
        if not isinstance(routing.get("provider"), str):
            return False
        if not isinstance(routing.get("requested_provider"), str):
            return False
        if not isinstance(routing.get("fallback_used"), bool):
            return False
        fallback_errors = routing.get("fallback_errors")
        if not isinstance(fallback_errors, list) or any(
            not isinstance(item, dict)
            or set(item) - {"provider", "error"}
            or not isinstance(item.get("provider"), str)
            or not isinstance(item.get("error"), str)
            for item in fallback_errors
        ):
            return False
    cacheable_result_fields = {
        "title",
        "url",
        "content",
        "raw_content",
        "provider",
        # Benign scalar metadata emitted by real providers (e.g. Exa). These
        # round-trip losslessly through the projection hints, so they must not
        # disqualify a write.
        "favicon",
        "published_date",
    }
    for item in legacy_payload.get("results") or []:
        if not isinstance(item, dict) or item.get("error"):
            return False
        if set(item) - cacheable_result_fields:
            return False
        if "provider" in item and not isinstance(item.get("provider"), str):
            return False
        for scalar_field in ("favicon", "published_date"):
            if scalar_field in item and not (
                item.get(scalar_field) is None
                or isinstance(item.get(scalar_field), str)
            ):
                return False
        if "raw_content" in item and item.get("raw_content") != item.get("content"):
            return False
    return True


def _extract_adapter() -> CapabilityAdapter:
    def execute(request, provider_plan, config):
        prepared = prepare_extract_request(request, config)
        return _execute_extract_v3(prepared.request, provider_plan, config)

    def finalize_response(request, _provider_plan, response, config):
        prepared = prepare_extract_request(request, config)
        return _finalize_extract_response(
            request,
            response,
            config,
            original_request=request,
            context_plan=prepared,
        )

    return CapabilityAdapter(
        capability=Capability.EXTRACT,
        plan=_plan_extract_v3,
        execute=execute,
        normalize=response_from_legacy,
        finalize_response=finalize_response,
        cache_eligible=_extract_cache_eligible,
        cache_identity=_extract_cache_identity,
        cache_vary=_extract_cache_vary,
        cache_write_eligible=_extract_cache_write_eligible,
    )


def run_extract_request_v3(
    request: RequestV3,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> ResponseV3:
    """Execute a native extract RequestV3 through the canonical orchestrator."""
    runtime_config = config or load_config()
    return execute_v3_request(request, _extract_adapter(), runtime_config).response


def extract_plus(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    spans: bool = False,
    spans_query: Optional[str] = None,
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
            "spans": spans,
            "spans_query": spans_query,
        },
    )
    execution = execute_v3_request(
        request,
        _extract_adapter(),
        runtime_config,
    )
    return v3_response_to_legacy_extract(execution)
