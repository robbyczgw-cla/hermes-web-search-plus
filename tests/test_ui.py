"""Tests for the local read-only web UI (ui.py).

All tests run against a real ThreadingHTTPServer on 127.0.0.1 with an
ephemeral port; no external network access happens (routing explainer is
offline, overview reads local state only).
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import threading
from pathlib import Path
from unittest import mock

import pytest

UI_PATH = Path(__file__).resolve().parents[1] / "ui.py"
ui_spec = importlib.util.spec_from_file_location("wsp_ui_under_test", UI_PATH)
ui = importlib.util.module_from_spec(ui_spec)
assert ui_spec.loader is not None
ui_spec.loader.exec_module(ui)

TOKEN = "test-ui-token"


@pytest.fixture()
def ui_server(monkeypatch, tmp_path):
    monkeypatch.setattr(ui._cache, "CACHE_DIR", tmp_path / "cache")
    server = ui.create_server(port=0, token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(server, method, path, body=None, token=TOKEN, host=None, extra_headers=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    if token is not None:
        headers["X-WSP-Token"] = token
    if host is not None:
        headers["Host"] = host
    if extra_headers:
        headers.update(extra_headers)
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response, raw


def test_api_requires_token(ui_server):
    response, raw = _request(ui_server, "GET", "/api/overview", token=None)
    assert response.status == 401
    assert b"token" in raw.lower()

    response, _ = _request(ui_server, "GET", "/api/overview", token="wrong-token")
    assert response.status == 401


def test_token_accepted_via_query_param_for_page_load(ui_server):
    response, raw = _request(ui_server, "GET", "/?token=" + TOKEN, token=None)
    assert response.status == 200
    assert b"Web Search Plus" in raw
    assert response.getheader("Content-Security-Policy") is not None


def test_index_without_token_is_denied(ui_server):
    response, _ = _request(ui_server, "GET", "/", token=None)
    assert response.status == 401


def test_dns_rebinding_host_header_is_rejected(ui_server):
    response, raw = _request(ui_server, "GET", "/api/overview", host="evil.example")
    assert response.status == 403
    assert b"Host" in raw

    response, _ = _request(ui_server, "GET", "/api/overview", host="evil.example:8765")
    assert response.status == 403


def test_localhost_host_headers_are_accepted(ui_server):
    port = ui_server.server_address[1]
    for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "localhost"):
        response, _ = _request(ui_server, "GET", "/api/overview", host=host)
        assert response.status == 200, host


def test_overview_reports_key_presence_without_leaking_values(ui_server, monkeypatch):
    secret = "tvly-THIS-MUST-NEVER-APPEAR"
    monkeypatch.setenv("TAVILY_API_KEY", secret)

    response, raw = _request(ui_server, "GET", "/api/overview")
    assert response.status == 200
    assert secret.encode("utf-8") not in raw

    overview = json.loads(raw)
    providers = {p["provider"]: p for p in overview["doctor"]["providers"]}
    assert providers["tavily"]["key_present"] is True
    assert overview["provider_meta"]["tavily"]["display_name"] == "Tavily"
    assert "cache" in overview
    assert overview["version"] != ""


def test_route_explainer_runs_offline(ui_server):
    with mock.patch.object(ui._search, "get_api_key", return_value="test-key"), \
            mock.patch.object(ui._search, "make_request", side_effect=AssertionError("network call")), \
            mock.patch.object(ui._search, "make_get_request", side_effect=AssertionError("network call")):
        response, raw = _request(ui_server, "POST", "/api/route", body={"query": "pydantic BaseModel docs"})

    assert response.status == 200
    decision = json.loads(raw)
    assert decision["analysis_summary"]["routing_class"] == "docs_api"
    assert decision["provider"]
    assert "scores" in decision


def test_route_explainer_classifies_even_without_configured_providers(ui_server):
    # Unconfigured installs get the no_available_providers fallback decision,
    # which must still carry the routing class for the explainer UI.
    with mock.patch.object(ui._search, "get_api_key", return_value=None):
        response, raw = _request(ui_server, "POST", "/api/route", body={"query": "EU AI Act official PDF"})

    assert response.status == 200
    decision = json.loads(raw)
    assert decision["reason"] == "no_available_providers"
    assert decision["analysis_summary"]["routing_class"] == "policy_pdf"


def test_route_explainer_validates_input(ui_server):
    response, _ = _request(ui_server, "POST", "/api/route", body={"query": "   "})
    assert response.status == 400

    response, _ = _request(ui_server, "POST", "/api/route", body={})
    assert response.status == 400

    port = ui_server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/route", body=b"not json", headers={"X-WSP-Token": TOKEN})
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 400


def test_oversized_route_body_is_rejected(ui_server):
    big_query = "x" * (ui._MAX_ROUTE_BODY_BYTES + 100)
    response, _ = _request(ui_server, "POST", "/api/route", body={"query": big_query})
    assert response.status == 413


def test_unknown_paths_return_404(ui_server):
    response, _ = _request(ui_server, "GET", "/api/secrets")
    assert response.status == 404
    response, _ = _request(ui_server, "POST", "/api/overview")
    assert response.status == 404


def test_server_binds_loopback_only(ui_server):
    assert ui_server.server_address[0] == "127.0.0.1"
    assert ui.UI_HOST == "127.0.0.1"


def test_each_server_gets_its_own_token():
    first = ui.create_server(port=0)
    second = ui.create_server(port=0)
    try:
        assert first.RequestHandlerClass.token
        assert first.RequestHandlerClass.token != second.RequestHandlerClass.token
    finally:
        first.server_close()
        second.server_close()
