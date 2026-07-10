"""Extraction orchestrator for Web Search Plus."""

import ipaddress
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config import get_api_key, keyless_public_allowed, load_config
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
from provider_dispatch import EXTRACT_DISPATCH
from provider_registry import EXTRACT_PROVIDER_IDS


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


def extract_plus(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    config: Optional[Dict[str, Any]] = None,
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
    base_providers = resolve_extract_provider_priority(config) if selected == "auto" else [selected] + [p for p in EXTRACT_PROVIDER_PRIORITY if p != selected]
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
            errors.append({"provider": prov, "error": "missing_api_key"})
            continue
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
                return adapter(globals(), prov, urls, key, output_format, include_images, include_raw_html, render_js, config, keyless_allowed)

            result = execute_provider_with_retry(prov, execute_extract)
            res_list = result.get("results") or []
            all_failed = bool(res_list) and all(r.get("error") for r in res_list)
            if all_failed:
                errors.append({
                    "provider": prov,
                    "error": "all_urls_failed",
                    "details": [r.get("error") for r in res_list],
                })
                continue
            reset_provider_health(prov)
            result["routing"] = {"provider": prov, "requested_provider": selected, "fallback_used": bool(errors) or bool(cooldown_skips), "fallback_errors": errors}
            if cooldown_skips:
                result["routing"]["cooldown_skips"] = cooldown_skips
            return result
        except Exception as e:
            error_msg = str(e)
            cooldown_info = mark_provider_failure(prov, error_msg, retry_after=getattr(e, "retry_after", None))
            errors.append({"provider": prov, "error": error_msg, "cooldown_seconds": cooldown_info.get("cooldown_seconds")})
            continue
    error_result = {"provider": selected, "results": [], "error": "All extraction providers failed", "fallback_errors": errors}
    if cooldown_skips:
        error_result["cooldown_skips"] = cooldown_skips
    return error_result
