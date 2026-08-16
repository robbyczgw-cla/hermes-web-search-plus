from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import provider_registry


def _provider():
    spec = provider_registry.PROVIDER_SPECS["donsetch"]
    return spec, spec.execute_search.__globals__


def _search_args(**overrides):
    values = {
        "query": "Rust Tokio official tutorial",
        "max_results": 3,
        "search_type": "search",
        "time_range": None,
        "freshness": None,
        "images": False,
        "include_domains": [],
        "exclude_domains": [],
        "category": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_donsetch_registers_as_explicit_only_search_and_extract_provider():
    spec, _module = _provider()
    assert spec.kind == "both"
    assert spec.env_var == "DONSETCH_BIN"
    assert spec.keyless is False
    assert spec.auto_allowed_by_default is False
    assert spec.supports_freshness is False
    assert "donsetch" in provider_registry.SEARCH_PROVIDER_IDS
    assert "donsetch" in provider_registry.EXTRACT_PROVIDER_IDS
    assert "donsetch" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["donsetch"] is False
    assert "hound" not in provider_registry.PROVIDER_SPECS


def test_donsetch_binary_resolution_is_explicit_and_fail_closed(tmp_path):
    _spec, module = _provider()
    with pytest.raises(RuntimeError, match="donsetch_binary_not_configured"):
        module["_resolve_binary"](None, {})
    binary = tmp_path / "donsetch"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    assert module["_resolve_binary"](str(binary), {}) == str(binary)


def test_mcp_response_parser_extracts_structured_content():
    _spec, module = _provider()
    response = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [{"type": "text", "text": "body"}],
            "structuredContent": {"content_ok": True, "status": 200},
        },
    }
    parsed = module["_mcp_response_from_stdout"](json.dumps(response), 2)
    assert parsed == {
        "structured": {"content_ok": True, "status": 200},
        "text": "body",
    }


def test_stdio_handshake_waits_for_initialize_response(tmp_path):
    _spec, module = _provider()
    binary = tmp_path / "strict-mcp"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "def receive(method):\n"
        "    message = json.loads(sys.stdin.readline())\n"
        "    assert message.get('method') == method, message\n"
        "    return message\n"
        "initialize = receive('initialize')\n"
        "print(json.dumps({'jsonrpc': '2.0', 'id': initialize['id'], 'result': {}}), flush=True)\n"
        "receive('notifications/initialized')\n"
        "call = receive('tools/call')\n"
        "print(json.dumps({'jsonrpc': '2.0', 'id': call['id'], 'result': {'content': [{'type': 'text', 'text': 'body'}], 'structuredContent': {'ok': True}}}), flush=True)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    result = module["_call_donsetch_tool"](str(binary), "web_search", {"query": "test"}, 5)
    assert result == {"structured": {"ok": True}, "text": "body"}


def test_search_projects_donsetch_results_and_metadata(monkeypatch):
    spec, module = _provider()
    calls = []

    def fake_call(binary, tool, arguments, timeout):
        calls.append((binary, tool, arguments, timeout))
        return {
            "structured": {
                "intent": "Code",
                "cached": False,
                "weak": False,
                "elapsed_ms": 321,
                "engines": [
                    {"engine": "ddg", "status": "ok"},
                    {"engine": "brave", "status": "blocked:429"},
                ],
                "results": [
                    {
                        "url": "https://tokio.rs/tokio/tutorial",
                        "title": "Tutorial | Tokio",
                        "snippet": "Tokio tutorial",
                        "score": 1.5,
                        "consensus": 3,
                        "engines": ["ddg", "brave"],
                    },
                    {
                        "url": "https://example.org/nope",
                        "title": "Nope",
                        "snippet": "",
                        "score": "bad",
                    },
                ],
            },
            "text": "",
        }

    monkeypatch.setitem(module, "_call_donsetch_tool", fake_call)
    result = spec.execute_search(
        None,
        "donsetch",
        _search_args(include_domains=["tokio.rs"]),
        "/bin/true",
        {"donsetch": {"timeout": 30}},
        {},
    )
    assert calls[0][1] == "web_search"
    assert calls[0][2]["query"] == "Rust Tokio official tutorial"
    assert calls[0][2]["max_results"] == 3
    assert result["provider"] == "donsetch"
    assert [item["url"] for item in result["results"]] == [
        "https://tokio.rs/tokio/tutorial"
    ]
    assert result["results"][0]["engines_consensus"] == "3"
    assert result["metadata"]["engines_used"] == ["ddg"]
    assert result["metadata"]["engine_blocked"] == ["brave"]
    assert result["metadata"]["duration_ms"] == 321.0


def test_search_rejects_unsupported_freshness_before_network(monkeypatch):
    spec, module = _provider()
    monkeypatch.setitem(module, "_call_donsetch_tool", lambda *_args: pytest.fail("network"))
    with pytest.raises(RuntimeError, match="donsetch_freshness_unsupported"):
        spec.execute_search(
            None,
            "donsetch",
            _search_args(freshness="day"),
            "/bin/true",
            {},
            {},
        )


def test_extract_projects_markdown_and_marks_raw_html_as_unsupported(monkeypatch):
    spec, module = _provider()
    calls = []

    def fake_call(binary, tool, arguments, timeout):
        calls.append((tool, arguments))
        return {
            "structured": {
                "url": "https://example.org/page",
                "title": "Example",
                "status": 200,
                "content_ok": True,
                "content_kind": "Article",
                "quality": 0.8,
                "lang": "en",
                "site": "example.org",
                "verdict": "ContentOk",
                "pdf": None,
            },
            "text": "# Example\n\nBody",
        }

    monkeypatch.setitem(module, "_call_donsetch_tool", fake_call)
    result = spec.execute_extract(
        None,
        "donsetch",
        ["https://example.org/page"],
        "/bin/true",
        "markdown",
        True,
        True,
        False,
        {"donsetch": {"timeout": 30}},
        False,
    )
    assert calls[0][0] == "web_fetch"
    assert calls[0][1]["tier"] == "auto"
    assert calls[0][1]["media"] is True
    assert result["results"][0]["content"] == "# Example\n\nBody"
    assert result["results"][0]["fetcher"] == "donsetch"
    assert result["results"][0]["raw_error"] == "donsetch_raw_html_unsupported"


def test_extract_render_js_requests_browser_tier(monkeypatch):
    spec, module = _provider()
    seen = []

    def fake_call(_binary, _tool, arguments, _timeout):
        seen.append(arguments)
        return {
            "structured": {
                "url": arguments["url"],
                "status": 200,
                "content_ok": True,
                "content_kind": "Page",
                "verdict": "ContentOk",
            },
            "text": "content",
        }

    monkeypatch.setitem(module, "_call_donsetch_tool", fake_call)
    spec.execute_extract(
        None,
        "donsetch",
        ["https://example.org/page"],
        "/bin/true",
        "markdown",
        False,
        False,
        True,
        {},
        False,
    )
    assert seen[0]["tier"] == "2"


def test_extract_rejects_non_markdown_without_network(monkeypatch):
    spec, module = _provider()
    monkeypatch.setitem(module, "_call_donsetch_tool", lambda *_args: pytest.fail("network"))
    with pytest.raises(RuntimeError, match="donsetch_output_format_unsupported"):
        spec.execute_extract(
            None,
            "donsetch",
            ["https://example.org/page"],
            sys.executable,
            "html",
            False,
            False,
            False,
            {},
            False,
        )
