"""Explicit-only Parallel Search MCP provider, using bounded Streamable HTTP.

Only source URLs and excerpts are projected. Anonymous access uses the SDK's
public opt-in; an optional dedicated MCP key never changes the API provider.
"""
from __future__ import annotations

import ipaddress
import json
import math
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wsp_sdk import (
    ProviderConfigError,
    ProviderRequestError,
    ProviderSpec,
    RequestDeadlineExceeded,
    dispatch_timeout,
    register_provider,
    search_result,
    source_result,
)

_URL = "https://search.parallel.ai/mcp"
_USER_AGENT = "hermes-web-search-plus/parallel-mcp"
_PROTOCOL = "2025-03-26"
_MAX_BYTES = 2 * 1024 * 1024


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _open_request(request, timeout):
    return _OPENER.open(request, timeout=timeout)


def _retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            stamp = parsedate_to_datetime(value)
            seconds = (stamp - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _decode(raw, content_type, request_id):
    try:
        text = raw.decode("utf-8")
        if content_type.split(";", 1)[0].strip().lower() == "text/event-stream":
            messages = []
            for event in text.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
                if data:
                    messages.append(json.loads(data))
            payload = next((item for item in messages if isinstance(item, dict) and item.get("id") == request_id), None)
        else:
            payload = json.loads(text)
    except (UnicodeError, ValueError):
        raise ProviderRequestError("parallel_mcp_invalid_response") from None
    if (
        not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
        or payload.get("id") != request_id or "error" in payload
        or not isinstance(payload.get("result"), dict)
    ):
        raise ProviderRequestError("parallel_mcp_invalid_response")
    return payload["result"]


class _Session:
    def __init__(self, key, timeout):
        # urllib errors can echo invalid header values. Reject malformed keys
        # here so a profile credential never becomes an error message.
        if key is not None and (
            not isinstance(key, str) or any(not 33 <= ord(char) <= 126 for char in key)
        ):
            raise ProviderConfigError("parallel_mcp_invalid_key")
        # Bound the whole handshake and tool call, not each step independently.
        self.expires = time.monotonic() + dispatch_timeout(timeout)
        self.headers = {
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if key:
            self.headers["Authorization"] = "Bearer " + key

    def request(self, payload=None, *, method="POST"):
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise RequestDeadlineExceeded()
        timeout = dispatch_timeout(remaining)
        request = Request(_URL, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=self.headers, method=method)
        try:
            with _open_request(request, timeout) as response:
                raw = response.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise ProviderRequestError("parallel_mcp_response_too_large")
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    if len(session_id) > 1024 or not session_id.isascii() or not session_id.isprintable():
                        raise ProviderRequestError("parallel_mcp_invalid_session")
                    self.headers["Mcp-Session-Id"] = session_id
                if method == "DELETE" or "id" not in (payload or {}):
                    return None
                return _decode(raw, response.headers.get("Content-Type", ""), payload["id"])
        except HTTPError as exc:
            retry = _retry_after(exc.headers.get("Retry-After")) if exc.headers else None
            exc.close()
            raise ProviderRequestError(
                f"parallel_mcp_http_{exc.code}", status_code=exc.code,
                transient=exc.code == 429 or exc.code >= 500, retry_after=retry,
            ) from None
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            raise ProviderRequestError("parallel_mcp_unavailable", transient=True) from None

    def search(self, query):
        try:
            initialized = self.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": _PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "hermes-web-search-plus", "version": "1"},
            }})
            if initialized.get("protocolVersion") != _PROTOCOL:
                raise ProviderRequestError("parallel_mcp_unsupported_protocol")
            self.headers["MCP-Protocol-Version"] = _PROTOCOL
            self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = self.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "web_search", "arguments": {"objective": query, "search_queries": [query]},
            }})
            if result.get("isError"):
                raise ProviderRequestError("parallel_mcp_tool_error")
            data = result.get("structuredContent")
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ProviderRequestError("parallel_mcp_invalid_sources")
            return data["results"]
        finally:
            if "Mcp-Session-Id" in self.headers:
                # Best effort; never hide the original failure or start cleanup
                # after the engine deadline. The server may also expire sessions.
                try:
                    self.expires = min(self.expires, time.monotonic() + 1)
                    self.request(method="DELETE")
                except ProviderRequestError:
                    pass


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() or not c.isprintable() for c in value):
        return None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not hostname or parts.username is not None
                or parts.password is not None or port is not None and not 1 <= port <= 65535):
            return None
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            if "." not in hostname or hostname.lower().rstrip(".").endswith((".localhost", ".local", ".internal")):
                return None
    except ValueError:
        return None
    return value


def _domains(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip().lower().lstrip("*.").rstrip(".") for item in value if isinstance(item, str) and item.strip()]


def _matches(host, domain):
    return host == domain or host.endswith("." + domain)


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c in "\n\t" or c.isprintable()).strip()[:limit]


def execute_search(search_module, prov, args, key, config, routing_info):
    section = config.get("parallel_mcp", {})
    try:
        timeout = float(section.get("timeout", 30))
        count = int(getattr(args, "max_results", 5))
    except (AttributeError, ValueError, TypeError, OverflowError):
        raise ProviderConfigError("parallel_mcp_invalid_config") from None
    if not math.isfinite(timeout) or not 0 < timeout <= 120 or not 1 <= count <= 100:
        raise ProviderConfigError("parallel_mcp_invalid_config")
    query = getattr(args, "query", None)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ProviderConfigError("parallel_mcp_invalid_query")
    include = _domains(getattr(args, "include_domains", None))
    exclude = _domains(getattr(args, "exclude_domains", None))
    raw_results = _Session(key, timeout).search(query)
    projected = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get("url"))
        if not url:
            continue
        host = urlsplit(url).hostname.lower().rstrip(".")
        if any(_matches(host, d) for d in exclude) or include and not any(_matches(host, d) for d in include):
            continue
        excerpts = item.get("excerpts")
        if not isinstance(excerpts, list) or any(not isinstance(text, str) for text in excerpts):
            raise ProviderRequestError("parallel_mcp_invalid_sources")
        projected.append(source_result(
            url, title=_text(item.get("title"), 1000), snippet=_text("\n\n".join(excerpts), 8000),
            date=_text(item.get("publish_date"), 100),
        ))
        if len(projected) >= count:
            break
    return search_result(prov, query, projected, metadata={"domain_filtering": "local"})


PROVIDER = register_provider(ProviderSpec(
    id="parallel-mcp", kind="search", env_var="PARALLEL_MCP_API_KEY",
    display_name="Parallel Search MCP",
    description="Opt-in Parallel MCP web search with source excerpts; no key required for the public tier.",
    config_section="parallel_mcp", capability_labels=("search", "keyless"),
    keyless=True, auto_allowed_by_default=False,
    free_tier="Free anonymous tier (explicit opt-in); rate limits apply",
    signup_url="https://docs.parallel.ai/integrations/mcp/search-mcp",
    execute_search=execute_search,
))
