"""Local DonSeTch MCP provider for WSP search and extraction.

DonSeTch is launched as an isolated stdio MCP subprocess. Search uses one
process per request. A single extract request reuses one initialized session
for every URL, then shuts the child down. The adapter projects structured
``web_search`` and ``web_fetch`` responses into WSP's source-only envelopes.
No shell is used. The binary path comes from ``DONSETCH_BIN`` or the
``donsetch.binary`` config field — it is a filesystem path, not an API key.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from wsp_sdk import ProviderSpec, extract_result, search_result, source_result

_ALLOWED_OUTPUT_FORMATS = {"markdown"}
_ALLOWED_SEARCH_TYPES = {"search", "news"}
_ALLOWED_INTENTS = {"auto", "web", "code", "paper", "news", "entity"}
TESTED_VERSION = "3.2.1"
STDERR_LIMIT = 2048
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b\s*[:=]\s*\S+"
)
_HOME_RE = re.compile(r"(?i)(/root|/home/[^/\s]+|/Users/[^/\s]+)")

_last_stderr = ""
_stderr_lock = threading.Lock()


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


def _candidate_binary(key: str | None, config: dict[str, Any]) -> str:
    section = config.get("donsetch", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        section = {}
    candidate = key or section.get("binary") or section.get("api_key")
    if not isinstance(candidate, str) or not candidate.strip():
        return shutil.which("donsetch") or ""
    candidate = candidate.strip()
    if not os.path.isabs(candidate):
        return shutil.which(candidate) or ""
    return candidate


def _resolve_binary(key: str | None, config: dict[str, Any]) -> str:
    candidate = _candidate_binary(key, config)
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise RuntimeError("donsetch_binary_not_configured")
    return candidate


def sanitize_stderr(text: str, limit: int = STDERR_LIMIT) -> str:
    if not isinstance(text, str) or not text:
        return ""
    cleaned = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    cleaned = _HOME_RE.sub("[path]", cleaned)
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def _remember_stderr(text: str) -> None:
    global _last_stderr
    with _stderr_lock:
        _last_stderr = sanitize_stderr(text)


def _last_stderr_excerpt() -> str:
    with _stderr_lock:
        return _last_stderr


def _parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return f"{int(match.group(1))}.{int(match.group(2))}.{int(match.group(3))}"


def _compatibility(version: str | None) -> str:
    if not version:
        return "unknown"
    major, minor, patch = (int(part) for part in version.split("."))
    tested_major, tested_minor, tested_patch = (int(part) for part in TESTED_VERSION.split("."))
    if (major, minor, patch) == (tested_major, tested_minor, tested_patch):
        return "tested"
    if major == tested_major:
        return "compatible_unverified"
    return "incompatible_major"


def inspect_donsetch_readiness(
    *,
    binary: str | None = None,
    key: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe local DonSeTch binary readiness without treating the path as a key."""
    report = {
        "state": "missing",
        "version": None,
        "tested_version": TESTED_VERSION,
        "compatibility": "unknown",
        "binary_configured": False,
    }
    candidate = binary if isinstance(binary, str) else _candidate_binary(key, config or {})
    if not candidate:
        return report
    report["binary_configured"] = True
    if not os.path.isfile(candidate):
        return report
    if not os.access(candidate, os.X_OK):
        report["state"] = "not_executable"
        return report
    report["state"] = "executable"
    try:
        completed = subprocess.run(
            [candidate, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return report
    version = _parse_version((completed.stdout or "") + "\n" + (completed.stderr or ""))
    report["version"] = version
    report["compatibility"] = _compatibility(version)
    return report


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


def _payload_from_tool_result(result: dict[str, Any]) -> dict[str, Any]:
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


class DonsetchSession:
    """One initialized DonSeTch stdio MCP process for one WSP request."""

    def __init__(self, binary: str, timeout_seconds: int):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._deadline = 0.0
        self._stderr = ""
        self._stderr_thread: threading.Thread | None = None
        self._closed = False

    def __enter__(self) -> DonsetchSession:
        try:
            self._start()
            self._initialize()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _start(self) -> None:
        self._deadline = time.monotonic() + self.timeout_seconds
        self._process = subprocess.Popen(
            [self.binary, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),
        )
        self._stderr_thread = threading.Thread(target=self._collect_stderr, daemon=True)
        self._stderr_thread.start()
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("donsetch_process_failed")

    def _collect_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        chunks: list[str] = []
        try:
            while True:
                piece = process.stderr.read(256)
                if not piece:
                    break
                chunks.append(piece)
                joined = "".join(chunks)
                if len(joined) > STDERR_LIMIT:
                    chunks = [joined[:STDERR_LIMIT]]
                    # Keep draining so the child cannot block on a full pipe.
        except OSError:
            pass
        self._stderr = "".join(chunks)[:STDERR_LIMIT]
        _remember_stderr(self._stderr)

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("donsetch_process_failed")
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("donsetch_process_failed") from exc

    def _initialize(self) -> None:
        request_id = self._next_id
        self._next_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "web-search-plus", "version": TESTED_VERSION},
                },
            }
        )
        assert self._process is not None
        _mcp_result(_read_mcp_message(self._process, request_id, self._deadline), request_id)
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        assert self._process is not None
        result = _mcp_result(_read_mcp_message(self._process, request_id, self._deadline), request_id)
        return _payload_from_tool_result(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            _remember_stderr(self._stderr)
            return
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
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        if process.stderr is not None:
            try:
                process.stderr.close()
            except OSError:
                pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
        _remember_stderr(self._stderr)
        self._process = None


def _call_donsetch_tool(
    binary: str,
    tool: str,
    arguments: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    try:
        with DonsetchSession(binary, timeout_seconds) as session:
            return session.call(tool, arguments)
    except FileNotFoundError:
        raise RuntimeError("donsetch_binary_not_configured") from None
    except TimeoutError:
        raise RuntimeError("donsetch_timeout") from None
    except (BrokenPipeError, OSError):
        raise RuntimeError("donsetch_process_failed") from None


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
    try:
        with DonsetchSession(binary, timeout_seconds) as session:
            for requested_url in [str(url) for url in urls]:
                payload = session.call(
                    "web_fetch",
                    {
                        "url": requested_url,
                        "max_chars": max_chars,
                        "media": bool(include_images),
                        "tier": tier,
                    },
                )
                result = _project_fetch_item(payload, requested_url)
                if include_raw_html and not result.get("error"):
                    # DonSeTch deliberately returns Markdown; do not label it HTML.
                    result["raw_error"] = "donsetch_raw_html_unsupported"
                projected.append(result)
    except FileNotFoundError:
        raise RuntimeError("donsetch_binary_not_configured") from None
    except TimeoutError:
        raise RuntimeError("donsetch_timeout") from None
    except (BrokenPipeError, OSError):
        raise RuntimeError("donsetch_process_failed") from None
    return extract_result(prov, projected)


PROVIDER = ProviderSpec(
    id="donsetch",
    kind="both",
    env_var="DONSETCH_BIN",
    display_name="DonSeTch (local MCP)",
    description=(
        "Local DonSeTch 3.x stdio MCP provider for metasearch, direct extraction, "
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
