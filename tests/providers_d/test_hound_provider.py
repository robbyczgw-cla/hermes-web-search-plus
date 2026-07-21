from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import extract
import provider_registry
from contract_v3 import Capability, RequestV3


def _provider_globals():
    spec = provider_registry.PROVIDER_SPECS["hound"]
    return spec, spec.execute_search.__globals__


def test_hound_registers_as_explicit_only_search_and_extract_provider():
    spec = provider_registry.PROVIDER_SPECS["hound"]

    assert spec.kind == "both"
    assert spec.env_var == "HOUND_MCP_URL"
    assert spec.keyless is False
    assert spec.auto_allowed_by_default is False
    assert spec.supports_freshness is True
    assert "hound" in provider_registry.SEARCH_PROVIDER_IDS
    assert "hound" in provider_registry.EXTRACT_PROVIDER_IDS
    assert "hound" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["hound"] is False


def test_hound_extract_is_excluded_from_auto_and_fallback_unless_opted_in(monkeypatch):
    monkeypatch.setattr(
        extract,
        "get_api_key",
        lambda provider, *_args, **_kwargs: (
            "configured" if provider == "hound" else None
        ),
    )
    monkeypatch.setattr(extract, "keyless_public_allowed", lambda *_args, **_kwargs: False)

    auto_request = RequestV3(
        capability=Capability.EXTRACT,
        input={"urls": ["https://example.test/a"]},
        routing={"provider": "auto"},
    )
    explicit_request = RequestV3(
        capability=Capability.EXTRACT,
        input={"urls": ["https://example.test/a"]},
        routing={"provider": "hound"},
    )

    assert "hound" not in extract._plan_extract_v3(auto_request, {}).candidate_order
    explicit_plan = extract._plan_extract_v3(explicit_request, {})
    assert explicit_plan.selected_provider == "hound"
    assert explicit_plan.candidate_order[0] == "hound"

    hound_first = {
        "auto_routing": {
            "extract_provider_priority": ["hound", "tavily"],
            "auto_allow": {"hound": False},
        }
    }
    assert extract._plan_extract_v3(auto_request, hound_first).candidate_order == (
        "tavily",
    )

    hound_calls = []
    monkeypatch.setattr(extract, "_validate_extract_urls", lambda urls, _config: urls)
    monkeypatch.setitem(
        extract.EXTRACT_DISPATCH,
        "hound",
        lambda *_args, **_kwargs: hound_calls.append(True),
    )
    extract._extract_plus_core(
        ["https://example.test/a"],
        provider="auto",
        config=hound_first,
    )
    assert hound_calls == []

    opted_in = {"auto_routing": {"auto_allow": {"hound": True}}}
    assert "hound" in extract._plan_extract_v3(auto_request, opted_in).candidate_order


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/mcp",
        "http://[::1]:8765/mcp",
    ],
)
def test_hound_endpoint_accepts_loopback_only(url):
    _spec, module = _provider_globals()
    assert module["_validate_endpoint"](url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765/mcp",
        "http://localhost:8765/mcp",
        "http://example.com:8765/mcp",
        "http://10.0.0.5:8765/mcp",
        "http://127.0.0.1:8765/other",
        "http://user:pass@127.0.0.1:8765/mcp",
        "http://127.0.0.1:8765/mcp?token=secret",
        "http://127.0.0.1:8765/mcp#fragment",
        "http://127.0.0.1:8765/mcp?",
        "http://127.0.0.1:8765/mcp#",
    ],
)
def test_hound_endpoint_rejects_non_loopback_or_ambiguous_urls(url):
    _spec, module = _provider_globals()
    with pytest.raises(ValueError, match="hound_endpoint_invalid"):
        module["_validate_endpoint"](url)


def test_hound_mcp_transport_disables_redirects_and_environment_proxies(monkeypatch):
    _spec, module = _provider_globals()
    import httpx
    import mcp.client.streamable_http as streamable_http

    captured = {}

    class ProbeStopped(Exception):
        pass

    class ProbeHttpClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class ProbeContext:
        async def __aenter__(self):
            raise ProbeStopped

        async def __aexit__(self, *_args):
            return False

    def fake_streamable_http_client(
        endpoint, *, http_client, terminate_on_close
    ):
        captured.update(
            endpoint=endpoint,
            http_client=http_client,
            terminate_on_close=terminate_on_close,
        )
        return ProbeContext()

    monkeypatch.setattr(httpx, "AsyncClient", ProbeHttpClient)
    monkeypatch.setattr(
        streamable_http,
        "streamable_http_client",
        fake_streamable_http_client,
    )

    with pytest.raises(ProbeStopped):
        asyncio.run(
            module["_call_hound_tool_async"](
                "http://127.0.0.1:8765/mcp",
                "mcp_smart_search",
                {},
                30,
            )
        )

    assert captured["endpoint"] == "http://127.0.0.1:8765/mcp"
    assert captured["client_kwargs"]["follow_redirects"] is False
    assert captured["client_kwargs"]["trust_env"] is False
    assert captured["terminate_on_close"] is True


def test_hound_call_sanitizes_ordinary_transport_and_protocol_errors(monkeypatch):
    _spec, module = _provider_globals()

    def raise_protocol_error(_factory):
        raise ValueError("upstream protocol detail must not escape")

    monkeypatch.setitem(module, "_run_async", raise_protocol_error)

    with pytest.raises(RuntimeError, match="^hound_mcp_unavailable$"):
        module["_call_hound_tool"](
            "http://127.0.0.1:8765/mcp", "mcp_smart_search", {}, 30
        )

    def raise_interrupt(_factory):
        raise KeyboardInterrupt

    monkeypatch.setitem(module, "_run_async", raise_interrupt)
    with pytest.raises(KeyboardInterrupt):
        module["_call_hound_tool"](
            "http://127.0.0.1:8765/mcp", "mcp_smart_search", {}, 30
        )


def test_hound_search_projects_source_results_and_disables_hound_cache(monkeypatch):
    spec, module = _provider_globals()
    calls = []

    def fake_call(endpoint, tool, arguments, timeout_seconds):
        calls.append((endpoint, tool, arguments, timeout_seconds))
        return {
            "query": "query",
            "results": [
                {
                    "url": "https://docs.example.test/a",
                    "title": "A",
                    "snippet": "Evidence",
                    "source": "brave,startpage",
                    "position": 1,
                    "relevance_score": 0.91,
                    "fetch_relevance": "high",
                    "engines_consensus": "2 of 3",
                },
                {
                    "url": "https://blocked.docs.example.test/b",
                    "title": "Blocked",
                    "snippet": "Must be excluded locally",
                },
                {
                    "url": "https://outside.example.org/c",
                    "title": "Outside",
                    "snippet": "Must fail include-domain filtering",
                },
            ],
            "engines_used": ["brave", "startpage"],
            "engine_blocked": ["qwant"],
            "rerank_mode": "neural",
            "cached": False,
            "duration_ms": 123.0,
            "error": "",
        }

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    args = SimpleNamespace(
        query="query",
        max_results=5,
        freshness="week",
        time_range=None,
        include_domains=["docs.example.test"],
        exclude_domains=["blocked.docs.example.test"],
        country="de",
        language="de",
        search_type="search",
    )

    result = spec.execute_search(
        None,
        "hound",
        args,
        "http://127.0.0.1:8765/mcp",
        {"hound": {"timeout": 45}},
        {},
    )

    assert result["provider"] == "hound"
    assert result["query"] == "query"
    assert result["results"] == [
        {
            "url": "https://docs.example.test/a",
            "title": "A",
            "snippet": "Evidence",
            "score": 0.91,
            "position": 1,
            "source": "brave,startpage",
            "fetch_relevance": "high",
            "engines_consensus": "2 of 3",
        }
    ]
    assert result["metadata"] == {
        "engines_used": ["brave", "startpage"],
        "engine_blocked": ["qwant"],
        "rerank_mode": "neural",
        "duration_ms": 123.0,
    }
    endpoint, tool, arguments, timeout = calls[0]
    assert endpoint == "http://127.0.0.1:8765/mcp"
    assert tool == "mcp_smart_search"
    assert arguments["options"]["cache_ttl"] == 0
    assert arguments["options"]["max_results"] == 5
    assert arguments["options"]["freshness"] == "week"
    assert arguments["options"]["site"] == "docs.example.test"
    assert arguments["options"]["exclude_sites"] == ["blocked.docs.example.test"]
    assert arguments["options"]["language"] == "de"
    assert arguments["options"]["region"] == "de-de"
    assert timeout == 45


def test_hound_search_sanitizes_malformed_numeric_metadata(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_search is not None
    monkeypatch.setitem(
        module,
        "_call_hound_tool",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "url": "https://example.test/a",
                    "position": "not-an-int",
                    "relevance_score": {"not": "a-number"},
                }
            ],
            "duration_ms": "not-a-float",
        },
    )
    args = SimpleNamespace(
        query="query",
        max_results=5,
        freshness=None,
        time_range=None,
        include_domains=None,
        exclude_domains=None,
        country=None,
        language=None,
        search_type="search",
    )

    result = spec.execute_search(
        None,
        "hound",
        args,
        "http://127.0.0.1:8765/mcp",
        {},
        {},
    )

    assert result["results"][0]["score"] == 0.0
    assert result["results"][0]["position"] == 0
    assert result["metadata"]["duration_ms"] == 0.0


def test_hound_search_uses_stable_failure_code(monkeypatch):
    spec, module = _provider_globals()
    monkeypatch.setitem(
        module,
        "_call_hound_tool",
        lambda *_args, **_kwargs: {"results": [], "error": "upstream secret text"},
    )
    args = SimpleNamespace(
        query="query",
        max_results=5,
        freshness=None,
        time_range=None,
        include_domains=None,
        exclude_domains=None,
        country=None,
        language=None,
        search_type="search",
    )

    with pytest.raises(RuntimeError, match="^hound_search_failed$"):
        spec.execute_search(
            None,
            "hound",
            args,
            "http://127.0.0.1:8765/mcp",
            {},
            {},
        )


def test_hound_search_rejects_non_web_vertical_before_network(monkeypatch):
    spec, module = _provider_globals()
    called = False

    def fake_call(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    args = SimpleNamespace(
        query="query",
        max_results=5,
        freshness=None,
        time_range=None,
        include_domains=None,
        exclude_domains=None,
        country=None,
        language=None,
        search_type="news",
    )

    with pytest.raises(RuntimeError, match="^hound_search_type_unsupported$"):
        spec.execute_search(
            None,
            "hound",
            args,
            "http://127.0.0.1:8765/mcp",
            {},
            {},
        )
    assert called is False


def test_hound_extract_projects_bulk_content_and_rendering_options(monkeypatch):
    spec, module = _provider_globals()
    calls = []

    def fake_call(endpoint, tool, arguments, timeout_seconds):
        calls.append((endpoint, tool, arguments, timeout_seconds))
        requested_url = arguments["urls"][0]
        return {
            "results": [
                (
                    {
                        "url": "https://example.test/a",
                        "status": 200,
                        "content_ok": True,
                        "content": ["# A", "Evidence"],
                        "metadata": {"title": "A"},
                        "media": ["https://example.test/a.png"],
                        "fetcher_used": "stealthy",
                        "page_type": "article",
                        "source_type": "official-docs",
                        "is_official": True,
                        "fetched_at": "2026-07-21T00:00:00Z",
                        "cached": False,
                        "duration_ms": 500.0,
                        "error": "",
                    }
                    if requested_url == "https://example.test/a"
                    else {
                        "url": "https://example.test/b",
                        "status": 403,
                        "content_ok": False,
                        "content": [],
                        "error": "sensitive upstream detail",
                    }
                )
            ]
        }

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    result = spec.execute_extract(
        None,
        "hound",
        ["https://example.test/a", "https://example.test/b"],
        "http://127.0.0.1:8765/mcp",
        "markdown",
        True,
        False,
        True,
        {"hound": {"timeout": 90, "max_content_chars": 20000}},
        False,
    )

    assert result["provider"] == "hound"
    assert result["results"][0] == {
        "url": "https://example.test/a",
        "title": "A",
        "content": "# A\nEvidence",
        "images": ["https://example.test/a.png"],
        "status": 200,
        "fetcher": "stealthy",
        "page_type": "article",
        "source_type": "official-docs",
        "is_official": True,
        "fetched_at": "2026-07-21T00:00:00Z",
        "duration_ms": 500.0,
    }
    assert result["results"][1] == {
        "url": "https://example.test/b",
        "error": "hound_fetch_failed",
        "status": 403,
    }
    endpoint, tool, arguments, timeout = calls[0]
    assert endpoint == "http://127.0.0.1:8765/mcp"
    assert tool == "mcp_smart_fetch"
    assert [call[2]["urls"] for call in calls] == [
        ["https://example.test/a"],
        ["https://example.test/b"],
    ]
    assert arguments["cache_ttl"] == 0
    assert arguments["force_fetcher"] == "stealthy"
    assert arguments["max_content_chars"] == 20000
    assert arguments["options"]["include_media"] is True
    assert timeout == 90


def test_hound_extract_never_promotes_empty_content_and_preserves_url_cardinality(monkeypatch):
    spec, module = _provider_globals()
    def fake_call(_endpoint, _tool, arguments, _timeout_seconds):
        requested_url = arguments["urls"][0]
        return {
            "results": [
                {
                    "url": requested_url,
                    "status": 200,
                    "content_ok": True,
                    "content": [],
                    "metadata": {},
                    "media": [],
                    "error": "",
                }
            ]
        }

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)

    result = spec.execute_extract(
        None,
        "hound",
        ["https://example.test/a", "https://example.test/b"],
        "http://127.0.0.1:8765/mcp",
        "markdown",
        False,
        False,
        False,
        {},
        False,
    )

    assert result["results"] == [
        {"url": "https://example.test/a", "error": "hound_fetch_failed", "status": 200},
        {"url": "https://example.test/b", "error": "hound_fetch_failed", "status": 200},
    ]


def test_hound_extract_uses_single_requests_and_fails_closed_for_bad_bulk_shapes(monkeypatch):
    spec, module = _provider_globals()
    calls = []

    def item(url, content):
        return {
            "url": url,
            "status": 200,
            "content_ok": True,
            "content": [content],
            "metadata": {},
            "media": [],
            "error": "",
        }

    def fake_call(_endpoint, _tool, arguments, _timeout_seconds):
        requested_url = arguments["urls"][0]
        extraction_type = arguments["extraction_type"]
        calls.append((requested_url, extraction_type, list(arguments["urls"])))
        if requested_url == "https://example.test/missing":
            return {"results": []}
        if (
            requested_url == "https://example.test/b"
            and extraction_type == "html"
        ):
            # A malformed/reordered multi-item response must not be attached by
            # index to this request's primary content.
            return {
                "results": [
                    item("https://elsewhere.test/first", "wrong raw content"),
                    item("https://example.test/b", "right raw content"),
                ]
            }
        suffix = "raw" if extraction_type == "html" else "primary"
        final_url = requested_url.replace("example.test", "redirected.example.test")
        return {"results": [item(final_url, f"{suffix} {requested_url}")]}

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    result = spec.execute_extract(
        None,
        "hound",
        [
            "https://example.test/a",
            "https://example.test/b",
            "https://example.test/a",
            "https://example.test/missing",
        ],
        "http://127.0.0.1:8765/mcp",
        "markdown",
        False,
        True,
        False,
        {},
        False,
    )

    assert calls == [
        ("https://example.test/a", "markdown", ["https://example.test/a"]),
        ("https://example.test/b", "markdown", ["https://example.test/b"]),
        ("https://example.test/a", "markdown", ["https://example.test/a"]),
        ("https://example.test/missing", "markdown", ["https://example.test/missing"]),
        ("https://example.test/a", "html", ["https://example.test/a"]),
        ("https://example.test/b", "html", ["https://example.test/b"]),
        ("https://example.test/a", "html", ["https://example.test/a"]),
    ]
    results = result["results"]
    assert [item["url"] for item in results] == [
        "https://redirected.example.test/a",
        "https://redirected.example.test/b",
        "https://redirected.example.test/a",
        "https://example.test/missing",
    ]
    assert [item.get("content") for item in results] == [
        "primary https://example.test/a",
        "primary https://example.test/b",
        "primary https://example.test/a",
        None,
    ]
    assert [item.get("raw_content") for item in results] == [
        "raw https://example.test/a",
        None,
        "raw https://example.test/a",
        None,
    ]
    assert [item.get("raw_error") for item in results] == [
        None,
        "hound_raw_html_failed",
        None,
        None,
    ]
    assert results[-1] == {
        "url": "https://example.test/missing",
        "error": "hound_fetch_failed",
        "status": 0,
    }


def test_hound_extract_fetches_raw_html_only_when_requested(monkeypatch):
    spec, module = _provider_globals()
    calls = []

    def fake_call(_endpoint, _tool, arguments, _timeout_seconds):
        calls.append(arguments)
        extraction_type = arguments["extraction_type"]
        content = ["<main>raw</main>"] if extraction_type == "html" else ["clean"]
        return {
            "results": [
                {
                    "url": "https://example.test/a",
                    "status": 200,
                    "content_ok": True,
                    "content": content,
                    "metadata": {},
                    "media": [],
                    "error": "",
                }
            ]
        }

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    result = spec.execute_extract(
        None,
        "hound",
        ["https://example.test/a"],
        "http://127.0.0.1:8765/mcp",
        "markdown",
        False,
        True,
        False,
        {},
        False,
    )

    assert [call["extraction_type"] for call in calls] == ["markdown", "html"]
    assert result["results"][0]["content"] == "clean"
    assert result["results"][0]["raw_content"] == "<main>raw</main>"


def test_hound_extract_marks_empty_requested_raw_html_as_failed(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_extract is not None

    def fake_call(_endpoint, _tool, arguments, _timeout_seconds):
        extraction_type = arguments["extraction_type"]
        return {
            "results": [
                {
                    "url": "https://example.test/a",
                    "status": 200,
                    "content_ok": True,
                    "content": [] if extraction_type == "html" else ["clean"],
                    "metadata": {},
                    "media": [],
                    "error": "",
                }
            ]
        }

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    result = spec.execute_extract(
        None,
        "hound",
        ["https://example.test/a"],
        "http://127.0.0.1:8765/mcp",
        "markdown",
        False,
        True,
        False,
        {},
        False,
    )

    assert result["results"][0]["content"] == "clean"
    assert result["results"][0]["raw_error"] == "hound_raw_html_failed"
    assert "raw_content" not in result["results"][0]


def test_hound_extract_keeps_primary_content_when_raw_html_call_raises(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_extract is not None

    def fake_call(_endpoint, _tool, arguments, _timeout_seconds):
        if arguments["extraction_type"] == "html":
            raise RuntimeError("hound_mcp_unavailable")
        return {
            "results": [
                {
                    "url": "https://example.test/a",
                    "status": 200,
                    "content_ok": True,
                    "content": ["clean"],
                    "metadata": {},
                    "media": [],
                    "error": "",
                }
            ]
        }

    monkeypatch.setitem(module, "_call_hound_tool", fake_call)
    result = spec.execute_extract(
        None,
        "hound",
        ["https://example.test/a"],
        "http://127.0.0.1:8765/mcp",
        "markdown",
        False,
        True,
        False,
        {},
        False,
    )

    assert result["results"][0]["content"] == "clean"
    assert result["results"][0]["raw_error"] == "hound_raw_html_failed"
    assert "raw_content" not in result["results"][0]
