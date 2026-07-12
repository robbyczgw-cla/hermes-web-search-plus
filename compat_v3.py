"""Pure compatibility projections between legacy WSP calls and v3 execution."""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, Mapping

from contract_v3 import Capability, RequestV3
from orchestrator_v3 import ExecutedV3


def legacy_request_to_v3(
    capability: Capability | str,
    payload: Mapping[str, Any],
    *,
    request_id: str | None = None,
) -> RequestV3:
    """Project a public legacy invocation into a complete RequestV3."""
    capability = Capability(capability)
    provider = str(payload.get("provider") or "auto")
    default_fallback = capability is Capability.EXTRACT or provider == "auto"
    routing = {
        "mode": "auto" if provider == "auto" else "fixed",
        "provider": provider,
        "allow_fallback": bool(payload.get("allow_fallback", default_fallback)),
        "policy_mode": "classic",
    }
    cache = {
        "mode": "bypass" if payload.get("no_cache") else "prefer",
        "ttl_seconds": int(payload.get("cache_ttl", 3600)),
    }
    client = {"accept_contract_versions": ["3.0", "2.x"]}

    if capability is Capability.SEARCH:
        query = unicodedata.normalize("NFC", str(payload.get("query") or "")).strip()
        options: Dict[str, Any] = {
            "max_results": int(payload.get("count", payload.get("max_results", 5))),
            "depth": str(payload.get("depth", payload.get("exa_depth", "normal"))),
            "mode": str(payload.get("mode", "normal")),
            "quality_report": bool(payload.get("quality_report", False)),
            "research_time_budget": float(payload.get("research_time_budget", 55.0)),
        }
        for key in (
            "freshness",
            "time_range",
            "search_type",
            "include_domains",
            "exclude_domains",
        ):
            value = payload.get(key)
            if value is not None:
                options[key] = list(value) if key.endswith("_domains") else value
        locale = {
            key: payload[key]
            for key in ("country", "language")
            if payload.get(key) not in (None, "auto")
        }
        if locale:
            options["locale"] = locale
        return RequestV3(
            capability=capability,
            input={"query": query},
            request_id=request_id,
            options=options,
            cache=cache,
            routing=routing,
            client=client,
        )

    urls = payload.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    return RequestV3(
        capability=capability,
        input={"urls": list(urls)},
        request_id=request_id,
        options={
            "output_format": str(
                payload.get("format", payload.get("output_format", "markdown"))
            ),
            "include_images": bool(payload.get("include_images", False)),
            "include_raw_html": bool(payload.get("include_raw_html", False)),
            "render_js": bool(payload.get("render_js", False)),
        },
        cache=cache,
        routing=routing,
        client=client,
    )


def _project(execution: ExecutedV3, capability: Capability) -> Dict[str, Any]:
    if execution.response.capability is not capability:
        raise ValueError("legacy projection capability mismatch")
    return execution.legacy_copy()


def v3_response_to_legacy_search(execution: ExecutedV3) -> Dict[str, Any]:
    """Return a fresh byte-equivalent copy of the legacy search payload."""
    return _project(execution, Capability.SEARCH)


def v3_response_to_legacy_extract(execution: ExecutedV3) -> Dict[str, Any]:
    """Return a fresh byte-equivalent copy of the legacy extract payload."""
    return _project(execution, Capability.EXTRACT)
