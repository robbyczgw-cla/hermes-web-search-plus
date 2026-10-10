"""Extraction orchestrator for Web Search Plus."""

import hashlib
import ipaddress
import os
import re
import socket
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse  # noqa: F401 - kept for downstream imports

from .budget_preflight_v3 import daily_preflight_budget as _daily_preflight_budget

from .config import (
    add_provider_setup_guidance,
    ProviderConfigError,
    SELF_HOSTED_EXTRACT_PROVIDER_IDS,
    get_api_key,
    validate_api_key,
    is_self_hosted_profile,
    keyless_public_allowed,
    load_config,
)
from .cache import CACHE_DIR
from .cache_identity_v3 import ExtractionCacheIdentityV3
from .bounded_context_v3 import (
    DEFAULT_FULL_TEXT_MAX_BYTES,
    DEFAULT_FULL_TEXT_TTL_SECONDS,
    FullTextStore,
    apply_bounded_context,
    prepare_extract_request,
)
from .attempt_engine_v3 import AttemptEngine, provider_attempt_context, request_deadline
from .errors_v3 import ProviderContractFailure
from .http_client import ProviderRequestError
# Provider dispatch resolves implementations late through this module.
from . import providers as _providers
from .provider_adapter_protocol import validate_adapter_result
from .provider_dispatch import EXTRACT_DISPATCH
from .provider_registry import (
    DEFAULT_AUTO_ALLOW,
    EXTRACT_PROVIDER_IDS,
    PROVIDER_SPECS,
)
from .compat_v3 import legacy_request_to_v3, v3_response_to_legacy_extract
from .contract_v3 import Capability, RequestV3, ResponseV3, SkipReason
from .orchestrator_v3 import (
    CapabilityAdapter,
    CapabilityExecution,
    ProviderPlan,
    execute_v3_request,
)
from .runtime_v3 import response_from_legacy
from .state_store_v3 import SQLiteStateStore
from .urls import canonical_url


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


_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_NUMERIC_LABEL = re.compile(r"^(?:0[xX][0-9a-fA-F]*|[0-9]+)$")
_STRICT_IPV4 = re.compile(r"^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?$")


def _is_private_or_internal_ip(value: str) -> bool:
    """True for any address that is not plainly public, including embedded IPv4."""
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped
        if embedded is None and int(ip) >> 32 == 0:
            # Deprecated IPv4-compatible ::/96 (::127.0.0.1); ::1 and :: are caught below.
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is None and int(ip) >> 32 == 0xFFFF0000:
            # SIIT ::ffff:0:a.b.c.d
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip.is_site_local or ip.is_reserved:
            return True
        if embedded is None and ip in _NAT64_PREFIX:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is None and ip.sixtofour is not None:
            embedded = ip.sixtofour
        if embedded is None and ip.teredo is not None:
            return True
        if embedded is not None:
            return _is_private_or_internal_ip(str(embedded))
    return (not ip.is_global) or ip.is_multicast or ip.is_reserved


def _reject_url(url: str, reason: str) -> ExtractUrlSecurityError:
    shown = url if len(url) <= 120 else url[:117] + "..."
    shown = "".join(ch if ch.isprintable() else "?" for ch in shown)
    return ExtractUrlSecurityError(f"Extraction URL blocked: {reason}: {shown}")


_IDNA_DEVIATION_CHARS = frozenset("\u00df\u03c2\u200c\u200d")


def _label_scripts(label: str) -> set:
    """Scripts used by the letters of a label (Japanese Han/kana count as one)."""
    scripts = set()
    for ch in label:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0]
        scripts.add("JAPANESE" if name in {"CJK", "HIRAGANA", "KATAKANA"} else name)
    return scripts


def _idn_to_ascii_url(url: str) -> str:
    """Return ``url`` with a non-ASCII host converted to its punycode form.

    The converted URL is what gets validated *and* what the fetcher receives, so
    both sides read the same host. With the optional ``idna`` package conversion
    is IDNA 2008 (as browsers do, so ``straße.de`` stays distinct from
    ``strasse.de``); without it the stdlib codec is used and the few characters
    where the two standards differ are refused. Compatibility characters such as
    fullwidth letters or dot variants are rejected, not mapped.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, tail_sep, remainder = rest.partition("/")
    for mark in ("?", "#"):
        if mark in authority:
            authority, _, extra = authority.partition(mark)
            remainder = mark + extra + (tail_sep + remainder if tail_sep else "")
            tail_sep = ""
            break
    if "@" in authority or authority.startswith("[") or "%" in authority:
        return url
    host, colon, port = authority.partition(":")
    if host.isascii():
        return url
    try:  # optional: IDNA 2008 like browsers; the stdlib codec is IDNA 2003
        import idna
    except ImportError:
        idna = None
    labels = []
    for label in host.split("."):
        if label.isascii():
            labels.append(label)
            continue
        label = unicodedata.normalize("NFC", label.lower())
        if any(unicodedata.normalize("NFKC", ch) != ch for ch in label):
            raise _reject_url(url, "non-ASCII hostname with compatibility characters")
        if len(_label_scripts(label)) > 1:
            raise _reject_url(url, "hostname label mixes scripts")
        try:
            if idna is not None:
                labels.append(idna.encode(label, uts46=False).decode("ascii"))
            else:
                # IDNA 2003 maps these differently from browsers (ß -> ss); refuse
                # them so the converted URL never names a different site.
                if any(ch in _IDNA_DEVIATION_CHARS for ch in label):
                    raise _reject_url(url, "hostname needs the 'idna' package (deviation character)")
                labels.append(label.encode("idna").decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            if isinstance(exc, ExtractUrlSecurityError):
                raise
            raise _reject_url(url, "invalid internationalized hostname") from None
    return f"{scheme}://{'.'.join(labels)}{colon}{port}{tail_sep}{remainder}"


def _strict_url_host(url: str) -> tuple[str, int]:
    """Return (host, port) using only syntax every URL parser reads the same way.

    The validated string is what a remote or browser fetcher receives, so
    anything Python's urlparse and a WHATWG parser could read differently is
    rejected instead of normalised.
    """
    for ch in url:
        if ch == "\\":
            raise _reject_url(url, "backslash in URL")
        if ord(ch) <= 0x20 or ord(ch) == 0x7F:
            raise _reject_url(url, "whitespace or control character in URL")
        if ord(ch) > 0x7F and unicodedata.category(ch)[0] in {"C", "Z"}:
            raise _reject_url(url, "invisible or separator character in URL")
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() not in {"http", "https"}:
        raise ValueError(f"Invalid URL — must start with http:// or https://: {url}")
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not authority:
        raise ValueError(f"Invalid URL — hostname is required: {url}")
    if "@" in authority:
        raise _reject_url(url, "userinfo is not allowed")
    if authority.startswith("["):
        close = authority.find("]")
        if close == -1:
            raise _reject_url(url, "malformed IPv6 literal")
        host, tail = authority[1:close], authority[close + 1 :]
        if "%" in host:
            raise _reject_url(url, "IPv6 zone identifiers are not allowed")
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise _reject_url(url, "malformed IPv6 literal") from None
        port_text = tail[1:] if tail.startswith(":") else ""
        if tail and not tail.startswith(":"):
            raise _reject_url(url, "malformed authority")
    else:
        host, colon, port_text = authority.partition(":")
        if not colon:
            port_text = ""
        if "%" in host:
            raise _reject_url(url, "percent-escaped hostname")
        if not host.isascii():
            raise _reject_url(url, "non-ASCII hostname (use punycode)")
        host = host.lower()
        bare = host.rstrip(".")
        if not bare:
            raise ValueError(f"Invalid URL — hostname is required: {url}")
        labels = bare.split(".")
        if _NUMERIC_LABEL.match(labels[-1]):
            # Browsers read this as IPv4 in decimal/octal/hex/short forms.
            if not _STRICT_IPV4.match(bare) or any(int(part) > 255 for part in labels):
                raise _reject_url(url, "ambiguous numeric IPv4 host")
        elif not all(_HOST_LABEL.match(label) for label in labels):
            raise _reject_url(url, "invalid hostname")
        host = bare
    if port_text and not (port_text.isascii() and port_text.isdigit() and int(port_text) <= 65535):
        raise _reject_url(url, "invalid port")
    default_port = 443 if scheme.lower() == "https" else 80
    return host, int(port_text) if port_text else default_port


def _validate_extract_urls(urls: List[str], config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Validate extraction target URLs before handing them to remote/local fetchers.

    Provider endpoint URLs are operator-controlled config and are intentionally
    not checked here. This guard only covers user/agent-controlled target URLs.

    Syntax checks always run. Network checks (blocked names, private literals,
    DNS answers) are skipped only for ``extract.allow_private_urls``. A
    pre-flight DNS check cannot stop DNS rebinding or redirects followed by the
    final fetcher; that needs a fetcher-side egress policy.
    """
    config = config or {}
    invalid = [u for u in urls if not (isinstance(u, str) and u.startswith(("http://", "https://")))]
    if invalid:
        raise ValueError(f"Invalid URL(s) — must start with http:// or https://: {invalid}")
    allow_private = _extract_allows_private_urls(config)

    urls = [_idn_to_ascii_url(u) for u in urls]
    for url in urls:
        hostname, port = _strict_url_host(url)
        if allow_private:
            continue
        if hostname in _BLOCKED_EXTRACT_HOSTS or hostname.endswith(".localhost"):
            raise ExtractUrlSecurityError(f"Extraction URL blocked: {hostname} is private/internal")

        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if _is_private_or_internal_ip(hostname):
                raise ExtractUrlSecurityError(f"Extraction URL blocked: {hostname} is private/internal")
            continue

        try:
            resolved_ips = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError) as exc:
            raise ExtractUrlSecurityError(f"Extraction URL blocked: cannot resolve hostname {hostname}") from exc
        for _family, _type, _proto, _canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            if _is_private_or_internal_ip(str(ip)):
                # Never echo the resolved address: it would reveal internal
                # network layout to the model.
                raise ExtractUrlSecurityError(
                    f"Extraction URL blocked: {hostname} resolves to a private/internal address"
                )
    return urls


def _check_extract_urls(
    urls: List[str], config: Optional[Dict[str, Any]] = None
) -> List[tuple]:
    """Validate each URL on its own so one bad URL cannot sink the batch.

    Returns ``(requested_url, validated_url, error)`` per requested URL, in
    order; exactly one of ``validated_url`` and ``error`` is set.
    """
    checked = []
    for url in urls:
        try:
            checked.append((url, _validate_extract_urls([url], config)[0], None))
        except ValueError as exc:  # includes ExtractUrlSecurityError
            checked.append((url, None, str(exc)))
    return checked


_EMPTY_EXTRACT_ERROR = "No content could be extracted from this URL"


def _extract_item_usable(item: Any) -> bool:
    """A per-URL result counts as success only with non-blank content."""
    if not isinstance(item, dict) or item.get("error"):
        return False
    content = item.get("content") or item.get("raw_content") or ""
    return bool(str(content).strip())


def _match_extract_items(urls: List[str], items: List[Any]) -> List[Optional[Dict[str, Any]]]:
    """Pair provider result items with the requested URLs, one item per URL.

    Exact URL first, then canonical URL, then the remaining items in order
    (providers report redirect targets). URLs left without an item get None.
    """
    candidates = [item for item in items if isinstance(item, dict)]
    matched: List[Optional[Dict[str, Any]]] = [None] * len(urls)
    used = set()
    for key in (str, canonical_url):
        for position, url in enumerate(urls):
            if matched[position] is not None:
                continue
            wanted = key(url)
            if not wanted:
                continue
            for index, item in enumerate(candidates):
                if index not in used and key(str(item.get("url") or "")) == wanted:
                    matched[position] = item
                    used.add(index)
                    break
    leftovers = [item for index, item in enumerate(candidates) if index not in used]
    for position in range(len(urls)):
        if matched[position] is None and leftovers:
            matched[position] = leftovers.pop(0)
    return matched


def _extract_error_item(url: str, error: str, item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if item is not None:
        return {**item, "error": item.get("error") or error}
    return {"url": url, "title": "", "content": "", "error": error}


def _extract_plus_core(
    urls: List[str],
    provider: str = "auto",
    output_format: str = "markdown",
    include_images: bool = False,
    include_raw_html: bool = False,
    render_js: bool = False,
    config: Optional[Dict[str, Any]] = None,
    urls_validated: bool = False,
) -> dict:
    """One extraction attempt against one provider.

    The v3 attempt engine owns fallback, retry and circuit state, so failures
    raise instead of moving on to another provider. ``urls_validated`` skips
    the URL check (and its DNS lookups) when the caller already ran it.
    """
    config = config or load_config()
    selected = provider or "auto"
    profile_deviation = (
        is_self_hosted_profile(config)
        and selected != "auto"
        and selected not in SELF_HOSTED_EXTRACT_PROVIDER_IDS
    )
    if not urls:
        return {"provider": selected, "results": [], "error": "No URLs provided", "requested_provider": selected}
    if not urls_validated:
        try:
            urls = _validate_extract_urls(urls, config)
        except (ValueError, ExtractUrlSecurityError) as exc:
            return {"provider": selected, "results": [], "error": str(exc), "requested_provider": selected}
    if selected not in EXTRACT_PROVIDER_PRIORITY:
        return {
            "provider": selected,
            "results": [],
            "error": "All extraction providers failed",
            "fallback_errors": [{"provider": selected, "error": f"Provider {selected} does not support extraction"}],
        }
    key = get_api_key(selected, config)
    keyless_allowed = keyless_public_allowed(selected, config)
    if not key and not keyless_allowed:
        validate_api_key(selected, config)  # raises WSP setup guidance
        raise ProviderConfigError(f"missing API key for {selected}")
    adapter = EXTRACT_DISPATCH.get(selected)
    if adapter is None:
        raise ValueError(f"Unknown extract provider: {selected}")
    # provider_dispatch.EXTRACT_DISPATCH builds provider kwargs; this module is
    # passed so adapters resolve extract_<provider> late.
    result = validate_adapter_result(
        selected,
        "extract",
        adapter(_providers, selected, urls, key, output_format, include_images,
                include_raw_html, render_js, config, keyless_allowed),
    )
    from .jev_optional import filter_extract_results

    res_list, jev_meta = filter_extract_results(result.get("results") or [], config=config)
    result["results"] = res_list
    if jev_meta:
        result.setdefault("metadata", {})["jev_extract_quality"] = jev_meta
        if not res_list:
            raise ProviderContractFailure("jev_extract_quality_rejected")
    if not any(_extract_item_usable(r) for r in res_list):
        # Empty lists and blank pages are failures too, so they fall back
        # and are never cached as a success.
        raise ProviderContractFailure("all_urls_failed")
    result["routing"] = {"provider": selected, "requested_provider": selected, "fallback_used": False, "fallback_errors": []}
    if profile_deviation:
        result.setdefault("metadata", {})["profile_deviation"] = True
    return result


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
    checked = _check_extract_urls(list(request.input["urls"]), config)
    urls = [validated for _url, validated, error in checked if error is None]
    if not urls:
        requested = str(request.routing.get("provider") or "auto")
        messages = list(dict.fromkeys(error for _url, _validated, error in checked))
        return CapabilityExecution(
            payload={
                "provider": requested,
                "results": [],
                "error": "; ".join(messages),
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
    deadline = request_deadline(request.budget)
    daily_budget = _daily_preflight_budget(config)
    # Per-URL state, indexed by position in ``urls``: URLs that failed or came
    # back blank on one provider are retried on the next one.
    pending = list(range(len(urls)))
    succeeded: Dict[int, Dict[str, Any]] = {}
    failed: Dict[int, Dict[str, Any]] = {}
    serving_providers = []

    for provider in plan.candidate_order:
        context = provider_attempt_context(
            store, provider, Capability.EXTRACT, config.get(provider) or {}, get_api_key(provider, config),
            budget_scope=scope, budget_limit_units=budget_limit, deadline_monotonic=deadline, **daily_budget,
        )
        if not pending:
            receipts.append(
                engine.skip(context, SkipReason.POLICY_EXCLUDED).receipt
            )
            continue
        if deadline is not None and time.monotonic() >= deadline:
            receipts.append(
                engine.skip(context, SkipReason.DEADLINE_EXCEEDED).receipt
            )
            continue
        batch = [urls[position] for position in pending]

        def operation(current_provider=provider, batch=batch):
            result = _extract_plus_core(
                urls=batch,
                provider=current_provider,
                output_format=str(options.get("output_format", "markdown")),
                include_images=bool(options.get("include_images", False)),
                include_raw_html=bool(options.get("include_raw_html", False)),
                render_js=bool(options.get("render_js", False)),
                config=dict(config),
                urls_validated=True,
            )
            if result.get("error"):
                raise ProviderRequestError(str(result["error"]), transient=False)
            return result

        attempted = engine.execute(context, operation)
        receipts.append(attempted.receipt)
        if attempted.payload is not None:
            if payload is None:
                payload = attempted.payload
                successful_provider = provider
            still_pending = []
            for position, item in zip(
                pending, _match_extract_items(batch, attempted.payload.get("results") or [])
            ):
                if _extract_item_usable(item):
                    succeeded[position] = (provider, item)
                    if provider not in serving_providers:
                        serving_providers.append(provider)
                else:
                    failed[position] = _extract_error_item(urls[position], _EMPTY_EXTRACT_ERROR, item)
                    still_pending.append(position)
            pending = still_pending
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

    if payload is not None and not succeeded:
        payload = None
    if payload is None:
        payload = {
            "provider": plan.selected_provider,
            "results": [],
            "error": "All extraction providers failed",
            "fallback_errors": fallback_errors,
        }
        add_provider_setup_guidance(payload, "extract", list(plan.candidate_order), config,
                                    requested_provider=str(request.routing.get("provider") or "auto"))
    else:
        if pending or len(serving_providers) > 1 or len(urls) < len(checked):
            # Rebuild the result list in requested-URL order: content from
            # whichever provider served each URL, error items for the rest.
            merged = len(serving_providers) > 1
            results = []
            positions = iter(range(len(urls)))
            for requested_url, _validated, error in checked:
                if error is not None:
                    results.append(_extract_error_item(requested_url, error))
                    continue
                position = next(positions)
                if position in succeeded:
                    provider, item = succeeded[position]
                    if merged:
                        # Ties each item to the attempt that produced it.
                        item = {**item, "provider": provider}
                else:
                    item = failed.get(position) or _extract_error_item(urls[position], _EMPTY_EXTRACT_ERROR)
                results.append(item)
            payload = {**payload, "results": results}
            if merged:
                # The receipt names one selected provider: the last one the
                # fallback reached. Earlier contributors are recorded as
                # attempts with insufficient results.
                successful_provider = serving_providers[-1]
                payload["provider"] = successful_provider
        # Failures after the selected provider (retrying leftover URLs) stay
        # in the attempt receipts; fallback_errors lists what preceded it.
        selected_index = plan.candidate_order.index(successful_provider)
        fallback_errors = [
            entry
            for entry in fallback_errors
            if plan.candidate_order.index(entry["provider"]) < selected_index
        ]
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


def _full_text_store(config: Dict[str, Any]) -> FullTextStore:
    policy = config.get("bounded_context") or {}
    if not isinstance(policy, dict):
        policy = {}
    return FullTextStore(
        Path(policy.get("cache_root") or CACHE_DIR),
        ttl_seconds=int(
            policy.get("full_text_ttl_seconds", DEFAULT_FULL_TEXT_TTL_SECONDS)
        ),
        max_bytes=int(
            policy.get("full_text_max_bytes", DEFAULT_FULL_TEXT_MAX_BYTES)
        ),
    )


def _resolve_full_text_paths(legacy: Dict[str, Any], config: Dict[str, Any]) -> None:
    """Turn projected full-text references into verified file paths in place.

    A path is only exposed when the stored file still reads back with the
    recorded sha256 and length; otherwise the result says nothing is stored.
    """
    store = None
    for result in legacy.get("results") or []:
        info = result.get("full_text") if isinstance(result, dict) else None
        if not isinstance(info, dict):
            continue
        key = info.pop("store_key", None)
        digest = info.get("sha256")
        path = None
        if isinstance(key, str) and key:
            try:
                store = store or _full_text_store(config)
                text = store.lookup(key)
                if (
                    isinstance(text, str)
                    and len(text) == info.get("original_chars")
                    and hashlib.sha256(text.encode("utf-8")).hexdigest() == digest
                ):
                    path = str(store.path_for_key(key))
            except (OSError, ValueError):
                path = None
        info["stored"] = path is not None
        if path is not None:
            info["path"] = path
        else:
            info.pop("sha256", None)


def _finalize_extract_response(
    request: RequestV3,
    response: ResponseV3,
    config: Dict[str, Any],
    *,
    original_request: RequestV3 | None = None,
    context_plan=None,
) -> ResponseV3:
    """Apply the extract envelope before cache write, receipts, and projection."""
    store = _full_text_store(config)
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
            "requested": {
                key: value
                for key, value in request.budget.items()
                if key != "max_wall_time_ms"
            },
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
    request: RequestV3,
    _provider_plan: ProviderPlan,
    _response: ResponseV3,
    legacy_payload: Dict[str, Any],
    config: Dict[str, Any],
) -> bool:
    """Avoid lossy cache projections for partial or provider-specific payloads."""
    # Only cache when every processed URL got real content: an empty list, a
    # blank page or a missing URL would otherwise be served for the full TTL.
    processed_urls = prepare_extract_request(request, config).processed_urls
    results = legacy_payload.get("results")
    if not isinstance(results, list) or not results:
        return False
    if not all(
        _extract_item_usable(item)
        for item in _match_extract_items(processed_urls, results)
    ):
        return False
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
    max_wall_time_ms: Optional[int] = None,
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
            "max_wall_time_ms": max_wall_time_ms,
        },
    )
    execution = execute_v3_request(
        request,
        _extract_adapter(),
        runtime_config,
    )
    legacy = v3_response_to_legacy_extract(execution)
    _resolve_full_text_paths(legacy, runtime_config)
    return legacy
