"""Local DonSeTch MCP provider for WSP search and extraction.

DonSeTch is launched as an isolated stdio MCP subprocess for each provider
request.  The adapter projects its structured ``web_search`` and ``web_fetch``
responses into WSP's source-only envelopes.  No shell is used and the binary
path is supplied through ``DONSETCH_BIN`` or the ``donsetch.binary`` config
field.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit

from wsp_sdk import ProviderSpec, extract_result, search_result, source_result

_ALLOWED_OUTPUT_FORMATS = {"markdown"}
_ALLOWED_SEARCH_TYPES = {"search", "news"}
_ALLOWED_INTENTS = {"auto", "web", "code", "paper", "news", "entity"}


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


def _clean_string_list(value: Any, *, limit: int = 50) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value[:limit] if isinstance(item, str) and item]


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in value
            if isinstance(item, (str, dict))
        )
    return ""


def _domain_matches(hostname: str, domain: str) -> bool:
    normalized = str(domain).strip().lower().rstrip(".")
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


def _resolve_binary(key: str | None, config: dict[str, Any]) -> str:
    section = config.get("donsetch", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    candidate = key or section.get("binary") or section.get("api_key")
    if not isinstance(candidate, str) or not candidate.strip():
        candidate = shutil.which("donsetch")
    else:
        candidate = candidate.strip()
        if not os.path.isabs(candidate):
            candidate = shutil.which(candidate) or ""
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise RuntimeError("donsetch_binary_not_configured")
    return candidate


def _mcp_response_from_stdout(stdout: str, request_id: int) -> dict[str, Any]:
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if message.get("error"):
            raise RuntimeError("donsetch_mcp_call_failed")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("donsetch_mcp_contract_failed")
        if result.get("isError") or result.get("is_error"):
            raise RuntimeError("donsetch_tool_error")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            structured = result.get("structured_content")
        if not isinstance(structured, dict):
            structured = {}
        return {
            "structured": structured,
            "text": _text_content(result.get("content")),
        }
    raise RuntimeError("donsetch_mcp_contract_failed")


def _read_mcp_message(process: subprocess.Popen[str], request_id: int, deadline: float) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("donsetch_process_failed")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            raise TimeoutError
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("donsetch_mcp_failed")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message


def _mcp_result(message: dict[str, Any], request_id: int) -> dict[str, Any]:
    if message.get("error"):
        code = "donsetch_mcp_initialize_failed" if request_id == 1 else "donsetch_mcp_call_failed"
        raise RuntimeError(code)
    result = message.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("donsetch_mcp_contract_failed")
    return result


def _call_donsetch_tool(
    binary: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "web-search-plus", "version": "4.0.0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    process = None
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            [binary, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),
        )
        if process.stdin is None:
            raise RuntimeError("donsetch_process_failed")
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        _mcp_result(_read_mcp_message(process, 1, deadline), 1)
        process.stdin.write(json.dumps(initialized, separators=(",", ":")) + "\n")
        process.stdin.write(json.dumps(call, separators=(",", ":")) + "\n")
        process.stdin.flush()
        result = _mcp_result(_read_mcp_message(process, 2, deadline), 2)
        if result.get("isError") or result.get("is_error"):
            raise RuntimeError("donsetch_tool_error")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            structured = result.get("structured_content")
        if not isinstance(structured, dict):
            structured = {}
        return {
            "structured": structured,
            "text": _text_content(result.get("content")),
        }
    except FileNotFoundError:
        raise RuntimeError("donsetch_binary_not_configured") from None
    except TimeoutError:
        raise RuntimeError("donsetch_timeout") from None
    except (BrokenPipeError, OSError):
        raise RuntimeError("donsetch_process_failed") from None
    finally:
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def _search_intent(args: Any) -> str:
    search_type = getattr(args, "search_type", "search") or "search"
    if search_type not in _ALLOWED_SEARCH_TYPES:
        raise RuntimeError("donsetch_search_type_unsupported")
    if search_type == "news":
        return "news"
    category = getattr(args, "category", None)
    return str(category) if category in _ALLOWED_INTENTS else "auto"


def execute_search(search_module, prov, args, key, config, routing_info):
    binary = _resolve_binary(key, config)
    section = config.get("donsetch", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    timeout_seconds = _bounded_int(section.get("timeout"), 180, 5, 600)
    freshness = getattr(args, "time_range", None) or getattr(args, "freshness", None)
    if freshness:
        raise RuntimeError("donsetch_freshness_unsupported")
    if bool(getattr(args, "images", False)):
        raise RuntimeError("donsetch_image_search_unsupported")

    include_domains = _clean_string_list(getattr(args, "include_domains", None))
    exclude_domains = _clean_string_list(getattr(args, "exclude_domains", None))
    max_results = _bounded_int(getattr(args, "max_results", 7), 7, 1, 12)
    payload = _call_donsetch_tool(
        binary,
        "web_search",
        {
            "query": str(getattr(args, "query", "")),
            "max_results": max_results,
            "intent": _search_intent(args),
        },
        timeout_seconds,
    )
    structured = payload["structured"]
    upstream_results = structured.get("results")
    if not isinstance(upstream_results, list):
        raise RuntimeError("donsetch_search_contract_failed")

    projected = []
    for position, item in enumerate(upstream_results, start=1):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        if not _url_allowed_by_domains(url, include_domains, exclude_domains):
            continue
        engines = _clean_string_list(item.get("engines"))
        projected.append(
            source_result(
                url,
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
                score=_safe_float(item.get("score")),
                position=position,
                source="donsetch",
                engines=engines,
                engines_consensus=str(item.get("consensus") or ""),
                source_type="web",
            )
        )

    engines = structured.get("engines")
    engines_used = []
    engines_blocked = []
    if isinstance(engines, list):
        for item in engines:
            if not isinstance(item, dict):
                continue
            name = item.get("engine")
            if not isinstance(name, str) or not name:
                continue
            if str(item.get("status", "")).lower() == "ok":
                engines_used.append(name)
            else:
                engines_blocked.append(name)
    metadata = {
        "engines_used": engines_used,
        "engine_blocked": engines_blocked,
        "intent": str(structured.get("intent") or ""),
        "cached": bool(structured.get("cached")),
        "weak": bool(structured.get("weak")),
        "duration_ms": _safe_float(structured.get("elapsed_ms")),
        "provider": "donsetch",
    }
    return search_result(
        prov,
        str(getattr(args, "query", "")),
        projected,
        metadata=metadata,
    )


def _project_fetch_item(payload: dict[str, Any], fallback_url: str) -> dict[str, Any]:
    structured = payload.get("structured")
    if not isinstance(structured, dict):
        return {"url": fallback_url, "error": "donsetch_fetch_contract_failed", "status": 0}
    observed_url = structured.get("url")
    url = observed_url if isinstance(observed_url, str) and observed_url else fallback_url
    status = _safe_int(structured.get("status"))
    content = str(payload.get("text") or "")
    if (
        not structured.get("content_ok")
        or structured.get("verdict") in {"Error", "Blocked", "Failed"}
        or status >= 400
        or not content.strip()
    ):
        return {"url": url, "error": "donsetch_fetch_failed", "status": status}
    return source_result(
        url,
        title=str(structured.get("title") or ""),
        content=content,
        images=[],
        status=status,
        fetcher="donsetch",
        page_type=str(structured.get("content_kind") or ""),
        source_type="web",
        quality=_safe_float(structured.get("quality")),
        lang=str(structured.get("lang") or ""),
        site=str(structured.get("site") or ""),
        pdf=structured.get("pdf") if isinstance(structured.get("pdf"), dict) else None,
        next_offset=_safe_int(structured.get("next_offset")) if structured.get("next_offset") is not None else None,
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
    if str(output_format or "markdown") not in _ALLOWED_OUTPUT_FORMATS:
        raise RuntimeError("donsetch_output_format_unsupported")
    binary = _resolve_binary(key, config)
    section = config.get("donsetch", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    timeout_seconds = _bounded_int(section.get("timeout"), 180, 5, 600)
    max_chars = _bounded_int(
        section.get("max_content_chars", config.get("web", {}).get("extract_char_limit", 15000)
        if isinstance(config.get("web", {}), dict)
        else 15000),
        15000,
        500,
        200000,
    )
    tier = "2" if render_js else str(section.get("tier") or "auto")
    if tier not in {"auto", "1", "2"}:
        tier = "auto"

    projected = []
    for requested_url in [str(url) for url in urls]:
        payload = _call_donsetch_tool(
            binary,
            "web_fetch",
            {
                "url": requested_url,
                "max_chars": max_chars,
                "media": bool(include_images),
                "tier": tier,
            },
            timeout_seconds,
        )
        result = _project_fetch_item(payload, requested_url)
        if include_raw_html and not result.get("error"):
            # DonSeTch deliberately returns Markdown; do not label it HTML.
            result["raw_error"] = "donsetch_raw_html_unsupported"
        projected.append(result)
    return extract_result(prov, projected)


PROVIDER = ProviderSpec(
    id="donsetch",
    kind="both",
    env_var="DONSETCH_BIN",
    display_name="DonSeTch (local MCP)",
    description=(
        "Local DonSeTch 2.x stdio MCP provider for metasearch, direct extraction, "
        "PDF/OCR-aware fetching, and optional browser escalation."
    ),
    config_section="donsetch",
    capability_labels=("search", "extract", "local", "pdf", "ocr", "browser"),
    upstream_capabilities=("web_search", "web_fetch", "web_crawl"),
    auto_allowed_by_default=False,
    recommended=False,
    keyless=False,
    supports_freshness=False,
    free_tier="Free local binary; no API key",
    signup_url="https://github.com/dondai44423/donsetch",
    execute_search=execute_search,
    execute_extract=execute_extract,
)
