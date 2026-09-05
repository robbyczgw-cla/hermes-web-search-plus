"""Local DonSeTch MCP provider for WSP search and extraction.

DonSeTch is launched as an isolated stdio MCP subprocess. Search uses one
process per request. A single extract request reuses one initialized session
for every URL, then shuts the child down. The adapter projects structured
``web_search`` and ``web_fetch`` responses into WSP's source-only envelopes.
No shell is used. The binary path comes from ``DONSETCH_BIN`` or the
``donsetch.binary`` config field — it is a filesystem path, not an API key.

DonSeTch 3.6+ keeps model evidence in the text block and only actionable state
on ``structuredContent``. Transport diagnostics live under namespaced
``_meta`` keys (``com.donsetch/search-debug``, ``com.donsetch/fetch-debug``).
This adapter whitelists those diagnostic fields and still accepts the older
pre-compact structured shape so 3.2.x binaries keep working.
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
TESTED_VERSION = "3.6.1"
STDERR_LIMIT = 2048
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b\s*[:=]\s*\S+"
)
_HOME_RE = re.compile(r"(?i)(/root|/home/[^/\s]+|/Users/[^/\s]+)")
_SEARCH_HEADER_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_SEARCH_HINT_RE = re.compile(r"\s+(·\s+⚠.*)$")
_SEARCH_FOOTER_PREFIXES = (
    "Weak results",
    "Degraded retrieval",
    "No results.",
    "*degraded:",
    "*fetch results",
)
_SEARCH_DEBUG_KEY = "com.donsetch/search-debug"
_FETCH_DEBUG_KEY = "com.donsetch/fetch-debug"

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


def _hostname_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""


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


def _child_env() -> dict[str, str]:
    """Copy the process env but pin DonSeTch to stdio for this child only."""
    env = os.environ.copy()
    env["DONSETCH_TRANSPORT"] = "stdio"
    return env


def _namespaced_meta(meta: Any, key: str) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    value = meta.get(key)
    return value if isinstance(value, dict) else {}


def _payload_from_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Decode one MCP tools/call result into structured/text/meta."""
    if result.get("isError") or result.get("is_error"):
        raise RuntimeError("donsetch_tool_error")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        structured = result.get("structured_content")
    if not isinstance(structured, dict):
        structured = {}
    meta = result.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    return {
        "structured": structured,
        "text": _text_content(result.get("content")),
        "meta": meta,
    }


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
        return _payload_from_tool_result(result)
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


def _split_search_title_host(after_dot: str, expected_host: str | None) -> tuple[str, str]:
    """Split ``title : host`` after the reference marker, preserving colon titles."""
    text = after_dot
    hint_match = _SEARCH_HINT_RE.search(text)
    if hint_match:
        text = text[: hint_match.start()]
    if expected_host:
        suffix = f" : {expected_host}"
        if text.endswith(suffix):
            return text[: -len(suffix)], expected_host
        # Tolerate trailing path fragments or case differences on the host token.
        lowered = text.lower()
        host_l = expected_host.lower()
        marker = f" : {host_l}"
        idx = lowered.rfind(marker)
        if idx >= 0 and idx + len(marker) == len(lowered):
            return text[:idx], expected_host
    if " : " not in text:
        return text, expected_host or ""
    title, host = text.rsplit(" : ", 1)
    host = host.strip().split()[0] if host.strip() else ""
    return title, host


def parse_search_evidence(text: str, structured_results: list[dict[str, Any]] | None = None) -> dict[int, dict[str, str]]:
    """Parse compact DonSeTch search markdown into rank-keyed title/snippet maps.

    Rank binding happens before any domain filter. Malformed or mismatched
    evidence lines are ignored rather than assigned to a different URL.
    """
    hosts_by_rank: dict[int, str] = {}
    handles_by_rank: dict[int, str] = {}
    urls_by_rank: dict[int, str] = {}
    if isinstance(structured_results, list):
        for index, item in enumerate(structured_results):
            if not isinstance(item, dict):
                continue
            rank = _safe_int(item.get("rank"), index + 1)
            if rank <= 0:
                rank = index + 1
            url = item.get("url") if isinstance(item.get("url"), str) else ""
            handle = item.get("handle") if isinstance(item.get("handle"), str) else ""
            urls_by_rank[rank] = url
            if handle:
                handles_by_rank[rank] = handle
            host = _hostname_of(url)
            if host:
                hosts_by_rank[rank] = host

    evidence: dict[int, dict[str, str]] = {}
    current_rank: int | None = None
    snippet_parts: list[str] = []

    def _flush() -> None:
        nonlocal current_rank, snippet_parts
        if current_rank is None:
            return
        bucket = evidence.setdefault(current_rank, {"title": "", "snippet": "", "reference": ""})
        if snippet_parts:
            bucket["snippet"] = "\n".join(snippet_parts).strip()
        snippet_parts = []

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if not line.startswith("   ") and any(stripped.startswith(prefix) for prefix in _SEARCH_FOOTER_PREFIXES):
            _flush()
            current_rank = None
            continue
        header = _SEARCH_HEADER_RE.match(line)
        if header:
            _flush()
            rank = int(header.group(1))
            body = header.group(2).strip()
            # When structured results are present, only bind ranks they declare.
            if urls_by_rank and rank not in urls_by_rank:
                current_rank = None
                continue
            expected_host = hosts_by_rank.get(rank)
            expected_handle = handles_by_rank.get(rank)
            expected_url = urls_by_rank.get(rank)
            reference = ""
            title = ""
            if " · " in body:
                reference, after = body.split(" · ", 1)
                reference = reference.strip()
                title, _host = _split_search_title_host(after, expected_host)
            else:
                reference = body
            # Reject rank/reference mismatches so evidence never drifts onto another URL.
            if expected_handle or expected_url:
                allowed = {value for value in (expected_handle, expected_url) if value}
                ok = reference in allowed
                if not ok and expected_url:
                    ok = reference.rstrip("/") == expected_url.rstrip("/")
                if not ok:
                    current_rank = None
                    continue
            evidence[rank] = {
                "title": title.strip(),
                "snippet": "",
                "reference": reference,
            }
            current_rank = rank
            snippet_parts = []
            continue
        if current_rank is None:
            continue
        if line.startswith("   "):
            snippet_parts.append(line[3:].rstrip())
            continue
        # Non-indented non-header body: ignore rather than attaching to the wrong hit.
    _flush()
    return evidence


def _whitelist_search_debug(meta: dict[str, Any]) -> dict[str, Any]:
    debug = _namespaced_meta(meta, _SEARCH_DEBUG_KEY)
    if not debug:
        return {}
    out: dict[str, Any] = {}
    if "intent" in debug:
        out["intent"] = debug.get("intent")
    if "cached" in debug:
        out["cached"] = bool(debug.get("cached"))
    if "elapsed_ms" in debug:
        out["elapsed_ms"] = _safe_float(debug.get("elapsed_ms"))
    if "weak" in debug:
        out["weak"] = bool(debug.get("weak"))
    engines = debug.get("engines")
    if isinstance(engines, list):
        out["engines"] = engines
    results = debug.get("results")
    if isinstance(results, list):
        cleaned = []
        for item in results:
            if not isinstance(item, dict):
                cleaned.append({})
                continue
            entry: dict[str, Any] = {}
            if "score" in item:
                entry["score"] = item.get("score")
            if "consensus" in item:
                entry["consensus"] = item.get("consensus")
            if "engines" in item:
                entry["engines"] = item.get("engines")
            cleaned.append(entry)
        out["results"] = cleaned
    return out


def _whitelist_fetch_debug(meta: dict[str, Any]) -> dict[str, Any]:
    debug = _namespaced_meta(meta, _FETCH_DEBUG_KEY)
    if not debug:
        return {}
    out: dict[str, Any] = {}
    for key in ("status", "title", "quality", "site", "verdict", "tier"):
        if key in debug:
            out[key] = debug.get(key)
    return out


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
            env=_child_env(),
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

    # Bind text evidence and debug diagnostics by original rank/index before
    # any domain filtering so dropped hits cannot steal another row's fields.
    evidence = parse_search_evidence(payload.get("text") or "", upstream_results)
    debug = _whitelist_search_debug(payload.get("meta") or {})
    debug_results = debug.get("results") if isinstance(debug.get("results"), list) else []

    projected = []
    for index, item in enumerate(upstream_results):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        rank = _safe_int(item.get("rank"), index + 1)
        if rank <= 0:
            rank = index + 1
        bound = evidence.get(rank) or {}
        debug_item = debug_results[index] if index < len(debug_results) and isinstance(debug_results[index], dict) else {}

        # Compact contract: title/snippet live in text. Legacy: still on item.
        title = str(bound.get("title") or item.get("title") or "")
        snippet = str(bound.get("snippet") or item.get("snippet") or "")
        score_raw = item.get("score")
        if score_raw is None:
            score_raw = debug_item.get("score")
        consensus_raw = item.get("consensus")
        if consensus_raw is None:
            consensus_raw = debug_item.get("consensus")
        engines = item.get("engines")
        if not isinstance(engines, (list, tuple)):
            engines = debug_item.get("engines")
        engines = _clean_string_list(engines)

        if not _url_allowed_by_domains(url, include_domains, exclude_domains):
            continue
        projected.append(
            source_result(
                url,
                title=title,
                snippet=snippet,
                score=_safe_float(score_raw),
                position=rank,
                source="donsetch",
                engines=engines,
                engines_consensus=str(consensus_raw or ""),
                source_type="web",
            )
        )

    engines = structured.get("engines")
    if not isinstance(engines, list):
        engines = debug.get("engines") if isinstance(debug.get("engines"), list) else []
    engines_used = []
    engines_blocked = []
    if isinstance(engines, list):
        for engine_item in engines:
            if not isinstance(engine_item, dict):
                continue
            name = engine_item.get("engine")
            if not isinstance(name, str) or not name:
                continue
            if str(engine_item.get("status", "")).lower() == "ok":
                engines_used.append(name)
            else:
                engines_blocked.append(name)

    intent = structured.get("intent")
    if intent in (None, ""):
        intent = debug.get("intent")
    cached = structured.get("cached")
    if cached is None:
        cached = debug.get("cached", False)
    weak = structured.get("weak")
    if weak is None:
        weak = debug.get("weak", False)
    elapsed = structured.get("elapsed_ms")
    if elapsed is None:
        elapsed = debug.get("elapsed_ms", 0)

    metadata = {
        "engines_used": engines_used,
        "engine_blocked": engines_blocked,
        "intent": str(intent or ""),
        "cached": bool(cached),
        "weak": bool(weak),
        "duration_ms": _safe_float(elapsed),
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
    debug = _whitelist_fetch_debug(payload.get("meta") or {})
    observed_url = structured.get("url")
    url = observed_url if isinstance(observed_url, str) and observed_url else fallback_url

    # Compact contract moved title/status/quality/site/verdict into fetch-debug.
    # Legacy binaries still place them on structuredContent — prefer structured.
    def _field(name: str, default: Any = None) -> Any:
        if name in structured and structured.get(name) is not None:
            return structured.get(name)
        if name in debug and debug.get(name) is not None:
            return debug.get(name)
        return default

    status = _safe_int(_field("status", 0))
    verdict = str(_field("verdict") or "")
    title = str(_field("title") or "")
    content = str(payload.get("text") or "")
    content_ok = structured.get("content_ok")
    if content_ok is None:
        content_ok = verdict == "ContentOk" and status and status < 400
    if (
        not content_ok
        or verdict in {"Error", "Blocked", "Failed"}
        or status >= 400
        or not content.strip()
    ):
        return {"url": url, "error": "donsetch_fetch_failed", "status": status}
    return source_result(
        url,
        title=title,
        content=content,
        images=[],
        status=status,
        fetcher="donsetch",
        page_type=str(structured.get("content_kind") or ""),
        source_type="web",
        quality=_safe_float(_field("quality", 0.0)),
        lang=str(structured.get("lang") or ""),
        site=str(_field("site") or ""),
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
