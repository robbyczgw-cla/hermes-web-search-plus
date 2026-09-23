"""Connection reuse (keep-alive pool) in http_client.

Runs against a real local HTTP/1.1 server so TCP connection reuse is observed,
not mocked.
"""
from __future__ import annotations

import gzip
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import http_client  # noqa: E402
from http_client import ProviderRequestError, make_get_request, make_request  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence test output
        pass

    def _send(self, code, body: bytes, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        srv = self.server
        with srv.lock:
            srv.peers.add(self.client_address)
            srv.requests.append((self.command, self.path, dict(self.headers)))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        if path == "/json":
            payload = {"path": self.path, "echo": json.loads(body) if body else None}
            self._send(200, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        elif path == "/close":
            self._send(200, b'{"ok": true}', {"Connection": "close"})
            self.close_connection = True
        elif path == "/gzip":
            self._send(200, gzip.compress(b'{"zipped": true}'), {"Content-Encoding": "gzip"})
        elif path == "/401":
            self._send(401, b'{"error": "bad key"}')
        elif path == "/429":
            self._send(429, b'{"error": "slow down"}', {"Retry-After": "7"})
        elif path == "/redirect":
            self._send(302, b"", {"Location": "/json?redirected=1"})
        elif path == "/slow":
            time.sleep(1.5)
            self._send(200, b"{}")
        else:
            self._send(404, b'{"error": "nope"}')

    do_GET = _handle
    do_POST = _handle


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.lock = threading.Lock()
    srv.peers = set()
    srv.requests = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    http_client.reset_connection_pool()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    http_client.reset_connection_pool()
    srv.shutdown()
    srv.server_close()


_POOLED_URLOPEN = http_client.urlopen


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    monkeypatch.setattr(http_client, "urlopen", _POOLED_URLOPEN)
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("WSP_HTTP_KEEPALIVE", raising=False)


def test_sequential_requests_reuse_one_connection(server):
    srv, base = server
    for i in range(5):
        assert make_request(f"{base}/json", {"Content-Type": "application/json"}, {"i": i})["echo"] == {"i": i}
    for i in range(3):
        assert make_get_request(f"{base}/json?i={i}", {})["path"] == f"/json?i={i}"
    assert len(srv.peers) == 1


def test_request_headers_and_body_match_urllib(server):
    srv, base = server
    make_request(f"{base}/json", {"X-API-KEY": "k", "Content-Type": "application/json"}, {"q": "x"})
    method, path, raw = srv.requests[-1]
    headers = {k.title(): v for k, v in raw.items()}
    assert (method, path) == ("POST", "/json")
    assert headers["X-Api-Key"] == "k"
    assert headers["User-Agent"] == http_client.DEFAULT_USER_AGENT
    assert headers["Content-Length"] == str(len(json.dumps({"q": "x"}).encode()))
    assert headers["Host"] == base.split("//", 1)[1]


def test_server_closed_connection_is_replaced(server):
    srv, base = server
    make_get_request(f"{base}/close", {})
    assert make_get_request(f"{base}/json", {})["path"] == "/json"
    assert len(srv.peers) == 2


def test_stale_pooled_connection_is_retried_once(server):
    srv, base = server
    make_get_request(f"{base}/json", {})
    # Simulate the server dropping an idle keep-alive socket behind our back.
    for conns in http_client._POOL._idle.values():
        for _, conn in conns:
            conn.sock.shutdown(socket.SHUT_RDWR)
    assert make_get_request(f"{base}/json?after=stale", {})["path"] == "/json?after=stale"


def test_http_errors_keep_structured_mapping(server):
    _, base = server
    with pytest.raises(ProviderRequestError) as exc:
        make_get_request(f"{base}/401", {})
    assert exc.value.status_code == 401 and not exc.value.transient
    with pytest.raises(ProviderRequestError) as exc:
        make_request(f"{base}/429", {}, {})
    assert exc.value.status_code == 429 and exc.value.transient and exc.value.retry_after == 7.0
    # Connection stays usable after an error response.
    assert make_get_request(f"{base}/json", {})["path"] == "/json"


def test_gzip_body_is_decoded(server):
    _, base = server
    assert make_get_request(f"{base}/gzip", {}) == {"zipped": True}


def test_redirect_is_followed(server):
    _, base = server
    assert make_get_request(f"{base}/redirect", {})["path"] == "/json?redirected=1"


def test_read_timeout_is_transient(server):
    _, base = server
    with pytest.raises(ProviderRequestError) as exc:
        make_get_request(f"{base}/slow", {}, timeout=0.3)
    assert exc.value.transient


def test_connection_refused_is_network_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ProviderRequestError, match="Network error"):
        make_get_request(f"http://127.0.0.1:{port}/json", {}, timeout=2)


def test_concurrent_threads_get_their_own_responses(server):
    srv, base = server
    errors, results = [], {}

    def worker(n):
        try:
            for i in range(10):
                out = make_request(f"{base}/json", {}, {"w": n, "i": i})
                results[(n, i)] = out["echo"] == {"w": n, "i": i}
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(results) == 160 and all(results.values())
    # Pool bounds idle connections; total TCP connections stay far below 160.
    assert len(srv.peers) <= 16


def test_proxy_env_falls_back_to_urllib(server, monkeypatch):
    _, base = server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    sentinel = mock.MagicMock()
    sentinel.__enter__.return_value = sentinel
    sentinel.read.return_value = b'{"via": "urllib"}'
    sentinel.getheader.return_value = None
    with mock.patch("urllib.request.urlopen", return_value=sentinel) as fallback:
        assert make_get_request(f"{base}/json", {}) == {"via": "urllib"}
    fallback.assert_called_once()


def test_keepalive_can_be_disabled(server, monkeypatch):
    srv, base = server
    monkeypatch.setenv("WSP_HTTP_KEEPALIVE", "0")
    for _ in range(3):
        make_get_request(f"{base}/json", {})
    assert len(srv.peers) == 3


def test_pool_is_dropped_after_fork(server, monkeypatch):
    _, base = server
    make_get_request(f"{base}/json", {})
    assert http_client._POOL._idle
    monkeypatch.setattr(http_client._POOL, "_pid", os.getpid() + 1)
    make_get_request(f"{base}/json", {})
    assert http_client._POOL._pid == os.getpid()
