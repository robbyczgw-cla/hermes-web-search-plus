from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest


TOKEN = "task-4-test-token"
ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "web" / "v3" / "console"


class FakeSnapshots:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    def build_overview(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(("overview", None))
        return {"schema_version": 1}

    def build_receipts(self, *, limit: int, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(("receipts", limit))
        return {"schema_version": 1, "receipts": []}

    def build_benchmark_history(
        self, *, limit: int, **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("benchmark-history", limit))
        return {
            "schema_version": 1,
            "runs": [],
            "availability": {"search": "not_collected", "extract": "not_collected"},
        }

    @staticmethod
    def serialize_endpoint_payload(
        payload: dict[str, Any], **_kwargs: Any
    ) -> bytes:
        return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


@contextmanager
def running_server(tmp_path: Path) -> Iterator[tuple[Any, FakeSnapshots]]:
    ui = importlib.import_module("ui")
    backend = FakeSnapshots()
    server = ui.create_server(
        host="127.0.0.1",
        port=0,
        token=TOKEN,
        cache_root=tmp_path,
        snapshot_backend=backend,
        config={},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    server: Any,
    method: str,
    target: str,
    *,
    token: str | None = TOKEN,
    host: str | None = None,
    cookie: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    headers: dict[str, str] = {
        "Host": host or f"127.0.0.1:{server.server_port}",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if cookie is not None:
        headers["Cookie"] = cookie
    connection.request(method, target, headers=headers)
    response = connection.getresponse()
    body = response.read()
    result = response.status, {key.lower(): value for key, value in response.getheaders()}, body
    connection.close()
    return result


def test_server_requires_literal_loopback_and_strong_startup_token(tmp_path: Path) -> None:
    ui = importlib.import_module("ui")
    for host in ("0.0.0.0", "::", "localhost", "192.168.1.20", "100.100.100.100"):
        with pytest.raises(ValueError, match="127.0.0.1"):
            ui.create_server(host=host, port=0, token=TOKEN, cache_root=tmp_path)
    for token in ("", "short", "x\nheader-injection"):
        with pytest.raises(ValueError, match="token"):
            ui.create_server(host="127.0.0.1", port=0, token=token, cache_root=tmp_path)


def test_server_rejects_bad_host_and_missing_or_wrong_token(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, backend):
        status, headers, body = request(
            server,
            "GET",
            "/api/v3/overview",
            host="evil.example",
        )
        assert status == 421
        assert b"evil.example" not in body
        assert backend.calls == []

        status, headers, body = request(
            server, "GET", "/api/v3/overview", token=None
        )
        assert status == 401
        assert headers["www-authenticate"] == "Bearer"
        assert TOKEN.encode() not in body
        assert backend.calls == []

        status, _, body = request(
            server, "GET", "/api/v3/overview", token="wrong-token-value"
        )
        assert status == 401
        assert TOKEN.encode() not in body
        assert backend.calls == []


def test_server_exposes_only_three_get_head_json_routes(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, backend):
        status, headers, body = request(server, "GET", "/api/v3/overview")
        assert status == 200
        assert json.loads(body) == {"schema_version": 1}
        assert headers["content-type"] == "application/json; charset=utf-8"

        status, _, body = request(server, "GET", "/api/v3/receipts?limit=999")
        assert status == 200
        assert json.loads(body) == {"schema_version": 1, "receipts": []}

        status, get_headers, get_body = request(
            server, "GET", "/api/v3/benchmark-history?limit=2"
        )
        assert status == 200
        status, head_headers, head_body = request(
            server, "HEAD", "/api/v3/benchmark-history?limit=2"
        )
        assert status == 200
        assert head_body == b""
        assert head_headers["content-length"] == get_headers["content-length"]
        assert int(head_headers["content-length"]) == len(get_body)

        assert backend.calls == [
            ("overview", None),
            ("receipts", 100),
            ("benchmark-history", 2),
            ("benchmark-history", 2),
        ]

        status, _, _ = request(server, "GET", "/api/v3/missing")
        assert status == 404
        status, _, _ = request(server, "GET", "/api/v3/receipts?unknown=1")
        assert status == 400


@pytest.mark.parametrize(
    "method",
    [
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "TRACE",
        "CONNECT",
        "PROPFIND",
        "BREW",
    ],
)
def test_server_blocks_every_non_read_method(method: str, tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, backend):
        status, headers, body = request(server, method, "/api/v3/overview")
        assert status == 405
        assert headers["allow"] == "GET, HEAD"
        assert backend.calls == []
        assert TOKEN.encode() not in body


def test_every_response_has_fail_closed_browser_security_headers(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _backend):
        for token, expected_status in ((TOKEN, 200), (None, 401)):
            status, headers, _ = request(
                server, "GET", "/api/v3/overview", token=token
            )
            assert status == expected_status
            assert headers["cache-control"] == "no-store"
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert headers["referrer-policy"] == "no-referrer"
            assert headers["cross-origin-resource-policy"] == "same-origin"
            assert "default-src 'none'" in headers["content-security-policy"]
            assert "access-control-allow-origin" not in headers


def test_frontend_assets_are_byte_identical_and_csp_clean() -> None:
    expected = {
        "index.html": "c3e0e498ced835032c20ee2c7fed319e344fceb7fb5bc07c3449fc22e0c293d0",
        "styles.css": "df7f2e531f3296fb8a3b453de39fbddf44211198b736aa707650bf1acc98a5f0",
        "app.js": "4daab257bdccd433d7a914aeafd15cd6bde10e2ab2fb143292a10c00cddfb62c",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((STATIC_ROOT / name).read_bytes()).hexdigest() == digest

    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]+src=)", html, re.IGNORECASE)
    assert "style=" not in html.lower()
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)
    assert 'method: "GET"' in script
    assert not re.search(r'method\s*:\s*["\'](?:POST|PUT|PATCH|DELETE)', script)


def test_server_serves_only_three_static_assets_with_static_csp(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _backend):
        expected = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        for route, (name, content_type) in expected.items():
            status, headers, body = request(server, "GET", route)
            assert status == 200
            assert body == (STATIC_ROOT / name).read_bytes()
            assert headers["content-type"] == content_type
            csp = headers["content-security-policy"]
            assert "script-src 'self'" in csp
            assert "style-src 'self'" in csp
            assert "connect-src 'self'" in csp
            assert "'unsafe-inline'" not in csp

            status, head_headers, head_body = request(server, "HEAD", route)
            assert status == 200
            assert head_body == b""
            assert int(head_headers["content-length"]) == len(body)

        assert request(server, "GET", "/favicon.ico")[0] == 404
        assert request(server, "GET", "/app.js.map")[0] == 404
        assert request(server, "GET", "/../ui.py")[0] == 404


def test_browser_token_bootstrap_sets_opaque_session_cookie(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, backend):
        status, headers, body = request(
            server, "GET", f"/?token={TOKEN}", token=None
        )
        assert status == 303
        assert headers["location"] == "/"
        cookie = headers["set-cookie"]
        assert cookie.startswith("wsp_console_session=")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        assert TOKEN not in cookie
        assert TOKEN.encode() not in body

        session = cookie.split(";", 1)[0]
        status, _, body = request(server, "GET", "/", token=None, cookie=session)
        assert status == 200
        assert body == (STATIC_ROOT / "index.html").read_bytes()
        status, _, body = request(
            server, "GET", "/api/v3/overview", token=None, cookie=session
        )
        assert status == 200
        assert json.loads(body) == {"schema_version": 1}
        assert backend.calls == [("overview", None)]

        status, headers, body = request(
            server, "GET", "/?token=definitely-wrong-token", token=None
        )
        assert status == 401
        assert "set-cookie" not in headers
        assert b"definitely-wrong-token" not in body


def test_static_routes_remain_get_head_only(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, backend):
        status, headers, _ = request(server, "POST", "/")
        assert status == 405
        assert headers["allow"] == "GET, HEAD"
        assert backend.calls == []


def test_static_root_and_assets_refuse_symlinks(tmp_path: Path) -> None:
    ui = importlib.import_module("ui")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(STATIC_ROOT, target_is_directory=True)
    with pytest.raises(ValueError, match="static"):
        ui.create_server(
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            cache_root=tmp_path,
            static_root=linked_root,
        )

    real_parent = tmp_path / "real-parent"
    assets = real_parent / "assets"
    assets.mkdir(parents=True)
    for name in ("index.html", "styles.css", "app.js"):
        (assets / name).write_bytes((STATIC_ROOT / name).read_bytes())
    (assets / "app.js").unlink()
    (assets / "app.js").symlink_to(STATIC_ROOT / "app.js")
    with pytest.raises(ValueError, match="static"):
        ui.create_server(
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            cache_root=tmp_path,
            static_root=assets,
        )

    (assets / "app.js").unlink()
    (assets / "app.js").write_bytes((STATIC_ROOT / "app.js").read_bytes())
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="static"):
        ui.create_server(
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            cache_root=tmp_path,
            static_root=linked_parent / "assets",
        )
