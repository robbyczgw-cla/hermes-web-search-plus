"""Explicit-only Octen source-search provider via Monid for Web Search Plus.

The adapter calls Octen's Web Search endpoint through Monid's HTTP API and
deliberately excludes answer, broad-search, and full-content modes. WSP remains
the routing and evidence layer; Octen contributes ranked source results only.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wsp_sdk import (
    ProviderConfigError,
    ProviderRequestError,
    ProviderSpec,
    register_provider,
    search_result,
    source_result,
)

_API_URL = "https://api.monid.ai/v1/run"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_FRESHNESS_VALUES = {"day", "week", "month", "year"}


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the API key on the fixed Monid origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _open_request(request: Request, timeout: int):
    return _OPENER.open(request, timeout=timeout)


def _timeout(config: Mapping[str, Any]) -> int:
    section = config.get("octen", {})
    raw = section.get("timeout", 30) if isinstance(section, Mapping) else 30
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ProviderConfigError("octen_timeout_invalid") from None
    if not 1 <= value <= 120:
        raise ProviderConfigError("octen_timeout_invalid")
    return value


def _clean_domains(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _retry_after(error: HTTPError) -> float | None:
    if error.code != 429:
        return None
    try:
        value = error.headers.get("Retry-After")
        if value is None:
            return None
        parsed = float(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _status(code: Any) -> tuple[int | None, bool]:
    if isinstance(code, bool) or not isinstance(code, int):
        return None, False
    return code, code in _TRANSIENT_STATUS


def _read_payload(response: Any) -> dict[str, Any]:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProviderRequestError("octen_response_too_large", transient=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderRequestError("octen_invalid_response", transient=True) from None
    if not isinstance(payload, dict):
        raise ProviderRequestError("octen_invalid_response", transient=True)
    return payload


def _request(body: Mapping[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    envelope = {"provider": "octen", "endpoint": "/search", "input": dict(body)}
    request = Request(
        _API_URL,
        data=json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _open_request(request, timeout) as response:
            payload = _read_payload(response)
    except HTTPError as exc:
        status_code, transient = _status(exc.code)
        raise ProviderRequestError(
            f"octen_http_{exc.code}",
            status_code=status_code,
            transient=transient,
            retry_after=_retry_after(exc),
        ) from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise ProviderRequestError("octen_unavailable", transient=True) from None

    if payload.get("provider") != "octen" or payload.get("endpoint") != "/search":
        raise ProviderRequestError("octen_monid_invalid_envelope", transient=True)

    run_status = payload.get("status")
    if run_status == "FAILED":
        raise ProviderRequestError("octen_monid_failed", status_code=500, transient=True)
    if run_status != "COMPLETED":
        raise ProviderRequestError("octen_monid_not_completed", transient=True)

    provider_response = payload.get("providerResponse")
    provider_status = (
        provider_response.get("httpStatus") if isinstance(provider_response, Mapping) else None
    )
    if isinstance(provider_status, int) and not isinstance(provider_status, bool):
        if not 200 <= provider_status < 300:
            status_code, transient = _status(provider_status)
            raise ProviderRequestError(
                f"octen_provider_http_{provider_status}",
                status_code=status_code,
                transient=transient,
            )
    else:
        raise ProviderRequestError("octen_monid_invalid_response", transient=True)

    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise ProviderRequestError("octen_monid_invalid_response", transient=True)
    code = output.get("code")
    if code != 0:
        status_code, transient = _status(code)
        suffix = code if isinstance(code, int) and not isinstance(code, bool) else "unknown"
        raise ProviderRequestError(
            f"octen_api_{suffix}",
            status_code=status_code,
            transient=transient,
        )
    return payload


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _metadata(envelope: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    run_id = envelope.get("runId")
    if isinstance(run_id, str) and run_id:
        output["monid_run_id"] = run_id

    request_id = payload.get("request_id")
    if isinstance(request_id, str) and request_id:
        output["request_id"] = request_id

    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        return output
    latency = meta.get("latency")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool):
        output["latency_ms"] = latency

    usage = meta.get("usage")
    if isinstance(usage, Mapping):
        projected_usage: dict[str, int] = {}
        queries = usage.get("num_search_queries")
        tokens = usage.get("full_content_tokens")
        if isinstance(queries, int) and not isinstance(queries, bool) and queries >= 0:
            projected_usage["search_queries"] = queries
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            projected_usage["full_content_tokens"] = tokens
        if projected_usage:
            output["usage"] = projected_usage

    billing = envelope.get("billing")
    actual_cost = billing.get("actualCost") if isinstance(billing, Mapping) else None
    if isinstance(actual_cost, Mapping):
        value = actual_cost.get("value")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and actual_cost.get("unit") == "MICRO_DOLLAR"
        ):
            output["cost_usd"] = value / 1_000_000
    return output


def execute_search(search_module, prov, args, key, config, routing_info):
    if not isinstance(key, str) or not key.strip():
        raise ProviderConfigError("monid_api_key_required")

    count = max(1, min(int(args.max_results), 100))
    body: dict[str, Any] = {
        "query": str(args.query),
        "count": count,
        # Octen also supports a news topic. WSP's public SDK does not yet expose
        # native search-vertical metadata, so this first adapter stays truthful
        # and uses general search even when a caller requests search_type=news.
        "topic": "general",
        "highlight": {"enable": True, "max_tokens": 300},
        "full_content": {"enable": False},
        "format": "text",
    }
    include_domains = _clean_domains(getattr(args, "include_domains", None))
    exclude_domains = _clean_domains(getattr(args, "exclude_domains", None))
    if include_domains:
        body["include_domains"] = include_domains
    if exclude_domains:
        body["exclude_domains"] = exclude_domains

    freshness = getattr(args, "freshness", None)
    if freshness not in _FRESHNESS_VALUES:
        freshness = getattr(args, "time_range", None)
    if freshness in _FRESHNESS_VALUES:
        body["time_range"] = freshness

    envelope = _request(body, key.strip(), _timeout(config))
    payload = envelope.get("output")
    if not isinstance(payload, Mapping):
        raise ProviderRequestError("octen_monid_invalid_response", transient=True)
    data = payload.get("data")
    raw_results = data.get("results") if isinstance(data, Mapping) else None
    if not isinstance(raw_results, list):
        raise ProviderRequestError("octen_invalid_response", transient=True)

    projected = []
    for item in raw_results[:count]:
        if not isinstance(item, Mapping):
            continue
        url = _string(item.get("url")).strip()
        if not url.startswith(("https://", "http://")):
            continue
        fields: dict[str, Any] = {
            "title": _string(item.get("title")),
            "snippet": _string(item.get("highlight")),
        }
        optional = {
            "date": item.get("time_published"),
            "author": item.get("authors"),
            "favicon": item.get("favicon"),
            "last_crawled": item.get("time_last_crawled"),
        }
        for field, value in optional.items():
            if isinstance(value, str) and value:
                fields[field] = value
        projected.append(source_result(url, **fields))

    return search_result(
        prov,
        str(args.query),
        projected,
        metadata=_metadata(envelope, payload),
    )


PROVIDER = register_provider(
    ProviderSpec(
        id="octen",
        kind="search",
        env_var="MONID_API_KEY",
        display_name="Octen via Monid",
        description="Explicit-only Octen source-result web search through Monid, with native recency and domain filtering.",
        config_section="octen",
        capability_labels=("search", "freshness"),
        upstream_capabilities=("search", "highlights", "freshness", "domain-filtering"),
        auto_allowed_by_default=False,
        recommended=False,
        supports_freshness=True,
        free_tier="No free-tier claim; Monid API key and wallet balance required",
        signup_url="https://app.monid.ai/access/api-keys",
        execute_search=execute_search,
    )
)
