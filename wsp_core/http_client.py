"""Shared HTTP client helpers for Web Search Plus providers."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import http.client
from contextlib import contextmanager
import io
import json
import math
import os
import re
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

# Wire bytes read from the socket and bytes after Content-Encoding decoding are
# limited separately, so a small compressed body cannot expand without bound.
MAX_WIRE_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DECODED_RESPONSE_BYTES = 16 * 1024 * 1024
# HTTP error bodies are only inspected to classify the error, never shown.
_ERROR_BODY_LIMIT = 4096
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


def _too_large() -> "ProviderRequestError":
    return ProviderRequestError("Provider response too large; refusing to read it.", transient=False)


def _redirect_blocked(reason: str) -> "ProviderRequestError":
    return ProviderRequestError(f"Provider redirect blocked ({reason}).", transient=False)


def _origin(url: str):
    """(scheme, host, effective port) or None when the URL is not plain HTTP(S)."""
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _same_origin(a: str, b: str) -> bool:
    first, second = _origin(a), _origin(b)
    return first is not None and first == second


def _check_redirect(old_url: str, new_url: str) -> None:
    """Allow only exact same-origin redirects; credentials travel with the request."""
    if not isinstance(new_url, str) or any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in new_url):
        raise _redirect_blocked("ambiguous target")
    try:
        netloc = urlsplit(new_url).netloc
    except ValueError:
        raise _redirect_blocked("malformed target") from None
    if "@" in netloc:
        raise _redirect_blocked("userinfo in target")
    if _origin(new_url) is None:
        raise _redirect_blocked("unsupported scheme or host")
    if not _same_origin(old_url, new_url):
        raise _redirect_blocked("different origin")


class _SameOriginRedirectHandler(_urllib_request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Validate before urllib parses Location itself (it raises ValueError on
        # malformed targets and would otherwise skip the origin check).
        location = headers.get("location") or headers.get("uri")
        if location:
            try:
                target = urljoin(req.full_url, location)
            except ValueError:
                raise _redirect_blocked("malformed target") from None
            _check_redirect(req.full_url, target)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_redirect(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_opener():
    """urllib opener with same-origin redirects and no file:/ftp:/data: handlers.

    Built per call on purpose: ProxyHandler snapshots the proxy environment at
    construction, and a cached opener would freeze it.
    """
    # Explicit handler list: filtering opener.handlers afterwards leaves
    # file:/ftp:/data: registered in opener.handle_open.
    opener = _urllib_request.OpenerDirector()
    for handler in (
        _urllib_request.ProxyHandler(),
        _urllib_request.UnknownHandler(),
        _urllib_request.HTTPHandler(),
        _urllib_request.HTTPSHandler(),
        _SameOriginRedirectHandler(),
        _urllib_request.HTTPDefaultErrorHandler(),
        _urllib_request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def _read_bounded_wire(response, limit: int | None = None) -> bytes:
    """Read at most ``limit`` (default MAX_WIRE_RESPONSE_BYTES) from a response, or refuse."""
    limit = MAX_WIRE_RESPONSE_BYTES if limit is None else limit
    declared = _response_header(response, "Content-Length").strip()
    if declared.isdigit() and int(declared) > limit:
        raise _too_large()
    try:
        raw = response.read(limit + 1)
    except TypeError:
        # Duck-typed responses whose read() takes no size argument.
        raw = response.read()
    if len(raw) > limit:
        raise _too_large()
    return raw


def _send_once(key: tuple, method: str, target: str, data, headers: dict, timeout: float):
    """Send one request, retrying once on a fresh socket if a reused one was stale."""
    conn, reused = _POOL.acquire(key, timeout)
    while True:
        try:
            conn.request(method, target, body=data, headers=headers)
            response = conn.getresponse()
            body = _read_bounded_wire(response)
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
                try:
                    next_url = urljoin(url, location)
                except ValueError:
                    raise _redirect_blocked("malformed target") from None
                _check_redirect(url, next_url)
                url = next_url
                if method not in {"GET", "HEAD"}:
                    method, data = "GET", None
                    headers = {k: v for k, v in headers.items() if k.lower() not in {"content-type", "content-length"}}
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


_TIMEOUT_CAP = threading.local()


@contextmanager
def request_timeout_cap(seconds: float | None):
    """Cap the socket timeout of every provider request made in this thread.

    Providers pass their own timeouts (often 30 s). When a fallback provider
    is waiting, a hung call should give up sooner; the engine wraps such an
    attempt in this cap instead of threading a timeout through every adapter.
    """
    previous = getattr(_TIMEOUT_CAP, "seconds", None)
    _TIMEOUT_CAP.seconds = seconds
    try:
        yield
    finally:
        _TIMEOUT_CAP.seconds = previous


def _capped_timeout(timeout: float) -> float:
    cap = getattr(_TIMEOUT_CAP, "seconds", None)
    return min(timeout, cap) if cap else timeout


def urlopen(req, timeout: float = 30):
    """Drop-in for urllib.request.urlopen: HTTP(S) only, same-origin redirects only."""
    timeout = _capped_timeout(timeout)
    url = req.full_url if isinstance(req, Request) else str(req)
    if _origin(url) is None:
        raise ProviderRequestError("Provider URL must be an http(s) URL.", transient=False)
    if not isinstance(req, Request) or not _keepalive_enabled():
        return _safe_opener().open(req, timeout=timeout)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if _proxy_applies(scheme, parts.hostname):
        return _safe_opener().open(req, timeout=timeout)
    return _pooled_open(req, timeout)


DEFAULT_USER_AGENT = "ClawdBot-WebSearchPlus/5.0.0"


class ProviderRequestError(Exception):
    """Structured provider error with retry/cooldown metadata."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        transient: bool = False,
        retry_after: float | None = None,
        out_of_credit: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient
        # Provider-requested wait (seconds) from a Retry-After header, if any.
        self.retry_after = retry_after
        # The provider account is empty. status_code stays the provider's real
        # status (Linkup answers 429, Serper 400); this flag drives QUOTA.
        self.out_of_credit = out_of_credit


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


def _bounded_inflate(raw: bytes, wbits: int, limit: int) -> bytes:
    """Decompress raw (possibly multi-member) data, never exceeding ``limit`` decoded bytes."""
    out = bytearray()
    data = raw
    while True:
        inflater = zlib.decompressobj(wbits)
        chunk = inflater.decompress(data, limit - len(out) + 1)
        out += chunk
        if len(out) > limit:
            raise _too_large()
        if not inflater.eof:
            raise zlib.error("truncated stream")
        data = inflater.unused_data
        if not data or wbits == -zlib.MAX_WBITS:
            return bytes(out)
        if wbits > 15 and not data.startswith(b"\x1f\x8b"):
            return bytes(out)


def _read_response_body(response, limit: int | None = None) -> bytes:
    """Read a bounded body and decode supported Content-Encoding values (bounded).

    ``limit`` replaces both default bounds, for bodies that are only inspected.
    """
    decoded_limit = MAX_DECODED_RESPONSE_BYTES if limit is None else limit
    raw = _read_bounded_wire(response, limit)
    if not raw:
        return raw
    encoding = _response_header(response, "Content-Encoding").strip().lower()

    if encoding in {"gzip", "x-gzip"} or raw.startswith(b"\x1f\x8b"):
        try:
            return _bounded_inflate(raw, 16 + zlib.MAX_WBITS, decoded_limit)
        except (OSError, EOFError, zlib.error):
            raise ProviderRequestError(
                "Provider sent a corrupted gzip response body. Please retry.",
                transient=True,
            )
    if encoding == "deflate":
        try:
            return _bounded_inflate(raw, zlib.MAX_WBITS, decoded_limit)
        except zlib.error:
            # Some servers send raw deflate without the zlib wrapper.
            try:
                return _bounded_inflate(raw, -zlib.MAX_WBITS, decoded_limit)
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
    if len(raw) > decoded_limit:
        raise _too_large()
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


def _friendly_http_error(code: int, error_detail: str = "") -> str:
    """Controlled message per status. Provider-supplied text is never reflected."""
    error_messages = {
        401: "Invalid or expired API key. Please check your credentials.",
        403: "Access forbidden. Your API key may not have permission for this operation.",
        429: "Rate limit exceeded. Please wait a moment and try again.",
        500: "Server error. The search provider is experiencing issues.",
        503: "Service unavailable. The search provider may be down.",
    }
    return error_messages.get(code, "API error")


def _parse_retry_after(error: HTTPError) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    value = _response_header(error, "Retry-After").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    return max(0.0, retry_at.timestamp() - time.time())


# Wording that says the account itself is empty. Some providers answer 429
# (Linkup) or 400 (Serper) for this; it is a quota problem, not a rate limit
# that clears in seconds. "Quota exceeded" is left out on purpose: providers
# also use it for per-minute limits.
_OUT_OF_CREDIT_WORDING = re.compile(
    r"(?<![a-z0-9])(?:insufficient[ _](?:credits|funds)|(?:out of|not enough|no) credits"
    r"|credit balance|not have enough funds)(?![a-z0-9])"
)
# Structured provider codes for the same thing (Linkup: INSUFFICIENT_FUNDS_CREDITS).
_OUT_OF_CREDIT_CODES = frozenset({
    "insufficient_funds_credits", "insufficient_credits", "insufficient_funds",
})
# A 429 carrying one of these is a rate limit that clears by itself, whatever
# its body says.
_RATE_LIMIT_HINT_HEADERS = ("Retry-After", "RateLimit-Reset", "X-RateLimit-Reset")


def _error_body_text(error: HTTPError) -> str:
    """The decoded start of an HTTP error body, for classification only.

    Decoded like any response body (Brave answers gzip). Callers must never
    copy this text into messages, logs or results.
    """
    try:
        body = _read_response_body(error, _ERROR_BODY_LIMIT)
    except Exception:
        return ""
    return body.decode("utf-8", "replace")


def _json_error_codes(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    nested = data.get("error")
    sources = (data, nested) if isinstance(nested, dict) else (data,)
    codes = (source.get(key) for source in sources for key in ("code", "error_code"))
    return [code.strip().lower() for code in codes if isinstance(code, str)]


def _out_of_credit(error: HTTPError) -> bool:
    """True when an HTTP error says the provider account is empty."""
    if error.code == 402:
        return True
    if error.code not in {400, 403, 429}:
        return False
    if error.code == 429 and any(_response_header(error, name).strip() for name in _RATE_LIMIT_HINT_HEADERS):
        return False
    text = _error_body_text(error)
    if any(code in _OUT_OF_CREDIT_CODES for code in _json_error_codes(text)):
        return True
    return _OUT_OF_CREDIT_WORDING.search(text.lower()) is not None


def _raise_provider_http_error(error: HTTPError) -> None:
    if _out_of_credit(error):
        raise ProviderRequestError(
            f"Out of credits: the provider account has no funds left; top it up or "
            f"remove its key (HTTP {error.code})",
            status_code=error.code,
            transient=False,
            out_of_credit=True,
        )
    friendly_msg = _friendly_http_error(error.code)
    raise ProviderRequestError(
        f"{friendly_msg} (HTTP {error.code})",
        status_code=error.code,
        transient=error.code in TRANSIENT_HTTP_CODES,
        retry_after=_parse_retry_after(error) if error.code == 429 else None,
    )


def make_request(url: str, headers: dict, body: dict, timeout: int = 30) -> dict:
    """Make HTTP POST request and return JSON response."""
    timeout = _capped_timeout(timeout)
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


def _capture_headers(source, names, sink: dict | None) -> None:
    if sink is None:
        return
    for name in names:
        value = _response_header(source, name)
        if value:
            sink[name] = value


def make_get_request(
    url: str,
    headers: dict,
    timeout: int = 30,
    *,
    capture_headers: tuple = (),
    response_headers: dict | None = None,
) -> dict:
    """Make HTTP GET request and return JSON response.

    ``capture_headers`` names response headers to copy into
    ``response_headers``, on success and on HTTP errors.
    """
    timeout = _capped_timeout(timeout)
    if "User-Agent" not in headers:
        headers["User-Agent"] = DEFAULT_USER_AGENT
    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=timeout) as response:
            _capture_headers(response, capture_headers, response_headers)
            return _read_json_response(response)
    except HTTPError as e:
        _capture_headers(e, capture_headers, response_headers)
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
