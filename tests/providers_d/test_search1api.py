from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import provider_registry
import providers
from http_client import ProviderRequestError


class FakeResponse:
    def __init__(self, payload, *, raw: bytes | None = None):
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._raw if size < 0 else self._raw[:size]


def _provider_globals():
    spec = provider_registry.PROVIDER_SPECS["search1api"]
    return spec, spec.execute_search.__globals__


def _args(**overrides):
    values = {
        "query": "search1api contract query",
        "max_results": 3,
        "freshness": "week",
        "time_range": None,
        "include_domains": ["docs.python.org"],
        "exclude_domains": ["spam.example"],
        "search_type": "search",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _search_payload():
    return {
        "searchParameters": {"query": "search1api contract query"},
        "results": [
            {
                "title": "Python docs",
                "link": "https://docs.python.org/3/",
                "snippet": "Official Python documentation.",
                "date": "2026-09-01",
            },
            {"title": "Missing URL", "snippet": "must be dropped"},
            {"title": "Unsafe scheme", "link": "javascript:alert(1)", "snippet": "dropped"},
        ],
    }


def test_search1api_registers_as_explicit_only_search_and_extract_provider():
    spec = provider_registry.PROVIDER_SPECS["search1api"]

    assert spec.kind == "both"
    assert spec.env_var == "SEARCH1API_KEY"
    assert spec.display_name == "Search1API"
    assert spec.keyless is False
    assert spec.auto_allowed_by_default is False
    assert spec.supports_freshness is True
    assert spec.capability_labels == ("search", "news", "extract", "freshness")
    assert spec.execute_search is not None
    assert spec.execute_extract is not None
    assert "search1api" in provider_registry.SEARCH_PROVIDER_IDS
    assert "search1api" in provider_registry.EXTRACT_PROVIDER_IDS
    assert "search1api" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["search1api"] is False


def test_search1api_refuses_redirects_to_keep_credentials_on_the_fixed_origin():
    _spec, module = _provider_globals()
    request = Request("https://api.search1api.com/search")

    redirected = module["_NoRedirectHandler"]().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_search1api_projects_request_and_source_only_response(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(_search_payload())

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_search(
        None,
        "search1api",
        _args(),
        "search1api-test-key",
        {"search1api": {"timeout": 17}},
        {},
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.search1api.com/search"
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer search1api-test-key"
    assert request.headers["Content-type"] == "application/json"
    assert captured["timeout"] == 17
    assert body == {
        "query": "search1api contract query",
        "max_results": 3,
        "search_service": "google",
        "crawl_results": 0,
        "include_sites": ["docs.python.org"],
        "exclude_sites": ["spam.example"],
        "time_range": "week",
    }

    assert result == {
        "provider": "search1api",
        "query": "search1api contract query",
        "results": [
            {
                "url": "https://docs.python.org/3/",
                "title": "Python docs",
                "snippet": "Official Python documentation.",
                "date": "2026-09-01",
            }
        ],
        "images": [],
        "metadata": {"endpoint": "search", "service": "google"},
    }


def test_search1api_maps_news_vertical_to_news_endpoint(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        return FakeResponse({"results": []})

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_search(
        None,
        "search1api",
        _args(query="latest news", search_type="news", freshness=None),
        "key",
        {},
        {},
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert captured["request"].full_url == "https://api.search1api.com/news"
    assert body["search_service"] == "bing"
    assert result["metadata"] == {"endpoint": "news", "service": "bing"}


def test_search1api_news_vertical_is_registered_for_truthful_metadata():
    assert providers.provider_supports_search_type("search1api", "news") is True
    assert providers.search_type_metadata("search1api", "news") == {
        "requested": "news",
        "applied": True,
        "provider": "search1api",
        "native_value": "news",
    }


def test_search1api_honors_service_overrides(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"results": []})

    monkeypatch.setitem(module, "_open_request", fake_open)
    spec.execute_search(
        None,
        "search1api",
        _args(),
        "key",
        {"search1api": {"search_service": "duckduckgo"}},
        {},
    )
    assert captured["body"]["search_service"] == "duckduckgo"

    spec.execute_search(
        None,
        "search1api",
        _args(search_type="news", freshness=None),
        "key",
        {"search1api": {"news_service": "hackernews"}},
        {},
    )
    assert captured["body"]["search_service"] == "hackernews"


@pytest.mark.parametrize(
    "config",
    [
        {"search1api": {"search_service": "not-a-service"}},
        {"search1api": {"news_service": "not-a-service"}},
        {"search1api": {"timeout": "abc"}},
        {"search1api": {"timeout": 0}},
    ],
)
def test_search1api_rejects_invalid_config(monkeypatch, config):
    spec, module = _provider_globals()
    monkeypatch.setitem(
        module, "_open_request", lambda *_args: pytest.fail("network must not run")
    )
    args = _args(search_type="news" if "news_service" in config["search1api"] else "search")
    with pytest.raises(Exception, match="search1api_"):
        spec.execute_search(None, "search1api", args, "key", config, {})


@pytest.mark.parametrize(
    "upstream_status,transient",
    [(400, False), (401, False), (403, False), (429, True), (503, True)],
)
def test_search1api_classifies_upstream_failures(monkeypatch, upstream_status, transient):
    spec, module = _provider_globals()

    def fake_open(request, timeout):
        error = HTTPError(request.full_url, upstream_status, "err", {}, None)
        if upstream_status == 429:
            error.headers = {"Retry-After": "2.5"}
        raise error

    monkeypatch.setitem(module, "_open_request", fake_open)
    with pytest.raises(Exception) as excinfo:
        spec.execute_search(None, "search1api", _args(), "key", {}, {})

    message = str(excinfo.value)
    if upstream_status in {401, 403}:
        assert message == "search1api_key_rejected"
    else:
        assert isinstance(excinfo.value, ProviderRequestError)
        assert excinfo.value.status_code == upstream_status
        assert excinfo.value.transient is transient
        if upstream_status == 429:
            assert excinfo.value.retry_after == 2.5


def test_search1api_requires_key():
    spec, _module = _provider_globals()
    with pytest.raises(Exception, match="search1api_key_required"):
        spec.execute_search(None, "search1api", _args(), "  ", {}, {})
    with pytest.raises(Exception, match="search1api_key_required"):
        spec.execute_extract(None, "search1api", ["https://example.com"], "", "markdown", False, False, False, {}, False)


def test_search1api_extract_projects_page_text(monkeypatch):
    spec, module = _provider_globals()
    calls = []

    def fake_open(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        calls.append(body["url"])
        return FakeResponse(
            {
                "crawlParameters": {"url": body["url"]},
                "results": {
                    "title": "Example",
                    "link": body["url"],
                    "content": "# Example\n\nBody",
                },
            }
        )

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_extract(
        None,
        "search1api",
        ["https://example.com/a", "https://example.com/b"],
        "key",
        "markdown",
        False,
        False,
        False,
        {},
        False,
    )

    assert calls == ["https://example.com/a", "https://example.com/b"]
    assert result["provider"] == "search1api"
    assert result["results"][0] == {
        "url": "https://example.com/a",
        "title": "Example",
        "content": "# Example\n\nBody",
        "images": [],
        "status": 200,
        "fetcher": "search1api",
        "source_type": "web",
    }


def test_search1api_extract_marks_raw_html_as_unsupported(monkeypatch):
    spec, module = _provider_globals()

    def fake_open(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"results": {"title": "ok", "link": body["url"], "content": "text"}})

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_extract(
        None,
        "search1api",
        ["https://ok.example/"],
        "key",
        "markdown",
        False,
        True,
        False,
        {},
        False,
    )

    assert result["results"][0]["content"] == "text"
    assert result["results"][0]["raw_error"] == "search1api_raw_html_unsupported"


def test_search1api_extract_records_per_url_errors(monkeypatch):
    spec, module = _provider_globals()

    def fake_open(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        if "dead" in body["url"]:
            raise HTTPError(request.full_url, 404, "dead", {}, None)
        return FakeResponse({"results": {"title": "ok", "link": body["url"], "content": "text"}})

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_extract(
        None,
        "search1api",
        ["https://dead.example/", "https://ok.example/"],
        "key",
        "markdown",
        False,
        False,
        False,
        {},
        False,
    )

    assert result["results"][0]["error"] == "search1api_http_404"
    assert result["results"][0]["status"] == 404
    assert result["results"][1]["content"] == "text"


def test_search1api_extract_stops_after_transient_failure(monkeypatch):
    spec, module = _provider_globals()
    calls = []

    def fake_open(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8"))["url"])
        raise HTTPError(request.full_url, 503, "down", {}, None)

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_extract(
        None,
        "search1api",
        ["https://a.example/", "https://b.example/"],
        "key",
        "markdown",
        False,
        False,
        False,
        {},
        False,
    )

    assert calls == ["https://a.example/"]
    assert result["results"] == [{"url": "https://a.example/", "error": "search1api_http_503", "status": 503}]


def test_search1api_envelopes_pass_adapter_validation(monkeypatch):
    from provider_adapter_protocol import validate_adapter_result

    spec, module = _provider_globals()
    monkeypatch.setitem(module, "_open_request", lambda *_a: FakeResponse(_search_payload()))
    search = spec.execute_search(None, "search1api", _args(), "key", {}, {})
    assert validate_adapter_result("search1api", "search", search) is search

    monkeypatch.setitem(
        module,
        "_open_request",
        lambda *_a: FakeResponse({"results": {"title": "t", "link": "https://e.example/", "content": "c"}}),
    )
    extract = spec.execute_extract(
        None, "search1api", ["https://e.example/"], "key", "markdown", False, False, False, {}, False
    )
    assert validate_adapter_result("search1api", "extract", extract) is extract


def test_search1api_conformance_clean():
    from wsp_sdk.conformance import provider_conformance_errors

    assert not [e for e in provider_conformance_errors() if e.startswith("search1api:")]
