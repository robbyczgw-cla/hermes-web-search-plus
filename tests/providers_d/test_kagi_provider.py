"""Contract tests for the zero-core-edit Kagi provider.

Kagi's HTTP surface is mocked here: no test performs a network request and no
live key is required.  The suite pins the provider's registration metadata, the
request it projects from the shared search arguments, the source-only envelope
it returns, and the typed errors it raises for upstream and configuration
failures.
"""

from __future__ import annotations

import json
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from wsp_core import provider_registry
from wsp_core.provider_adapter_protocol import validate_adapter_result
from wsp_sdk import ProviderConfigError, ProviderRequestError

_API_URL = "https://kagi.com/api/v1/search"


class FakeResponse:
    def __init__(self, payload=None, *, raw: bytes | None = None):
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._raw if size < 0 else self._raw[:size]


def _provider_globals():
    spec = provider_registry.PROVIDER_SPECS["kagi"]
    return spec, spec.execute_search.__globals__


def _args(**overrides):
    values = {
        "query": "kagi contract query",
        "max_results": 3,
        "freshness": None,
        "time_range": None,
        "include_domains": None,
        "exclude_domains": None,
        "search_type": "search",
        "country": None,
        "raw_content": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _search(module, payload, *, key="kagi-test-key", config=None, args=None, timeout=11):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(payload)

    module["urlopen"] = fake_urlopen
    try:
        spec = provider_registry.PROVIDER_SPECS["kagi"]
        result = spec.execute_search(
            None,
            "kagi",
            args if args is not None else _args(),
            key,
            {"kagi": {"timeout": timeout}} if config is None else config,
            {},
        )
    finally:
        module.pop("urlopen", None)
    return result, captured


def test_kagi_registers_as_explicit_only_search_provider():
    spec = provider_registry.PROVIDER_SPECS["kagi"]

    assert spec.kind == "search"
    assert spec.env_var == "KAGI_API_KEY"
    assert spec.display_name == "Kagi Search"
    assert spec.config_section == "kagi"
    assert spec.keyless is False
    assert spec.auto_allowed_by_default is False
    # reserved for the starter preset (brave, serper, exa, linkup)
    assert spec.recommended is False
    assert spec.supports_freshness is True
    assert spec.production is True
    assert spec.capability_labels == ("search", "news", "freshness", "privacy")
    assert spec.upstream_capabilities == (
        "search",
        "news",
        "freshness",
        "domain-filtering",
        "extract-inline",
    )
    assert spec.free_tier == "No free tier; Kagi API credits required"
    assert spec.signup_url == "https://kagi.com/api"
    assert spec.execute_search is not None
    assert spec.execute_extract is None
    assert "kagi" in provider_registry.SEARCH_PROVIDER_IDS
    assert "kagi" not in provider_registry.EXTRACT_PROVIDER_IDS
    assert "kagi" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["kagi"] is False


def test_kagi_projects_request_and_source_only_response():
    spec, module = _provider_globals()

    payload = {
        "data": {
            "search": [
                {
                    "url": "https://a.example/one",
                    "title": "A one",
                    "snippet": "snip A",
                    "time": "2026-01-02T03:04:05Z",
                    "image": {"url": "https://a.example/i.png"},
                    "props": {"language": "en"},
                },
                {"title": "Missing URL", "snippet": "must be dropped"},
                "not-a-mapping",
                {"url": "ftp://bad.example/x", "title": "Wrong scheme"},
            ],
            "news": [
                {
                    "url": "http://news.example/two",
                    "title": "N two",
                    "snippet": "snip N",
                    "props": {"language": "fr"},
                }
            ],
            "interesting_finds": [],
        },
        "meta": {"ms": 123, "trace": "trace-safe-id"},
    }

    result, captured = _search(module, payload, args=_args(max_results=5))

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == _API_URL
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bot kagi-test-key"
    assert request.headers["Content-type"] == "application/json"
    assert captured["timeout"] == 11
    assert body == {
        "query": "kagi contract query",
        "limit": 5,
        "workflow": "search",
    }

    assert result == {
        "provider": "kagi",
        "query": "kagi contract query",
        "results": [
            {
                "url": "https://a.example/one",
                "title": "A one",
                "snippet": "snip A",
                "date": "2026-01-02T03:04:05Z",
                "thumbnail": "https://a.example/i.png",
                "language": "en",
            },
            {
                "url": "http://news.example/two",
                "title": "N two",
                "snippet": "snip N",
                "language": "fr",
            },
        ],
        "images": [],
        "metadata": {"latency_ms": 123, "trace": "trace-safe-id"},
    }
    assert "answer" not in result
    assert "summary" not in result
    assert validate_adapter_result("kagi", "search", result) == result


def test_kagi_maps_freshness_region_and_domain_filters():
    _spec, module = _provider_globals()

    result, captured = _search(
        module,
        {"data": {"search": []}},
        args=_args(
            freshness="week",
            country="de",
            include_domains=["docs.python.org", "  example.com  "],
            exclude_domains=["spam.example"],
        ),
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert body["filters"] == {"time_relative": "week", "region": "DE"}
    assert body["lens"] == {
        "sites_included": ["docs.python.org", "example.com"],
        "sites_excluded": ["spam.example"],
    }
    assert validate_adapter_result("kagi", "search", result) == result


def test_kagi_omits_filters_it_cannot_honour():
    _spec, module = _provider_globals()

    _result, captured = _search(
        module,
        {"data": {"search": []}},
        args=_args(freshness="hour", include_domains=["only.example"]),
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert "filters" not in body
    assert body["lens"] == {"sites_included": ["only.example"]}


def test_kagi_uses_news_workflow_and_inline_extract_on_request():
    _spec, module = _provider_globals()

    _result, captured = _search(
        module,
        {"data": {"search": []}},
        args=_args(search_type="news", raw_content=True, max_results=25),
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert body["workflow"] == "news"
    assert body["extract"] == {"count": 10}


@pytest.mark.parametrize("requested,expected", [(None, 10), (0, 10), (3, 3), (999, 50)])
def test_kagi_clamps_requested_result_count(requested, expected):
    _spec, module = _provider_globals()

    _result, captured = _search(module, {"data": {"search": []}}, args=_args(max_results=requested))

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert body["limit"] == expected


def test_kagi_honours_safe_search_configuration():
    _spec, module = _provider_globals()

    _result, captured = _search(
        module,
        {"data": {"search": []}},
        config={"kagi": {"timeout": 11, "safe_search": False}},
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert body["safe_search"] is False


@pytest.mark.parametrize("key", ["", "   ", None])
def test_kagi_requires_a_key_before_any_request(key):
    spec, _module = _provider_globals()

    with pytest.raises(ProviderConfigError) as caught:
        spec.execute_search(None, "kagi", _args(), key, {}, {})

    assert "kagi_api_key_required" in str(caught.value)


@pytest.mark.parametrize("configured", [0, 121, "soon", None])
def test_kagi_rejects_an_unusable_timeout(configured):
    _spec, module = _provider_globals()

    with pytest.raises(ProviderConfigError) as caught:
        _search(module, {"data": {"search": []}}, config={"kagi": {"timeout": configured}})

    assert "kagi_timeout_invalid" in str(caught.value)


def test_kagi_treats_an_invalid_key_as_a_configuration_error():
    _spec, module = _provider_globals()

    def fake_urlopen(request, timeout):
        raise HTTPError(_API_URL, 401, "Unauthorized", Message(), None)

    module["urlopen"] = fake_urlopen
    try:
        with pytest.raises(ProviderConfigError) as caught:
            provider_registry.PROVIDER_SPECS["kagi"].execute_search(
                None, "kagi", _args(), "kagi-test-key", {}, {}
            )
    finally:
        module.pop("urlopen", None)

    assert "kagi_api_key_invalid" in str(caught.value)


@pytest.mark.parametrize(
    "upstream_status,transient",
    [(429, True), (500, True), (502, True), (503, True), (504, True), (400, False), (403, False)],
)
def test_kagi_classifies_http_failures_without_leaking_upstream_text(upstream_status, transient):
    _spec, module = _provider_globals()

    def fake_urlopen(request, timeout):
        raise HTTPError(_API_URL, upstream_status, "secret upstream detail", Message(), None)

    module["urlopen"] = fake_urlopen
    try:
        with pytest.raises(ProviderRequestError) as caught:
            provider_registry.PROVIDER_SPECS["kagi"].execute_search(
                None, "kagi", _args(), "kagi-test-key", {}, {}
            )
    finally:
        module.pop("urlopen", None)

    assert caught.value.status_code == upstream_status
    assert caught.value.transient is transient
    assert "secret upstream detail" not in str(caught.value)


@pytest.mark.parametrize(
    "failure",
    [URLError("dns failure"), TimeoutError("slow"), OSError("socket closed")],
)
def test_kagi_reports_transport_failures_as_temporarily_unavailable(failure):
    _spec, module = _provider_globals()

    def fake_urlopen(request, timeout):
        raise failure

    module["urlopen"] = fake_urlopen
    try:
        with pytest.raises(ProviderRequestError) as caught:
            provider_registry.PROVIDER_SPECS["kagi"].execute_search(
                None, "kagi", _args(), "kagi-test-key", {}, {}
            )
    finally:
        module.pop("urlopen", None)

    assert "kagi_unavailable" in str(caught.value)
    assert caught.value.transient is True


def test_kagi_rejects_an_oversized_body():
    _spec, module = _provider_globals()
    module["urlopen"] = lambda request, timeout: FakeResponse(
        None, raw=b"x" * (8 * 1024 * 1024 + 2)
    )
    try:
        with pytest.raises(ProviderRequestError) as caught:
            provider_registry.PROVIDER_SPECS["kagi"].execute_search(
                None, "kagi", _args(), "kagi-test-key", {}, {}
            )
    finally:
        module.pop("urlopen", None)

    assert "kagi_response_too_large" in str(caught.value)
    assert caught.value.transient is True


@pytest.mark.parametrize("body", [b"{not json", b"\xff\xfe\x00", b'"a string"'])
def test_kagi_rejects_a_non_object_payload(body):
    _spec, module = _provider_globals()
    module["urlopen"] = lambda request, timeout: FakeResponse(None, raw=body)
    try:
        with pytest.raises(ProviderRequestError) as caught:
            provider_registry.PROVIDER_SPECS["kagi"].execute_search(
                None, "kagi", _args(), "kagi-test-key", {}, {}
            )
    finally:
        module.pop("urlopen", None)

    assert "kagi_invalid_response" in str(caught.value)


@pytest.mark.parametrize(
    "error_item,expected_code,config_error",
    [
        ({"code": 2, "message": "secret key detail"}, "kagi_api_key_invalid", True),
        ({"code": 5, "message": "secret quota detail"}, "kagi_api_error_5", False),
    ],
)
def test_kagi_maps_api_error_envelopes(error_item, expected_code, config_error):
    _spec, module = _provider_globals()
    payload = {"error": [error_item]}
    module["urlopen"] = lambda request, timeout: FakeResponse(payload)
    try:
        with pytest.raises((ProviderConfigError, ProviderRequestError)) as caught:
            provider_registry.PROVIDER_SPECS["kagi"].execute_search(
                None, "kagi", _args(), "kagi-test-key", {}, {}
            )
    finally:
        module.pop("urlopen", None)

    assert expected_code in str(caught.value)
    assert "secret" not in str(caught.value)
    if not config_error:
        assert caught.value.status_code == 400
        assert caught.value.transient is False


def test_kagi_returns_an_empty_envelope_when_kagi_has_no_results():
    spec, module = _provider_globals()

    result, _captured = _search(module, {"data": {"search": []}, "meta": {}})

    assert result == {
        "provider": "kagi",
        "query": "kagi contract query",
        "results": [],
        "images": [],
        "metadata": {},
    }


def test_kagi_ignores_malformed_metadata_and_buckets():
    _spec, module = _provider_globals()

    result, _captured = _search(
        module,
        {
            "data": {"search": "not-a-list", "news": None, "interesting_finds": {"a": 1}},
            "meta": {"ms": "fast", "trace": 7},
        },
    )

    assert result["results"] == []
    assert result["metadata"] == {}
