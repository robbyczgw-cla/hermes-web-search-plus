"""Hardened loopback-only HTTP API for the WS-3 Operator Console."""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, cast
from urllib.parse import parse_qs, urlsplit

import operator_console_v3
from cache import CACHE_DIR
from config import load_config


LOOPBACK_HOST = "127.0.0.1"
MIN_TOKEN_CHARS = 16
MAX_TOKEN_CHARS = 256
ALLOWED_METHODS = "GET, HEAD"


class _OperatorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    operator_token: str
    cache_root: Path
    state_path: Path
    operator_config: Mapping[str, Any]
    configured_secrets: tuple[str, ...]
    plugin_version: str
    snapshot_backend: Any


def _configured_secrets(config: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        normalized = key.lower().replace("_", "").replace("-", "")
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif normalized in {"apikey", "secret", "token", "authorization"}:
            if isinstance(value, str) and value:
                values.append(value)

    walk(config)
    return tuple(values)


def _error_payload(code: str) -> dict[str, Any]:
    return {"schema_version": 1, "status": "failed", "error_code": code}


def _parse_limit(query: str) -> int:
    if not query:
        return 100
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("invalid query parameters") from exc
    if set(values) != {"limit"} or len(values["limit"]) != 1:
        raise ValueError("only one limit parameter is allowed")
    raw = values["limit"][0]
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return max(1, min(parsed, 100))


def _handler_class() -> type[BaseHTTPRequestHandler]:
    class OperatorRequestHandler(BaseHTTPRequestHandler):
        server_version = "WSP-Operator-Console"
        sys_version = ""

        @property
        def _operator_server(self) -> _OperatorHTTPServer:
            return cast(_OperatorHTTPServer, self.server)

        def log_message(self, format: str, *args: Any) -> None:
            """Do not log request targets or authorization-bearing metadata."""

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            """Replace stdlib HTML/501 responses with the hardened JSON boundary."""
            if code == 501 and getattr(self, "command", None):
                self._handle()
                return
            self._send_error_code(400, "wsp.console.bad_request")

        def _send_headers(
            self,
            *,
            status: int,
            content_length: int,
            content_type: str = "application/json; charset=utf-8",
            extra: Mapping[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            if extra:
                for name, value in extra.items():
                    self.send_header(name, value)
            self.end_headers()

        def _send_bytes(
            self,
            *,
            status: int,
            body: bytes,
            extra: Mapping[str, str] | None = None,
        ) -> None:
            self._send_headers(
                status=status,
                content_length=len(body),
                extra=extra,
            )
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_error_code(
            self,
            status: int,
            code: str,
            *,
            extra: Mapping[str, str] | None = None,
        ) -> None:
            body = (json.dumps(_error_payload(code), sort_keys=True) + "\n").encode(
                "utf-8"
            )
            self._send_bytes(status=status, body=body, extra=extra)

        def _host_is_valid(self) -> bool:
            supplied = self.headers.get("Host")
            expected = {
                LOOPBACK_HOST,
                f"{LOOPBACK_HOST}:{self._operator_server.server_port}",
            }
            return supplied in expected

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization")
            if not isinstance(supplied, str) or not supplied.startswith("Bearer "):
                return False
            candidate = supplied[len("Bearer ") :]
            return hmac.compare_digest(candidate, self._operator_server.operator_token)

        def _snapshot_body(self) -> bytes:
            parsed = urlsplit(self.path)
            backend = self._operator_server.snapshot_backend
            common = {"cache_root": self._operator_server.cache_root}
            if parsed.path == "/api/v3/overview":
                if parsed.query:
                    raise ValueError("overview does not accept query parameters")
                payload = backend.build_overview(
                    **common,
                    config=self._operator_server.operator_config,
                    state_path=self._operator_server.state_path,
                    plugin_version=self._operator_server.plugin_version,
                )
            elif parsed.path == "/api/v3/receipts":
                payload = backend.build_receipts(
                    **common,
                    limit=_parse_limit(parsed.query),
                )
            elif parsed.path == "/api/v3/benchmark-history":
                payload = backend.build_benchmark_history(
                    **common,
                    limit=_parse_limit(parsed.query),
                )
            else:
                raise FileNotFoundError
            return backend.serialize_endpoint_payload(
                payload,
                configured_secrets=self._operator_server.configured_secrets,
            )

        def _handle(self) -> None:
            if not self._host_is_valid():
                self._send_error_code(421, "wsp.console.misdirected_request")
                return
            if not self._authorized():
                self._send_error_code(
                    401,
                    "wsp.console.unauthorized",
                    extra={"WWW-Authenticate": "Bearer"},
                )
                return
            if self.command not in {"GET", "HEAD"}:
                self._send_error_code(
                    405,
                    "wsp.console.method_not_allowed",
                    extra={"Allow": ALLOWED_METHODS},
                )
                return
            try:
                body = self._snapshot_body()
            except FileNotFoundError:
                self._send_error_code(404, "wsp.console.not_found")
                return
            except (TypeError, ValueError):
                self._send_error_code(400, "wsp.console.bad_request")
                return
            except Exception:
                self._send_error_code(503, "wsp.console.snapshot_unavailable")
                return
            self._send_bytes(status=200, body=body)

        do_GET = _handle
        do_HEAD = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle
        do_OPTIONS = _handle
        do_TRACE = _handle
        do_CONNECT = _handle

    return OperatorRequestHandler


def create_server(
    *,
    host: str = LOOPBACK_HOST,
    port: int = 8765,
    token: str,
    cache_root: str | Path = CACHE_DIR,
    state_path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    plugin_version: str = "2.9.1",
    snapshot_backend: Any = operator_console_v3,
) -> ThreadingHTTPServer:
    """Create, but do not start, a strictly local read-only console server."""
    if host != LOOPBACK_HOST:
        raise ValueError("WS-3 Operator Console binds only to 127.0.0.1")
    if (
        not isinstance(token, str)
        or not MIN_TOKEN_CHARS <= len(token) <= MAX_TOKEN_CHARS
        or any(character in token for character in "\r\n\x00")
    ):
        raise ValueError("operator console token must be 16-256 safe characters")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("operator console port must be between 0 and 65535")

    active_config = dict(config) if isinstance(config, Mapping) else load_config()
    server = _OperatorHTTPServer((host, port), _handler_class())
    server.operator_token = token
    server.cache_root = Path(cache_root)
    server.state_path = (
        Path(state_path)
        if state_path is not None
        else Path(cache_root) / "state" / "v3.sqlite3"
    )
    server.operator_config = active_config
    server.configured_secrets = _configured_secrets(active_config)
    server.plugin_version = plugin_version
    server.snapshot_backend = snapshot_backend
    return server
