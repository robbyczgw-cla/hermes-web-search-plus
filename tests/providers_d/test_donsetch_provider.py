from __future__ import annotations

import json
import os
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

    class FakeSession:
        def __init__(self, _binary, _timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def call(self, tool, arguments):
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

    monkeypatch.setitem(module, "DonsetchSession", FakeSession)
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

    class FakeSession:
        def __init__(self, _binary, _timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def call(self, _tool, arguments):
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

    monkeypatch.setitem(module, "DonsetchSession", FakeSession)
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


_FAKE_MCP = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

mode = os.environ.get("DONSETCH_FAKE_MODE", "ok")
state = Path(os.environ["DONSETCH_FAKE_STATE"])
pid_path = state / "pid"
init_path = state / "initialize_count"
call_path = state / "call_count"
pid_path.write_text(str(os.getpid()), encoding="utf-8")

def bump(path):
    current = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    path.write_text(str(current + 1), encoding="utf-8")
    return current + 1

def reply(message, payload):
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], **payload}), flush=True)

def fetch_ok(url):
    return {
        "result": {
            "content": [{"type": "text", "text": f"body:{url}"}],
            "structuredContent": {
                "url": url,
                "status": 200,
                "content_ok": True,
                "content_kind": "Page",
                "verdict": "ContentOk",
                "title": "ok",
            },
        }
    }

if mode == "stderr":
    sys.stderr.write("token=supersecretvalue " + ("x" * 4000) + "\n")
    sys.stderr.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get("method")
    if method == "initialize":
        bump(init_path)
        if mode == "init-error":
            reply(message, {"error": {"code": -32000, "message": "nope"}})
            break
        if mode == "malformed":
            print("{not-json", flush=True)
            continue
        reply(message, {"result": {}})
        if mode == "hang":
            time.sleep(30)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        bump(call_path)
        if mode == "tool-error":
            reply(message, {"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}})
            continue
        if mode == "broken-json":
            print("[[[", flush=True)
            continue
        args = message.get("params", {}).get("arguments", {})
        url = args.get("url") or "https://example.org/search"
        reply(message, fetch_ok(url) if message["params"]["name"] == "web_fetch" else {
            "result": {
                "content": [{"type": "text", "text": "hits"}],
                "structuredContent": {
                    "results": [{"url": url, "title": "hit", "snippet": "s"}],
                    "engines": [],
                    "intent": "auto",
                    "cached": False,
                    "weak": False,
                    "elapsed_ms": 1,
                },
            }
        })
'''


def _write_fake(tmp_path, mode="ok"):
    state = tmp_path / "state"
    state.mkdir()
    binary = tmp_path / "donsetch"
    binary.write_text(_FAKE_MCP, encoding="utf-8")
    binary.chmod(0o700)
    env = {
        "DONSETCH_FAKE_MODE": mode,
        "DONSETCH_FAKE_STATE": str(state),
    }
    return binary, state, env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_multi_url_extract_reuses_one_initialized_mcp_session(tmp_path, monkeypatch):
    spec, _module = _provider()
    binary, state, env = _write_fake(tmp_path)
    monkeypatch.setenv("DONSETCH_FAKE_MODE", env["DONSETCH_FAKE_MODE"])
    monkeypatch.setenv("DONSETCH_FAKE_STATE", env["DONSETCH_FAKE_STATE"])
    result = spec.execute_extract(
        None,
        "donsetch",
        ["https://example.org/a", "https://example.org/b", "https://example.org/c"],
        str(binary),
        "markdown",
        False,
        False,
        False,
        {"donsetch": {"timeout": 5}},
        False,
    )
    assert (state / "initialize_count").read_text(encoding="utf-8") == "1"
    assert (state / "call_count").read_text(encoding="utf-8") == "3"
    assert [item["url"] for item in result["results"]] == [
        "https://example.org/a",
        "https://example.org/b",
        "https://example.org/c",
    ]
    pid = int((state / "pid").read_text(encoding="utf-8"))
    assert _alive(pid) is False


def test_session_closes_after_successful_search(tmp_path, monkeypatch):
    spec, _module = _provider()
    binary, state, env = _write_fake(tmp_path)
    monkeypatch.setenv("DONSETCH_FAKE_MODE", env["DONSETCH_FAKE_MODE"])
    monkeypatch.setenv("DONSETCH_FAKE_STATE", env["DONSETCH_FAKE_STATE"])
    result = spec.execute_search(
        None,
        "donsetch",
        _search_args(),
        str(binary),
        {"donsetch": {"timeout": 5}},
        {},
    )
    assert result["provider"] == "donsetch"
    assert (state / "initialize_count").read_text(encoding="utf-8") == "1"
    assert (state / "call_count").read_text(encoding="utf-8") == "1"
    pid = int((state / "pid").read_text(encoding="utf-8"))
    assert _alive(pid) is False


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("hang", "donsetch_timeout"),
        ("malformed", "donsetch_timeout|donsetch_mcp_failed|donsetch_mcp_contract_failed"),
        ("init-error", "donsetch_mcp_initialize_failed"),
        ("tool-error", "donsetch_tool_error"),
        ("broken-json", "donsetch_mcp_failed|donsetch_mcp_contract_failed|donsetch_timeout"),
    ],
)
def test_failed_mcp_paths_raise_typed_errors_and_reap_child(tmp_path, monkeypatch, mode, error):
    spec, _module = _provider()
    binary, state, env = _write_fake(tmp_path, mode=mode)
    monkeypatch.setenv("DONSETCH_FAKE_MODE", mode)
    monkeypatch.setenv("DONSETCH_FAKE_STATE", env["DONSETCH_FAKE_STATE"])
    with pytest.raises(RuntimeError, match=error):
        spec.execute_extract(
            None,
            "donsetch",
            ["https://example.org/page"],
            str(binary),
            "markdown",
            False,
            False,
            False,
            {"donsetch": {"timeout": 1}},
            False,
        )
    pid = int((state / "pid").read_text(encoding="utf-8"))
    assert _alive(pid) is False


def test_stderr_capture_is_bounded_sanitized_and_absent_from_success(tmp_path, monkeypatch):
    spec, module = _provider()
    binary, state, env = _write_fake(tmp_path, mode="stderr")
    monkeypatch.setenv("DONSETCH_FAKE_MODE", "stderr")
    monkeypatch.setenv("DONSETCH_FAKE_STATE", env["DONSETCH_FAKE_STATE"])
    result = spec.execute_search(
        None,
        "donsetch",
        _search_args(),
        str(binary),
        {"donsetch": {"timeout": 5}},
        {},
    )
    dumped = json.dumps(result)
    assert "supersecretvalue" not in dumped
    excerpt = module["sanitize_stderr"](module["_last_stderr_excerpt"]())
    assert len(excerpt) <= module["STDERR_LIMIT"] + 1
    assert "supersecretvalue" not in excerpt
    assert "token=[redacted]" in excerpt


def test_version_detection_classifies_missing_tested_compatible_and_incompatible(tmp_path):
    _spec, module = _provider()
    missing = module["inspect_donsetch_readiness"](binary="")
    assert missing["state"] == "missing"

    blocked = tmp_path / "blocked"
    blocked.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    blocked.chmod(0o600)
    not_exec = module["inspect_donsetch_readiness"](binary=str(blocked))
    assert not_exec["state"] == "not_executable"

    def _version_bin(text: str):
        path = tmp_path / f"bin-{text.replace('.', '-')}"
        path.write_text(f"#!/bin/sh\necho 'DonSeTch {text}'\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    tested = module["inspect_donsetch_readiness"](binary=_version_bin("2.3.1"))
    assert tested["state"] == "executable"
    assert tested["version"] == "2.3.1"
    assert tested["compatibility"] == "tested"
    assert tested["tested_version"] == "2.3.1"
    assert "api_key" not in tested

    other = module["inspect_donsetch_readiness"](binary=_version_bin("2.1.0"))
    assert other["compatibility"] == "compatible_unverified"
    assert other["version"] == "2.1.0"

    major = module["inspect_donsetch_readiness"](binary=_version_bin("3.0.0"))
    assert major["compatibility"] == "incompatible_major"
    assert major["state"] == "executable"
