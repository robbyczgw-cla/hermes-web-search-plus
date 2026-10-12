"""Kagi Search provider for Web Search Plus (zero-core-edit, auto-discovered).

Calls Kagi's v1 Search API (POST /api/v1/search) with the ``Authorization:
Bot <token>`` header. Returns source-only results (web + news). Freshness,
region, and domain filtering map onto Kagi's ``filters`` / inline ``lens``
objects; requesting raw content triggers Kagi's inline ``extract`` so the
markdown lands in each result's ``snippet``.

The module is picked up automatically by ``provider_registry.discover_providers``
— no core edit required.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from wsp_sdk import (
    ProviderConfigError,
    ProviderRequestError,
    ProviderSpec,
    register_provider,
    search_result,
    source_result,
)

_API_URL = "https://kagi.com/api/v1/search"
_MAX_BYTES = 8 * 1024 * 1024
_TRANSIENT = {429, 500, 502, 503, 504}
_FRESHNESS = {"day", "week", "month", "year"}


def _timeout(config: Mapping[str, Any]) -> int:
    section = config.get("kagi", {}) if isinstance(config, Mapping) else {}
    raw = section.get("timeout", 30)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ProviderConfigError("kagi_timeout_invalid") from None
    if not 1 <= value <= 120:
        raise ProviderConfigError("kagi_timeout_invalid")
    return value


def _clean_domains(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _read(response: Any) -> dict[str, Any]:
    raw = response.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ProviderRequestError("kagi_response_too_large", transient=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderRequestError("kagi_invalid_response", transient=True) from None
    if not isinstance(payload, dict):
        raise ProviderRequestError("kagi_invalid_response", transient=True)
    return payload


def _request(body: Mapping[str, Any], key: str, timeout: int) -> dict[str, Any]:
    request = Request(
        _API_URL,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bot {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = _read(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise ProviderConfigError("kagi_api_key_invalid") from None
        transient = exc.code in _TRANSIENT
        raise ProviderRequestError(
            f"kagi_http_{exc.code}", status_code=exc.code, transient=transient
        ) from None
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ProviderRequestError("kagi_unavailable", transient=True) from exc

    if payload.get("error"):
        err = payload["error"]
        code = err[0].get("code") if isinstance(err, list) and err else None
        if code == 2:
            raise ProviderConfigError("kagi_api_key_invalid")
        raise ProviderRequestError(f"kagi_api_error_{code}", status_code=400, transient=False)
    return payload


def execute_search(search_module, prov, args, key, config, routing_info):
    if not isinstance(key, str) or not key.strip():
        raise ProviderConfigError("kagi_api_key_required")
    key = key.strip()

    count = max(1, min(int(getattr(args, "max_results", 10) or 10), 50))
    workflow = "news" if getattr(args, "search_type", None) == "news" else "search"

    body: dict[str, Any] = {
        "query": str(args.query),
        "limit": count,
        "workflow": workflow,
    }

    kagi_cfg = config.get("kagi", {}) if isinstance(config, Mapping) else {}
    if kagi_cfg.get("safe_search") is not None:
        body["safe_search"] = bool(kagi_cfg["safe_search"])

    fresh = getattr(args, "freshness", None) or getattr(args, "time_range", None)
    if fresh in _FRESHNESS:
        body.setdefault("filters", {})["time_relative"] = fresh

    country = getattr(args, "country", None)
    if country:
        body.setdefault("filters", {})["region"] = str(country).upper()

    inc = _clean_domains(getattr(args, "include_domains", None))
    exc = _clean_domains(getattr(args, "exclude_domains", None))
    if inc or exc:
        lens: dict[str, Any] = {}
        if inc:
            lens["sites_included"] = inc
        if exc:
            lens["sites_excluded"] = exc
        body["lens"] = lens

    if getattr(args, "raw_content", False) or getattr(args, "include_raw_content", False):
        body["extract"] = {"count": min(count, 10)}

    payload = _request(body, key, _timeout(config))
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}

    raw_results: list[Any] = []
    for bucket in ("search", "news", "interesting_finds"):
        items = data.get(bucket)
        if isinstance(items, list):
            raw_results.extend(items)

    projected = []
    for item in raw_results[:count]:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        fields: dict[str, Any] = {
            "title": item.get("title") or "",
            "snippet": item.get("snippet") or "",
        }
        t = item.get("time")
        if isinstance(t, str) and t:
            fields["date"] = t
        img = item.get("image")
        if isinstance(img, Mapping) and isinstance(img.get("url"), str):
            fields["thumbnail"] = img["url"]
        props = item.get("props") if isinstance(item.get("props"), Mapping) else {}
        lang = props.get("language")
        if isinstance(lang, str) and lang:
            fields["language"] = lang
        projected.append(source_result(url, **fields))

    meta: dict[str, Any] = {}
    m = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    if isinstance(m.get("ms"), int):
        meta["latency_ms"] = m["ms"]
    if isinstance(m.get("trace"), str):
        meta["trace"] = m["trace"]

    return search_result(prov, str(args.query), projected, metadata=meta)


PROVIDER = register_provider(
    ProviderSpec(
        id="kagi",
        kind="search",
        env_var="KAGI_API_KEY",
        display_name="Kagi Search",
        description=(
            "Privacy-first Kagi v1 Search API: web and news results with freshness, "
            "region, and domain filtering, plus inline markdown extract on request."
        ),
        config_section="kagi",
        capability_labels=("search", "news", "freshness", "privacy"),
        upstream_capabilities=(
            "search",
            "news",
            "freshness",
            "domain-filtering",
            "extract-inline",
        ),
        auto_allowed_by_default=False,
        # recommended stays with the starter preset (brave, serper, exa, linkup).
        recommended=False,
        supports_freshness=True,
        free_tier="No free tier; Kagi API credits required",
        signup_url="https://kagi.com/api",
        execute_search=execute_search,
    )
)
