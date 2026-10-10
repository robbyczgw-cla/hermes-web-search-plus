"""Pure compatibility projections between legacy WSP calls and v3 execution."""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, Mapping

from .contract_v3 import Capability, RequestV3
from .orchestrator_v3 import ExecutedV3


def legacy_request_to_v3(
    capability: Capability | str,
    payload: Mapping[str, Any],
    *,
    request_id: str | None = None,
    policy_mode: str = "classic",
) -> RequestV3:
    """Project a public legacy invocation into a complete RequestV3."""
    capability = Capability(capability)
    provider = str(payload.get("provider") or "auto")
    default_fallback = capability is Capability.EXTRACT or provider == "auto"
    routing = {
        "mode": "auto" if provider == "auto" else "fixed",
        "provider": provider,
        "allow_fallback": bool(payload.get("allow_fallback", default_fallback)),
        "policy_mode": policy_mode if policy_mode == "shadow" else "classic",
    }
    cache = {
        "mode": "bypass" if payload.get("no_cache") else "prefer",
        "ttl_seconds": int(payload.get("cache_ttl", 3600)),
    }
    client = {"accept_contract_versions": ["3.0", "2.x"]}
    budget: Dict[str, Any] = {}
    wall_time = payload.get("max_wall_time_ms")
    if isinstance(wall_time, int) and not isinstance(wall_time, bool) and wall_time > 0:
        budget["max_wall_time_ms"] = wall_time

    if capability is Capability.SEARCH:
        query = unicodedata.normalize("NFC", str(payload.get("query") or "")).strip()
        mode = str(payload.get("mode", "normal"))
        if mode == "research" and cache["mode"] == "prefer":
            # The standalone/public Research path has never used the legacy
            # cache, whose source-only projection cannot preserve its envelope.
            cache["mode"] = "bypass"
        options: Dict[str, Any] = {
            "max_results": int(payload.get("count", payload.get("max_results", 5))),
            "depth": str(payload.get("depth", payload.get("exa_depth", "normal"))),
            "mode": mode,
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
            budget=budget,
            client=client,
        )

    urls = payload.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    extract_options: Dict[str, Any] = {
        "output_format": str(
            payload.get("format", payload.get("output_format", "markdown"))
        ),
        "include_images": bool(payload.get("include_images", False)),
        "include_raw_html": bool(payload.get("include_raw_html", False)),
        "render_js": bool(payload.get("render_js", False)),
    }
    if payload.get("spans") is True:
        extract_options["spans"] = True
    spans_query = payload.get("spans_query")
    if spans_query is None and payload.get("query") is not None:
        spans_query = payload.get("query")
    if spans_query is not None:
        extract_options["spans_query"] = unicodedata.normalize(
            "NFC", str(spans_query)
        ).strip()
    return RequestV3(
        capability=capability,
        input={"urls": list(urls)},
        request_id=request_id,
        options=extract_options,
        cache=cache,
        routing=routing,
        budget=budget,
        client=client,
    )


def _project(execution: ExecutedV3, capability: Capability) -> Dict[str, Any]:
    if execution.response.capability is not capability:
        raise ValueError("legacy projection capability mismatch")
    return {
        key: value
        for key, value in execution.legacy_copy().items()
        if not str(key).startswith("_v3_")
    }


def v3_response_to_legacy_search(execution: ExecutedV3) -> Dict[str, Any]:
    """Return a fresh byte-equivalent copy of the legacy search payload."""
    return _project(execution, Capability.SEARCH)


def v3_response_to_legacy_extract(execution: ExecutedV3) -> Dict[str, Any]:
    """Project the bounded v3 extraction response onto the legacy payload shape."""
    legacy = _project(execution, Capability.EXTRACT)
    originals = {
        str(item.get("url") or ""): dict(item)
        for item in legacy.get("results") or []
        if isinstance(item, dict)
    }
    stored_by_observation = {
        str(stored.get("observation_id")): stored
        for stored in execution.response.stored_content
        if isinstance(stored, dict)
    }
    projected_by_url: Dict[str, Dict[str, Any]] = {}
    for item in execution.response.results:
        observed_url = str((item.get("url") or {}).get("observed") or "")
        title = item.get("title") or {}
        text = item.get("text") or {}
        result = originals.get(observed_url, {}).copy()
        result["title"] = title.get("text")
        result["url"] = observed_url
        result["content"] = text.get("text")
        stored = stored_by_observation.get(
            str(item.get("representative_observation_id"))
        )
        if stored is not None:
            # The inline text was cut to the context budget. Carry the real
            # length and the full-text reference so output can point at it.
            reference = stored.get("reference")
            succeeded = stored.get("storage_succeeded") is True
            result["full_text"] = {
                "truncated": True,
                "original_chars": stored.get("full_text_chars") if succeeded else None,
                "store_key": reference.get("key") if succeeded and isinstance(reference, dict) else None,
                "sha256": stored.get("full_text_sha256") if succeeded else None,
            }
        if "spans" in item:
            result["span_contract_version"] = item.get("span_contract_version")
            result["spans"] = [dict(span) for span in item["spans"]]
        if "raw_content" in result:
            result["raw_content"] = result["content"]
        projected_by_url.setdefault(observed_url, result)
    # Keep the payload's order; error items without an observation (e.g. a
    # URL rejected before any provider call) stay in their requested slot.
    projected = []
    emitted = set()
    for url, item in originals.items():
        if url in projected_by_url:
            projected.append(projected_by_url[url])
        elif item.get("error"):
            projected.append(item)
        else:
            continue
        emitted.add(url)
    projected.extend(
        result for url, result in projected_by_url.items() if url not in emitted
    )
    legacy["results"] = projected
    if execution.response.cache_status.get("disposition") in {
        "fresh_hit",
        "stale_hit",
    }:
        legacy["cached"] = True
    return legacy
