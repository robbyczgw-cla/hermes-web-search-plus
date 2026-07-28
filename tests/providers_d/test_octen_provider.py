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
    spec = provider_registry.PROVIDER_SPECS["octen"]
    return spec, spec.execute_search.__globals__


def _args(**overrides):
    values = {
        "query": "octen contract query",
        "max_results": 3,
        "freshness": "week",
        "time_range": None,
        "include_domains": ["docs.python.org"],
        "exclude_domains": ["spam.example"],
        "search_type": "search",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_octen_registers_as_explicit_only_search_provider():
    spec = provider_registry.PROVIDER_SPECS["octen"]

    assert spec.kind == "search"
    assert spec.env_var == "MONID_API_KEY"
    assert spec.display_name == "Octen via Monid"
    assert spec.keyless is False
    assert spec.auto_allowed_by_default is False
    assert spec.supports_freshness is True
    assert spec.free_tier == "No free-tier claim; Monid API key and wallet balance required"
    assert spec.capability_labels == ("search", "freshness")
    assert spec.upstream_capabilities == ("search", "highlights", "freshness", "domain-filtering")
    assert spec.execute_search is not None
    assert "octen" in provider_registry.SEARCH_PROVIDER_IDS
    assert "octen" not in provider_registry.EXTRACT_PROVIDER_IDS
    assert "octen" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["octen"] is False


def test_octen_refuses_redirects_to_keep_credentials_on_the_fixed_origin():
    _spec, module = _provider_globals()
    request = Request("https://api.monid.ai/v1/run")

    redirected = module["_NoRedirectHandler"]().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_sdk_freshness_capability_drives_truthful_metadata():
    assert providers.provider_supports_freshness("octen") is True
    assert providers.map_freshness_for_provider("octen", "week") == "week"
    assert providers.freshness_metadata("octen", "week") == {
        "requested": "week",
        "applied": True,
        "provider": "octen",
        "native_value": "week",
    }


def test_octen_projects_request_and_source_only_response(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "runId": "run-safe-id",
                "provider": "octen",
                "endpoint": "/search",
                "status": "COMPLETED",
                "providerResponse": {"httpStatus": 200},
                "billing": {
                    "actualCost": {"value": 1000, "unit": "MICRO_DOLLAR", "currency": "USD"}
                },
                "output": {
                    "code": 0,
                    "msg": "success",
                    "request_id": "req-safe-id",
                    "data": {
                        "query": "octen contract query",
                        "results": [
                            {
                                "title": "Python docs",
                                "url": "https://docs.python.org/3/",
                                "highlight": "Official Python documentation.",
                                "authors": "Python Software Foundation",
                                "time_published": "2026-01-02T03:04:05Z",
                                "time_last_crawled": "2026-07-27T03:04:05Z",
                                "favicon": "https://docs.python.org/favicon.ico",
                            },
                            {"title": "Missing URL", "highlight": "must be dropped"},
                        ],
                    },
                    "meta": {
                        "usage": {"num_search_queries": 1, "full_content_tokens": 0},
                        "latency": 64,
                        "warning": None,
                    },
                },
            }
        )

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_search(
        None,
        "octen",
        _args(),
        "monid-test-key",
        {"octen": {"timeout": 17}},
        {},
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.monid.ai/v1/run"
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer monid-test-key"
    assert request.headers["Content-type"] == "application/json"
    assert captured["timeout"] == 17
    assert body == {
        "provider": "octen",
        "endpoint": "/search",
        "input": {
            "query": "octen contract query",
            "count": 3,
            "topic": "general",
            "include_domains": ["docs.python.org"],
            "exclude_domains": ["spam.example"],
            "time_range": "week",
            "highlight": {"enable": True, "max_tokens": 300},
            "full_content": {"enable": False},
            "format": "text",
        },
    }

    assert result == {
        "provider": "octen",
        "query": "octen contract query",
        "results": [
            {
                "url": "https://docs.python.org/3/",
                "title": "Python docs",
                "snippet": "Official Python documentation.",
                "date": "2026-01-02T03:04:05Z",
                "author": "Python Software Foundation",
                "favicon": "https://docs.python.org/favicon.ico",
                "last_crawled": "2026-07-27T03:04:05Z",
            }
        ],
        "images": [],
        "metadata": {
            "monid_run_id": "run-safe-id",
            "request_id": "req-safe-id",
            "latency_ms": 64,
            "usage": {"search_queries": 1, "full_content_tokens": 0},
            "cost_usd": 0.001,
        },
    }


def test_octen_does_not_claim_news_vertical_support(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "runId": "run-news",
                "provider": "octen",
                "endpoint": "/search",
                "status": "COMPLETED",
                "providerResponse": {"httpStatus": 200},
                "output": {
                    "code": 0,
                    "msg": "success",
                    "data": {"query": "news", "results": []},
                    "meta": {"usage": {}, "latency": 10},
                },
            }
        )

    monkeypatch.setitem(module, "_open_request", fake_open)
    spec.execute_search(None, "octen", _args(query="news", search_type="news"), "key", {}, {})

    assert captured["body"]["input"]["topic"] == "general"


@pytest.mark.parametrize(
    "upstream_status,expected_status,transient",
    [(401, 401, False), (402, 402, False), (403, 403, False), (429, 429, True), (503, 503, True)],
)
def test_octen_classifies_http_failures_without_leaking_upstream_text(
    monkeypatch, upstream_status, expected_status, transient
):
    spec, module = _provider_globals()

    def fake_open(request, timeout):
        raise HTTPError(
            request.full_url,
            upstream_status,
            "secret upstream detail",
            {"Retry-After": "2"},
            None,
        )

    monkeypatch.setitem(module, "_open_request", fake_open)
    with pytest.raises(ProviderRequestError) as caught:
        spec.execute_search(None, "octen", _args(), "key", {}, {})

    assert caught.value.status_code == expected_status
    assert caught.value.transient is transient
    assert caught.value.retry_after == (2.0 if upstream_status == 429 else None)
    assert "secret upstream detail" not in str(caught.value)


@pytest.mark.parametrize(
    "payload,expected_status,transient",
    [
        ({"status": "FAILED", "provider": "octen", "endpoint": "/search"}, 500, True),
        (
            {
                "status": "COMPLETED",
                "provider": "octen",
                "endpoint": "/search",
                "providerResponse": {"httpStatus": 429, "error": {"message": "private rate-limit detail"}},
                "output": None,
            },
            429,
            True,
        ),
        (
            {
                "status": "COMPLETED",
                "provider": "octen",
                "endpoint": "/search",
                "providerResponse": {"httpStatus": 500, "error": {"message": "private backend detail"}},
                "output": None,
            },
            500,
            True,
        ),
    ],
)
def test_octen_classifies_monid_and_provider_failures_without_leaking_message(
    monkeypatch, payload, expected_status, transient
):
    spec, module = _provider_globals()
    monkeypatch.setitem(module, "_open_request", lambda *_args: FakeResponse(payload))

    with pytest.raises(ProviderRequestError) as caught:
        spec.execute_search(None, "octen", _args(), "key", {}, {})

    assert caught.value.status_code == expected_status
    assert caught.value.transient is transient
    assert "private" not in str(caught.value)


def test_octen_rejects_confused_or_async_monid_envelopes(monkeypatch):
    spec, module = _provider_globals()
    bad_payloads = [
        {"status": "RUNNING", "provider": "octen", "endpoint": "/search"},
        {"status": "COMPLETED", "provider": "other", "endpoint": "/search"},
        {"status": "COMPLETED", "provider": "octen", "endpoint": "/other"},
    ]
    for payload in bad_payloads:
        monkeypatch.setitem(module, "_open_request", lambda *_args, p=payload: FakeResponse(p))
        with pytest.raises(ProviderRequestError):
            spec.execute_search(None, "octen", _args(), "key", {}, {})


def test_octen_rejects_invalid_or_oversized_responses(monkeypatch):
    spec, module = _provider_globals()

    monkeypatch.setitem(module, "_open_request", lambda *_args: FakeResponse({}, raw=b"not-json"))
    with pytest.raises(ProviderRequestError, match="^octen_invalid_response$"):
        spec.execute_search(None, "octen", _args(), "key", {}, {})

    oversized = b"{" + b"x" * (module["_MAX_RESPONSE_BYTES"] + 1)
    monkeypatch.setitem(module, "_open_request", lambda *_args: FakeResponse({}, raw=oversized))
    with pytest.raises(ProviderRequestError, match="^octen_response_too_large$"):
        spec.execute_search(None, "octen", _args(), "key", {}, {})
