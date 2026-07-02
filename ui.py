#!/usr/bin/env python3
"""Local read-only web UI for Web Search Plus diagnostics.

Serves a single-page dashboard on 127.0.0.1 with the doctor report, provider
health/cooldowns, adaptive provider stats, cache stats, and an offline
Routing v2 explainer.

Security model (see docs/USER_GUIDE.md, "Local web UI"):

- Binds to 127.0.0.1 only; the interface is intentionally not configurable.
- Every request must carry the startup-generated bearer token (``?token=`` on
  the initial page load, ``X-WSP-Token`` header on API calls). Pages on other
  origins never learn the token, so cross-site requests and DNS-rebinding
  hosts can neither read data nor trigger work.
- The Host header must name 127.0.0.1/localhost, rejecting DNS-rebinding
  requests outright.
- Responses never include secret values: provider reports carry
  ``key_present`` booleans only, mirroring the doctor/status surfaces.
- No endpoint performs provider or network calls; the routing explainer runs
  the offline Routing v2 scoring only.

Usage::

    python3 ui.py            # prints the tokenized URL to open
    python3 ui.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

# Keep sibling-module imports cwd-independent, matching __init__.py (#75).
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.append(str(_PLUGIN_DIR))

import cache as _cache  # noqa: E402
import search as _search  # noqa: E402
from config import load_config  # noqa: E402
from provider_registry import PROVIDER_SPECS  # noqa: E402
from provider_stats import get_provider_performance  # noqa: E402


UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765

# Hostnames a browser may use to reach the bound loopback interface. Anything
# else in the Host header is a DNS-rebinding attempt or a misdirected request.
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}

_STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"

# Routing-explainer queries are short strings; anything larger is abuse.
_MAX_ROUTE_BODY_BYTES = 16384

_TOKEN_QUERY_PARAM = "token"
_TOKEN_HEADER = "X-WSP-Token"


def _plugin_version() -> str:
    try:
        text = (Path(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^version:\s*"?([^"\n]+?)"?\s*$', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _provider_metadata() -> Dict[str, Dict[str, Any]]:
    """Public, non-secret display metadata per provider from the registry."""
    meta = {}
    for name, spec in PROVIDER_SPECS.items():
        meta[name] = {
            "display_name": spec.display_name,
            "capabilities": list(spec.capability_labels),
            "recommended": spec.recommended,
            "free_tier": spec.free_tier,
        }
    return meta


def build_overview() -> Dict[str, Any]:
    """Collect the read-only dashboard payload from local state only."""
    config = load_config()
    return {
        "version": _plugin_version(),
        "doctor": _search._build_doctor_report(config),
        "provider_meta": _provider_metadata(),
        "provider_stats": {name: get_provider_performance(name) for name in PROVIDER_SPECS},
        "cache": _cache.cache_stats(),
    }


def explain_route(query: str) -> Dict[str, Any]:
    """Run the offline Routing v2 decision for a query. No provider calls."""
    config = load_config()
    decision = _search.QueryAnalyzer(config).route(query)
    if "analysis_summary" not in decision:
        # Fallback decisions (e.g. no_available_providers) skip the summary
        # but still classify the query; surface it so the explainer stays
        # useful on unconfigured installs.
        analysis = decision.get("analysis") or {}
        decision["analysis_summary"] = {
            "routing_class": analysis.get("routing_class", "general"),
            "language_hint": analysis.get("language_hint", "en"),
        }
    return decision


class _UIRequestHandler(BaseHTTPRequestHandler):
    """Request handler bound to one server instance's token via subclassing."""

    server_version = "WSP-UI"
    token = ""  # set on the per-server subclass by create_server()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    # ----- shared plumbing -------------------------------------------------

    def _send_common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code: int, message: str) -> None:
        self._send_json(code, {"error": message})

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            return False  # IPv6 literals never match the IPv4 loopback bind
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
        return hostname in _ALLOWED_HOSTNAMES

    def _token_ok(self, parsed_query: str) -> bool:
        supplied = self.headers.get(_TOKEN_HEADER) or ""
        if not supplied:
            supplied = (parse_qs(parsed_query).get(_TOKEN_QUERY_PARAM) or [""])[0]
        expected = type(self).token
        return bool(supplied) and bool(expected) and secrets.compare_digest(supplied, expected)

    def _authorize(self, parsed_query: str) -> bool:
        if not self._host_allowed():
            self._deny(403, "Host header not allowed (expected 127.0.0.1 or localhost).")
            return False
        if not self._token_ok(parsed_query):
            self._deny(401, "Missing or invalid UI token. Restart the UI and open the printed URL.")
            return False
        return True

    # ----- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if not self._authorize(parsed.query):
            return
        if parsed.path == "/":
            self._serve_index()
        elif parsed.path == "/api/overview":
            self._send_json(200, build_overview())
        else:
            self._deny(404, "Unknown path.")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if not self._authorize(parsed.query):
            return
        if parsed.path != "/api/route":
            self._deny(404, "Unknown path.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._deny(400, "Invalid Content-Length.")
            return
        if length > _MAX_ROUTE_BODY_BYTES:
            self._deny(413, "Request body too large.")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._deny(400, "Body must be UTF-8 JSON.")
            return
        query = str(payload.get("query") or "").strip()
        if not query:
            self._deny(400, "Missing 'query'.")
            return
        self._send_json(200, explain_route(query))

    def _serve_index(self) -> None:
        try:
            body = _STATIC_INDEX.read_bytes()
        except OSError:
            self._deny(500, "static/index.html is missing from the plugin directory.")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)


def create_server(port: int = 0, token: Optional[str] = None) -> ThreadingHTTPServer:
    """Create the UI server bound to 127.0.0.1 with a per-instance token.

    ``port=0`` picks a free port (used by tests). The token is attached to a
    per-server handler subclass so concurrent instances cannot share auth.
    """
    handler = type(
        "BoundUIRequestHandler",
        (_UIRequestHandler,),
        {"token": token or secrets.token_urlsafe(24)},
    )
    return ThreadingHTTPServer((UI_HOST, port), handler)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only Web Search Plus dashboard (binds to 127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT, help="Port on 127.0.0.1 (default: %(default)s)")
    args = parser.parse_args(argv)

    server = create_server(port=args.port)
    token = server.RequestHandlerClass.token
    url = "http://{}:{}/?{}={}".format(UI_HOST, server.server_address[1], _TOKEN_QUERY_PARAM, token)
    print("Web Search Plus UI: {}".format(url))
    print("Binds to 127.0.0.1 only. The token is required on every request and changes on each start.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
