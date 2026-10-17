"""Parallel MCP discovery and actual WSP caller, without live provider quota."""
from __future__ import annotations

import json
import time
from http.client import BadStatusLine
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from wsp_core import config, provider_registry, routing, search
from wsp_core.http_client import ProviderRequestError, request_deadline_scope, request_timeout_cap
from wsp_core.sdk.errors import ProviderConfigError


def _provider():
    spec = provider_registry.PROVIDER_SPECS["parallel-mcp"]
    return spec, spec.execute_search.__globals__


class Response:
    def __init__(self, payload=None, *, raw=None, content_type="application/json", session=None):
        self.raw = raw if raw is not None else json.dumps(payload).encode()
        self.headers = {"Content-Type": content_type}
        if session:
            self.headers["Mcp-Session-Id"] = session
        self.closed = False

    def read(self, size=-1):
        return self.raw if size < 0 else self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True


def _args(**overrides):
    values = dict(query="Python release documentation", max_results=3, include_domains=[], exclude_domains=[])
    values.update(overrides)
    return SimpleNamespace(**values)


def _transport(monkeypatch, *, tool_result=None, fail=None, sse=False):
    spec, module = _provider()
    calls = []
    tool_result = tool_result or {"structuredContent": {"results": [
        {"url": "https://docs.python.org/3/", "title": "Python docs", "excerpts": ["Release notes and reference."]},
        {"url": "https://example.org/", "title": "Other", "excerpts": ["Other source."]},
    ]}}

    def open_request(request, timeout):
        method = request.get_method()
        payload = json.loads(request.data) if request.data else None
        calls.append((request, timeout, payload))
        if method == "DELETE":
            return Response(raw=b"")
        if payload["method"] == "initialize":
            return Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="test-session")
        assert request.get_header("Mcp-session-id") == "test-session"
        if payload["method"] == "notifications/initialized":
            return Response(raw=b"")
        assert payload["method"] == "tools/call"
        if fail:
            raise fail
        reply = {"jsonrpc": "2.0", "id": 2, "result": tool_result}
        if sse:
            return Response(raw=("event: message\ndata: " + json.dumps(reply) + "\n\n").encode(), content_type="text/event-stream")
        return Response(reply)

    monkeypatch.setitem(module, "_open_request", open_request)
    return spec, module, calls


def test_discovery_opt_in_and_existing_defaults():
    spec, _ = _provider()
    assert spec.kind == "search"
    assert spec.keyless and not spec.auto_allowed_by_default
    assert "parallel-mcp" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert not routing._provider_auto_allowed("parallel-mcp", {})
    assert not config.provider_configured("parallel-mcp", {})
    assert config.provider_configured("parallel-mcp", {"parallel_mcp": {"allow_public": True}})
    assert provider_registry.PROVIDER_SPECS["parallel"].env_var == "PARALLEL_API_KEY"
    assert routing._provider_auto_allowed("parallel", {}) is True


@pytest.mark.parametrize("sse", [False, True])
def test_actual_wsp_caller_projects_sources_and_observes_transport(monkeypatch, sse):
    _, _, calls = _transport(monkeypatch, sse=sse)
    monkeypatch.setenv("PARALLEL_API_KEY", "unrelated-existing-api-key")
    result = search.run_search_request(
        query="Python release documentation", provider="parallel-mcp", no_cache=True,
        include_domains=["python.org"], count=1,
        config={"parallel_mcp": {"allow_public": True}},
    )
    assert "error" not in result, result
    assert result["provider"] == "parallel-mcp"
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] == "https://docs.python.org/3/"
    assert "Release notes" in result["results"][0]["snippet"]
    assert [p["method"] if p else r.get_method() for r, _, p in calls] == [
        "initialize", "notifications/initialized", "tools/call", "DELETE",
    ]
    for request, timeout, _ in calls:
        assert request.full_url == "https://search.parallel.ai/mcp"
        assert request.get_header("User-agent").startswith("hermes-web-search-plus/")
        assert request.get_header("Authorization") is None
        assert 0 < timeout <= 30
    args = calls[2][2]["params"]["arguments"]
    assert args == {"objective": "Python release documentation", "search_queries": ["Python release documentation"]}


def test_missing_opt_in_starts_no_request(monkeypatch):
    _, _, calls = _transport(monkeypatch)
    result = search.run_search_request(query="Python docs", provider="parallel-mcp", no_cache=True, config={})
    assert "error" in result
    assert calls == []


def test_bearer_key_is_separate_and_redirects_are_refused(monkeypatch):
    spec, module, calls = _transport(monkeypatch)
    spec.execute_search(None, "parallel-mcp", _args(), "dedicated-mcp-key", {}, {})
    assert all(r.get_header("Authorization") == "Bearer dedicated-mcp-key" for r, _, _ in calls)
    assert module["_NoRedirectHandler"]().redirect_request(None, None, 302, "Found", {}, "https://other.example/") is None


def test_source_only_projection_filters_and_bounds(monkeypatch):
    payload = {"structuredContent": {"answer": "must not escape", "results": [
        {"url": "file:///etc/passwd", "excerpts": ["bad"]},
        {"url": "https://user:secret@example.org/", "excerpts": ["bad"]},
        {"url": "https://example.org/", "title": "skip", "excerpts": ["bad"]},
        {"url": "https://docs.python.org/3/", "title": "x" * 2000, "excerpts": ["y" * 20000], "publish_date": "2026-01-01"},
        {"url": "https://www.python.org/", "excerpts": ["later"]},
    ]}}
    spec, _, _ = _transport(monkeypatch, tool_result=payload)
    result = spec.execute_search(None, "parallel-mcp", _args(max_results=1, exclude_domains=["example.org"]), None, {}, {})
    assert len(result["results"]) == 1
    assert len(result["results"][0]["title"]) <= 1000
    assert len(result["results"][0]["snippet"]) <= 8000
    assert result["results"][0]["date"] == "2026-01-01"
    assert "must not escape" not in json.dumps(result)


@pytest.mark.parametrize("payload", [
    {"isError": True, "content": [{"type": "text", "text": "private diagnostic"}]},
    {"structuredContent": {"answer": "synthesis only"}},
    {"structuredContent": {"results": "invalid"}},
])
def test_tool_errors_are_typed_sanitized_and_close_session(monkeypatch, payload):
    spec, _, calls = _transport(monkeypatch, tool_result=payload)
    with pytest.raises(ProviderRequestError, match="parallel_mcp_") as error:
        spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert "private diagnostic" not in str(error.value)
    assert calls[-1][0].get_method() == "DELETE"


@pytest.mark.parametrize("failure", [TimeoutError("sensitive network detail"), BadStatusLine("sensitive protocol detail"), HTTPError("https://search.parallel.ai/mcp", 429, "quota", {"Retry-After": "30"}, None)])
def test_transport_failure_is_typed_and_session_is_closed(monkeypatch, failure):
    spec, _, calls = _transport(monkeypatch, fail=failure)
    with pytest.raises(ProviderRequestError) as error:
        spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert error.value.transient
    assert "sensitive" not in str(error.value)
    if isinstance(failure, HTTPError):
        assert error.value.status_code == 429 and error.value.retry_after == 30
    assert calls[-1][0].get_method() == "DELETE"


def test_deadline_cancellation_starts_no_request(monkeypatch):
    spec, _, calls = _transport(monkeypatch)
    with request_deadline_scope(time.monotonic() - 1):
        with pytest.raises(ProviderRequestError):
            spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert not calls


def test_attempt_cap_applies_to_every_request(monkeypatch):
    spec, _, calls = _transport(monkeypatch)
    with request_timeout_cap(0.5):
        spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert all(0 < timeout <= 0.5 for _, timeout, _ in calls)


def test_keyboard_interrupt_propagates_and_closes_session(monkeypatch):
    spec, _, calls = _transport(monkeypatch, fail=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert calls[-1][0].get_method() == "DELETE"


def test_sse_response_and_rpc_identity_are_checked():
    _, module = _provider()
    result = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"results": []}}}
    raw = (": heartbeat\n\nevent: message\ndata: " + json.dumps(result) + "\n\n").encode()
    assert module["_decode"](raw, "text/event-stream; charset=utf-8", 2) == result["result"]
    for raw in [b"not json", b'[]', b'{"jsonrpc":"2.0","id":7,"result":{}}']:
        with pytest.raises(ProviderRequestError):
            module["_decode"](raw, "application/json", 2)


def test_response_bound_closes_http_response(monkeypatch):
    spec, module = _provider()
    response = Response(raw=b"x" * (module["_MAX_BYTES"] + 1))
    monkeypatch.setitem(module, "_open_request", lambda request, timeout: response)
    with pytest.raises(ProviderRequestError, match="response_too_large"):
        spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert response.closed


def test_deadline_expiring_during_initialization_stops_tool_dispatch(monkeypatch):
    spec, module = _provider()
    calls = []
    clock = [0.0]
    monkeypatch.setitem(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def open_request(request, timeout):
        calls.append(request)
        clock[0] = 31.0
        return Response({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}, session="test-session")

    monkeypatch.setitem(module, "_open_request", open_request)
    with pytest.raises(ProviderRequestError):
        spec.execute_search(None, "parallel-mcp", _args(), None, {}, {})
    assert len(calls) == 1



@pytest.mark.parametrize("section", [{"timeout": float("nan")}, {"timeout": 0}, {"timeout": 121}, None])
def test_invalid_config_makes_no_request(monkeypatch, section):
    spec, _, calls = _transport(monkeypatch)
    with pytest.raises(ProviderConfigError, match="parallel_mcp_invalid_config"):
        spec.execute_search(None, "parallel-mcp", _args(), None, {"parallel_mcp": section}, {})
    assert not calls


def test_self_hosted_profile_preserves_explicit_only_and_allows_explicit_search(monkeypatch):
    _, _, calls = _transport(monkeypatch)
    runtime = config.apply_profile_effects({"profile": "self_hosted", "parallel_mcp": {"allow_public": True}})
    assert runtime["auto_routing"]["provider_priority"] == ["searxng", "keenable"]
    assert runtime["auto_routing"]["auto_allow"]["parallel-mcp"] is False
    result = search.run_search_request(
        query="Python release documentation", provider="parallel-mcp", no_cache=True, config=runtime,
    )
    assert "error" not in result, result
    assert result["provider"] == "parallel-mcp"
    assert result["results"][0]["url"] == "https://docs.python.org/3/"
    assert len(calls) == 4


@pytest.mark.parametrize("key", ["placeholder\r\nInjected: private", "placeholder secret", "placeholder\x7f", 123])
def test_malformed_keys_never_reach_http_or_error_text(monkeypatch, key):
    spec, _, calls = _transport(monkeypatch)
    with pytest.raises(ProviderConfigError) as error:
        spec.execute_search(None, "parallel-mcp", _args(), key, {}, {})
    assert str(error.value) == "parallel_mcp_invalid_key"
    assert calls == []
