"""Shared HTTP client helpers for Web Search Plus providers."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import gzip
import http.client
import io
import json
import os
import socket
import ssl
import threading
import time
import urllib.request as _urllib_request
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request


TRANSIENT_HTTP_CODES = {429, 503}

# --- Keep-alive connection pool -------------------------------------------
# urllib.request opens a fresh TCP + TLS connection per call and sends
# ``Connection: close``. In long-lived processes (the Hermes gateway, the MCP
# server) that costs a DNS lookup and a TLS handshake on every provider call.
# ``urlopen`` below reuses idle HTTP/1.1 connections per (scheme, host, port)
# and falls back to urllib whenever a proxy applies, the scheme is not
# http/https, or WSP_HTTP_KEEPALIVE=0 is set.
_KEEPALIVE_IDLE_SECONDS = 30.0
_KEEPALIVE_MAX_IDLE_PER_HOST = 8
_MAX_REDIRECTS = 10
_REDIRECT_CODES = {301, 302, 303, 307, 308}
# Errors that mean a reused idle socket was already closed by the server.
_STALE_CONNECTION_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


class _ConnectionPool:
    """Thread-safe pool of idle keep-alive connections, dropped after fork."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple, list] = {}
        self._pid = os.getpid()
        self._ssl_context: ssl.SSLContext | None = None

    def _check_pid(self) -> None:
        # A forked child must never share TLS sockets with its parent.
        if self._pid != os.getpid():
            self._idle = {}
            self._pid = os.getpid()

    def new(self, key: tuple, timeout: float):
        scheme, host, port = key
        if scheme == "https":
            with self._lock:
                if self._ssl_context is None:
                    self._ssl_context = ssl.create_default_context()
                context = self._ssl_context
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def acquire(self, key: tuple, timeout: float):
        now = time.monotonic()
        with self._lock:
            self._check_pid()
            idle = self._idle.get(key) or []
            while idle:
                stamp, conn = idle.pop()
                if now - stamp <= _KEEPALIVE_IDLE_SECONDS and conn.sock is not None:
                    conn.timeout = timeout
                    conn.sock.settimeout(timeout)
                    return conn, True
                conn.close()
        return self.new(key, timeout), False

    def release(self, key: tuple, conn) -> None:
        with self._lock:
            if self._pid != os.getpid():
                return
            idle = self._idle.setdefault(key, [])
            if len(idle) < _KEEPALIVE_MAX_IDLE_PER_HOST:
                idle.append((time.monotonic(), conn))
                return
        conn.close()

    def reset(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
            self._pid = os.getpid()
        for conns in idle.values():
            for _, conn in conns:
                conn.close()


_POOL = _ConnectionPool()


def reset_connection_pool() -> None:
    """Close every idle pooled connection (tests, config reloads)."""
    _POOL.reset()


class _PooledResponse:
    """Minimal urllib-compatible response over an already-read body."""

    def __init__(self, url: str, status: int, reason: str, headers, body: bytes):
        self.url = url
        self.status = self.code = status
        self.reason = reason
        self.headers = headers
        self._body = io.BytesIO(body)

    def read(self, amt: int | None = None) -> bytes:
        return self._body.read() if amt is None else self._body.read(amt)

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self._body.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _keepalive_enabled() -> bool:
    return os.environ.get("WSP_HTTP_KEEPALIVE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _proxy_applies(scheme: str, host: str | None) -> bool:
    proxies = _urllib_request.getproxies()
    if not (proxies.get(scheme) or proxies.get("all")):
        return False
    return not (host and _urllib_request.proxy_bypass(host))


def _send_once(key: tuple, method: str, target: str, data, headers: dict, timeout: float):
    """Send one request, retrying once on a fresh socket if a reused one was stale."""
    conn, reused = _POOL.acquire(key, timeout)
    while True:
        try:
            conn.request(method, target, body=data, headers=headers)
            response = conn.getresponse()
            body = response.read()
        except _STALE_CONNECTION_ERRORS:
            conn.close()
            if not reused:
                raise
            conn, reused = _POOL.new(key, timeout), False
            continue
        except BaseException:
            conn.close()
            raise
        if response.will_close:
            conn.close()
        else:
            _POOL.release(key, conn)
        return response.status, response.reason, response.msg, body


def _pooled_open(req: Request, timeout: float):
    url = req.full_url
    method = req.get_method()
    data = req.data
    headers = dict(req.header_items())
    if data is not None and not any(k.lower() == "content-type" for k in headers):
        headers["Content-type"] = "application/x-www-form-urlencoded"
    status, reason, resp_headers, body = 0, "", {}, b""
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            port = parts.port or (443 if scheme == "https" else 80)
            key = (scheme, parts.hostname, port)
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            status, reason, resp_headers, body = _send_once(key, method, target, data, headers, timeout)
            location = resp_headers.get("Location")
            # Mirror urllib: follow any redirect for GET/HEAD, and 301/302/303
            # for other methods as a body-less GET.
            if status in _REDIRECT_CODES and location and (method in {"GET", "HEAD"} or status in {301, 302, 303}):
                url = urljoin(url, location)
                if method not in {"GET", "HEAD"}:
                    method, data = "GET", None
                    headers = {k: v for k, v in headers.items() if k.lower() not in {"content-type", "content-length"}}
                if urlsplit(url).scheme.lower() not in {"http", "https"}:
                    break
                continue
            if not 200 <= status < 300:
                raise HTTPError(url, status, reason, resp_headers, io.BytesIO(body))
            return _PooledResponse(url, status, reason, resp_headers, body)
        raise HTTPError(url, status, "Too many or unsupported redirects", resp_headers, io.BytesIO(body))
    except (HTTPError, IncompleteRead, TimeoutError, socket.timeout):
        raise
    except (OSError, http.client.HTTPException) as exc:
        # Match urllib: connection-level failures surface as URLError.
        raise URLError(exc) from exc


def urlopen(req, timeout: float = 30):
    """Drop-in for urllib.request.urlopen that reuses keep-alive connections."""
    if not isinstance(req, Request) or not _keepalive_enabled():
        return _urllib_request.urlopen(req, timeout=timeout)
    parts = urlsplit(req.full_url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or _proxy_applies(scheme, parts.hostname):
        return _urllib_request.urlopen(req, timeout=timeout)
    return _pooled_open(req, timeout)
DEFAULT_USER_AGENT = "ClawdBot-WebSearchPlus/4.3.1"


class ProviderRequestError(Exception):
    """Structured provider error with retry/cooldown metadata."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        transient: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        # Provider-requested wait (seconds) from a Retry-After header, if any.
        self.retry_after = retry_after


def _response_header(response, name: str) -> str:
    """Return an HTTP response header from urllib response/error objects."""
    if hasattr(response, "getheader"):
        value = response.getheader(name)
        if value is not None:
            return str(value)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get(name)
        except AttributeError:
            value = None
        if value is not None:
            return str(value)
    return ""


def _read_response_body(response) -> bytes:
    """Read an urllib response body and decode supported Content-Encoding values."""
    raw = response.read()
    encoding = _response_header(response, "Content-Encoding").strip().lower()

    if encoding in {"gzip", "x-gzip"} or raw.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            raise ProviderRequestError(
                "Provider sent a corrupted gzip response body. Please retry.",
                transient=True,
            )
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            # Some servers send raw deflate without the zlib wrapper.
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                raise ProviderRequestError(
                    "Provider sent a corrupted deflate response body. Please retry.",
                    transient=True,
                )
    if encoding == "br":
        raise ProviderRequestError(
            "Brotli-compressed response received, but web-search-plus does not bundle brotli support. "
            "Disable brotli for this provider or install a brotli-capable transport.",
            transient=False,
        )
    return raw


def _read_json_response(response) -> dict:
    """Read an urllib response as UTF-8 JSON with Content-Encoding handling."""
    body = _read_response_body(response)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderRequestError(
            "Provider sent a non-UTF-8 response body. Please retry.",
            transient=True,
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ProviderRequestError(
            "Provider sent an invalid JSON response. Please retry.",
            transient=True,
        )


def _friendly_http_error(code: int, error_detail: str) -> str:
    error_messages = {
        401: "Invalid or expired API key. Please check your credentials.",
        403: "Access forbidden. Your API key may not have permission for this operation.",
        429: "Rate limit exceeded. Please wait a moment and try again.",
        500: "Server error. The search provider is experiencing issues.",
        503: "Service unavailable. The search provider may be down.",
    }
    return error_messages.get(code, f"API error: {error_detail}")


def _extract_http_error_detail(error: HTTPError) -> str:
    try:
        error_body = _read_response_body(error).decode("utf-8", errors="replace") if error.fp else str(error)
    except (ProviderRequestError, OSError):
        # A corrupted error body must not mask the HTTP status we are reporting.
        return str(error)
    try:
        error_json = json.loads(error_body)
        return error_json.get("error") or error_json.get("message") or error_body
    except json.JSONDecodeError:
        return error_body[:500]


def _parse_retry_after(error: HTTPError) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    value = _response_header(error, "Retry-After").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    return max(0.0, retry_at.timestamp() - time.time())


def _raise_provider_http_error(error: HTTPError) -> None:
    error_detail = _extract_http_error_detail(error)
    friendly_msg = _friendly_http_error(error.code, error_detail)
    raise ProviderRequestError(
        f"{friendly_msg} (HTTP {error.code})",
        status_code=error.code,
        transient=error.code in TRANSIENT_HTTP_CODES,
        retry_after=_parse_retry_after(error) if error.code == 429 else None,
    )


def make_request(url: str, headers: dict, body: dict, timeout: int = 30) -> dict:
    """Make HTTP POST request and return JSON response."""
    # Ensure User-Agent is set (required by some APIs like Exa/Cloudflare)
    if "User-Agent" not in headers:
        headers["User-Agent"] = DEFAULT_USER_AGENT
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")

    try:
        with urlopen(req, timeout=timeout) as response:
            return _read_json_response(response)
    except HTTPError as e:
        _raise_provider_http_error(e)
        raise
    except URLError as e:
        reason = str(getattr(e, "reason", e))
        is_timeout = isinstance(getattr(e, "reason", None), socket.timeout) or "timed out" in reason.lower()
        raise ProviderRequestError(f"Network error: {reason}. Check your internet connection.", transient=is_timeout)
    except IncompleteRead as e:
        partial_len = len(getattr(e, "partial", b"") or b"")
        raise ProviderRequestError(
            f"Connection interrupted while reading response ({partial_len} bytes received). Please retry.",
            transient=True,
        )
    except (TimeoutError, socket.timeout):
        # socket.timeout only becomes a TimeoutError alias in Python 3.10;
        # catching both keeps read timeouts transient on 3.8/3.9.
        raise ProviderRequestError(f"Request timed out after {timeout}s. Try again or reduce max_results.", transient=True)


def make_get_request(url: str, headers: dict, timeout: int = 30) -> dict:
    """Make HTTP GET request and return JSON response."""
    if "User-Agent" not in headers:
        headers["User-Agent"] = DEFAULT_USER_AGENT
    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=timeout) as response:
            return _read_json_response(response)
    except HTTPError as e:
        _raise_provider_http_error(e)
        raise
    except URLError as e:
        reason = str(getattr(e, "reason", e))
        is_timeout = isinstance(getattr(e, "reason", None), socket.timeout) or "timed out" in reason.lower()
        raise ProviderRequestError(f"Network error: {reason}. Check your internet connection.", transient=is_timeout)
    except IncompleteRead as e:
        partial_len = len(getattr(e, "partial", b"") or b"")
        raise ProviderRequestError(
            f"Connection interrupted while reading response ({partial_len} bytes received). Please retry.",
            transient=True,
        )
    except (TimeoutError, socket.timeout):
        # socket.timeout only becomes a TimeoutError alias in Python 3.10;
        # catching both keeps read timeouts transient on 3.8/3.9.
        raise ProviderRequestError(f"Request timed out after {timeout}s. Try again or reduce max_results.", transient=True)
