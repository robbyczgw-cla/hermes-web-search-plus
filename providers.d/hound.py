"""Local-only Hound MCP bridge for WSP search and extraction.

Hound remains a separately managed sidecar.  This provider talks only to a
loopback Streamable HTTP MCP endpoint and projects Hound's source material into
WSP's stable source-only envelopes.  It is explicit-only by default.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable, Coroutine
from urllib.parse import urlsplit

from wsp_sdk import ProviderSpec, extract_result, search_result, source_result

_ALLOWED_HOSTS = {"127.0.0.1", "::1"}
_ALLOWED_FRESHNESS = {"day", "week", "month", "year"}
_ALLOWED_OUTPUT_FORMATS = {"markdown", "html", "text"}


def _validate_endpoint(value: str) -> str:
    """Accept only an uncredentialed loopback HTTP MCP endpoint."""

    endpoint = str(value).strip()
    if "?" in endpoint or "#" in endpoint:
        raise ValueError("hound_endpoint_invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("hound_endpoint_invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_HOSTS
        or port is None
        or parsed.path != "/mcp"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("hound_endpoint_invalid")
    return endpoint


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _domain_matches(hostname: str, domain: str) -> bool:
    normalized = domain.strip().lower().rstrip(".")
    if normalized.startswith("*."):
        normalized = normalized[2:]
    return bool(normalized) and (
        hostname == normalized or hostname.endswith(f".{normalized}")
    )


def _url_allowed_by_domains(
    url: str,
    include_domains: list[str],
    exclude_domains: list[str],
) -> bool:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname or parsed.scheme not in {"http", "https"}:
        return False
    if any(_domain_matches(hostname, domain) for domain in exclude_domains):
        return False
    return not include_domains or any(
        _domain_matches(hostname, domain) for domain in include_domains
    )


def _run_async(factory: Callable[[], Coroutine[Any, Any, dict[str, Any]]]) -> dict[str, Any]:
    """Run one MCP request from WSP's synchronous provider boundary."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    # Hermes normally invokes sync tools in a worker thread.  Keep the adapter
    # correct for direct callers that already own an event loop as well.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="wsp-hound") as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


async def _call_hound_tool_async(
    endpoint: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx.Timeout(float(timeout_seconds), read=float(timeout_seconds))
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as http_client:
        async with streamable_http_client(
            endpoint,
            http_client=http_client,
            terminate_on_close=True,
        ) as streams:
            read_stream, write_stream, _session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.call_tool(
                    tool,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=timeout_seconds),
                )

    if getattr(response, "isError", False):
        raise RuntimeError("hound_mcp_call_failed")
    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(response, "content", ()):
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("hound_mcp_contract_failed")


def _call_hound_tool(
    endpoint: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    endpoint = _validate_endpoint(endpoint)
    try:
        return _run_async(
            lambda: _call_hound_tool_async(endpoint, tool, arguments, timeout_seconds)
        )
    except Exception:
        raise RuntimeError("hound_mcp_unavailable") from None


def _clean_string_list(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value[:limit] if isinstance(item, str) and item]


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if isinstance(item, str))
    return ""


def execute_search(search_module, prov, args, key, config, routing_info):
    endpoint = _validate_endpoint(key or "")
    section = config.get("hound", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    timeout_seconds = _bounded_int(section.get("timeout"), 120, 5, 180)
    search_type = getattr(args, "search_type", "search") or "search"
    if search_type != "search":
        raise RuntimeError("hound_search_type_unsupported")

    options: dict[str, Any] = {
        "max_results": _bounded_int(getattr(args, "max_results", 6), 6, 1, 50),
        # WSP owns provider-level freshness and cache identity.  A hidden Hound
        # cache would make WSP refresh/bypass controls dishonest.
        "cache_ttl": 0,
    }
    freshness = getattr(args, "time_range", None) or getattr(args, "freshness", None)
    if freshness in _ALLOWED_FRESHNESS:
        options["freshness"] = freshness
    include_domains = _clean_string_list(getattr(args, "include_domains", None))
    if len(include_domains) == 1:
        options["site"] = include_domains[0]
    exclude_domains = _clean_string_list(getattr(args, "exclude_domains", None))
    if exclude_domains:
        options["exclude_sites"] = exclude_domains
    language = getattr(args, "language", None)
    country = getattr(args, "country", None)
    if isinstance(language, str) and language:
        options["language"] = language
    if isinstance(country, str) and country:
        options["region"] = (
            f"{country}-{language}"
            if isinstance(language, str) and language
            else country
        )

    payload = _call_hound_tool(
        endpoint,
        "mcp_smart_search",
        {"query": str(getattr(args, "query", "")), "options": options},
        timeout_seconds,
    )
    upstream_results = payload.get("results")
    if not isinstance(upstream_results, list):
        raise RuntimeError("hound_search_contract_failed")
    if payload.get("error") and not upstream_results:
        raise RuntimeError("hound_search_failed")

    projected = []
    for item in upstream_results:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("url"), str)
            or not item["url"]
            or not _url_allowed_by_domains(
                item["url"], include_domains, exclude_domains
            )
        ):
            continue
        projected.append(
            source_result(
                item["url"],
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
                score=_safe_float(item.get("relevance_score")),
                position=_safe_int(item.get("position")),
                source=str(item.get("source") or ""),
                fetch_relevance=str(item.get("fetch_relevance") or ""),
                engines_consensus=str(item.get("engines_consensus") or ""),
            )
        )

    metadata = {
        "engines_used": _clean_string_list(payload.get("engines_used")),
        "engine_blocked": _clean_string_list(payload.get("engine_blocked")),
        "rerank_mode": str(payload.get("rerank_mode") or ""),
        "duration_ms": _safe_float(payload.get("duration_ms")),
    }
    return search_result(
        prov,
        str(getattr(args, "query", "")),
        projected,
        metadata=metadata,
    )


def _fetch_arguments(
    urls: list[str],
    output_format: str,
    include_images: bool,
    render_js: bool,
    section: dict[str, Any],
) -> dict[str, Any]:
    extraction_type = output_format if output_format in _ALLOWED_OUTPUT_FORMATS else "markdown"
    arguments: dict[str, Any] = {
        "urls": list(urls),
        "extraction_type": extraction_type,
        "cache_ttl": 0,
        "max_content_chars": _bounded_int(
            section.get("max_content_chars"), 40000, 500, 200000
        ),
        "options": {"include_media": bool(include_images)},
    }
    if render_js:
        arguments["force_fetcher"] = "stealthy"
    return arguments


def _bulk_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("results")
    if not isinstance(items, list):
        raise RuntimeError("hound_extract_contract_failed")
    return [item if isinstance(item, dict) else {} for item in items]


def _single_bulk_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Fail closed unless a one-URL Hound request returns exactly one item."""

    items = _bulk_items(payload)
    return items[0] if len(items) == 1 else {}


def _project_fetch_item(item: dict[str, Any], fallback_url: str) -> dict[str, Any]:
    observed_url = item.get("url")
    url = observed_url if isinstance(observed_url, str) else fallback_url
    status = _safe_int(item.get("status"))
    content = _text_content(item.get("content"))
    if item.get("error") or not item.get("content_ok") or status >= 400 or not content.strip():
        return {"url": url, "error": "hound_fetch_failed", "status": status}

    metadata_value = item.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    return source_result(
        url,
        title=str(metadata.get("title") or ""),
        content=content,
        images=_clean_string_list(item.get("media")),
        status=status,
        fetcher=str(item.get("fetcher_used") or ""),
        page_type=str(item.get("page_type") or ""),
        source_type=str(item.get("source_type") or ""),
        is_official=bool(item.get("is_official")),
        fetched_at=str(item.get("fetched_at") or ""),
        duration_ms=_safe_float(item.get("duration_ms")),
    )


def execute_extract(
    extract_module,
    prov,
    urls,
    key,
    output_format,
    include_images,
    include_raw_html,
    render_js,
    config,
    keyless_allowed,
):
    endpoint = _validate_endpoint(key or "")
    section = config.get("hound", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    timeout_seconds = _bounded_int(section.get("timeout"), 120, 5, 180)
    requested_urls = [str(url) for url in urls]

    primary_arguments = _fetch_arguments(
        [], str(output_format), bool(include_images), bool(render_js), section
    )
    # Hound exposes the final URL only, not both a requested and final URL.  A
    # request per URL therefore keeps redirects usable without relying on bulk
    # response ordering or an upstream URL to associate content with a caller.
    primary_items = []
    for requested_url in requested_urls:
        arguments = dict(primary_arguments)
        arguments["urls"] = [requested_url]
        payload = _call_hound_tool(
            endpoint, "mcp_smart_fetch", arguments, timeout_seconds
        )
        primary_items.append(_single_bulk_item(payload))
    projected = [
        _project_fetch_item(item, requested_url)
        for item, requested_url in zip(primary_items, requested_urls)
    ]

    if include_raw_html:
        if primary_arguments["extraction_type"] == "html":
            for result in projected:
                if not result.get("error"):
                    result["raw_content"] = result.get("content", "")
        else:
            raw_arguments = _fetch_arguments([], "html", False, bool(render_js), section)
            for requested_url, result in zip(requested_urls, projected):
                if result.get("error"):
                    continue
                arguments = dict(raw_arguments)
                arguments["urls"] = [requested_url]
                try:
                    raw_payload = _call_hound_tool(
                        endpoint, "mcp_smart_fetch", arguments, timeout_seconds
                    )
                    raw_item = _single_bulk_item(raw_payload)
                except Exception:
                    result["raw_error"] = "hound_raw_html_failed"
                    continue
                raw_status = _safe_int(raw_item.get("status"))
                raw_content = _text_content(raw_item.get("content"))
                if (
                    raw_item.get("error")
                    or not raw_item.get("content_ok")
                    or raw_status >= 400
                    or not raw_content.strip()
                ):
                    result["raw_error"] = "hound_raw_html_failed"
                    continue
                result["raw_content"] = raw_content

    return extract_result(prov, projected)


PROVIDER = ProviderSpec(
    id="hound",
    kind="both",
    env_var="HOUND_MCP_URL",
    display_name="Hound (local MCP)",
    description=(
        "Local key-free metasearch and browser-backed extraction through a loopback "
        "Hound sidecar. Hound is an independent MIT-licensed project by Bishesh Bhandari."
    ),
    config_section="hound",
    capability_labels=("search", "extract", "local", "browser", "pdf", "ocr"),
    upstream_capabilities=("crawl", "screenshot", "browser", "pdf", "ocr"),
    auto_allowed_by_default=False,
    recommended=False,
    keyless=False,
    supports_freshness=True,
    free_tier="Free local sidecar; no API key",
    signup_url="https://github.com/dondai1234/master-fetch",
    execute_search=execute_search,
    execute_extract=execute_extract,
)
