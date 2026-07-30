from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

import provider_registry
import providers
from http_client import ProviderRequestError
from provider_adapter_protocol import validate_adapter_result


class FakeResponse:
    def __init__(self, payload=None, *, raw: bytes | None = None):
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._raw if size < 0 else self._raw[:size]


def _provider_globals():
    spec = provider_registry.PROVIDER_SPECS["tinyfish"]
    return spec, spec.execute_search.__globals__


def _args(**overrides):
    values = {
        "query": "tinyfish contract query",
        "max_results": 3,
        "freshness": "week",
        "time_range": None,
        "include_domains": ["docs.python.org", "example.com"],
        "exclude_domains": ["spam.example"],
        "search_type": "search",
        "country": "at",
        "language": "de",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_tinyfish_registers_as_privacy_warned_explicit_only_search_provider():
    spec = provider_registry.PROVIDER_SPECS["tinyfish"]

    assert spec.kind == "search"
    assert spec.env_var == "TINYFISH_API_KEY"
    assert spec.display_name == "TinyFish Search"
    assert spec.keyless is False
    assert spec.auto_allowed_by_default is False
    assert spec.supports_freshness is True
    assert spec.free_tier == "Search does not consume credits; API access required (30 rpm Free/PAYG)"
    assert spec.capability_labels == ("search", "news", "freshness", "privacy-warning")
    assert spec.upstream_capabilities == (
        "search",
        "news",
        "research-paper",
        "freshness",
        "domain-filtering",
    )
    assert "training" in spec.description.lower()
    assert "fine-tuning" in spec.description.lower()
    assert "your own account/api key" in spec.description.lower()
    assert "does not provide, pool, proxy, or share" in spec.description.lower()
    assert "tinyfish.ai/terms" in spec.description
    assert spec.execute_search is not None
    assert "tinyfish" in provider_registry.SEARCH_PROVIDER_IDS
    assert "tinyfish" not in provider_registry.EXTRACT_PROVIDER_IDS
    assert "tinyfish" not in provider_registry.DEFAULT_PROVIDER_PRIORITY
    assert provider_registry.DEFAULT_AUTO_ALLOW["tinyfish"] is False


def test_tinyfish_freshness_capability_drives_truthful_metadata():
    assert providers.provider_supports_freshness("tinyfish") is True
    assert providers.map_freshness_for_provider("tinyfish", "week") == "week"
    assert providers.freshness_metadata("tinyfish", "week") == {
        "requested": "week",
        "applied": True,
        "provider": "tinyfish",
        "native_value": "week",
    }


def test_tinyfish_projects_get_request_and_source_only_response(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "query": "tinyfish contract query",
                "results": [
                    {
                        "position": 1,
                        "site_name": "docs.python.org",
                        "title": "Python docs",
                        "snippet": "Official Python documentation.",
                        "url": "https://docs.python.org/3/",
                        "date": "2026-07-01",
                    },
                    {
                        "position": 2,
                        "site_name": "example.com",
                        "title": "Example",
                        "snippet": "Example source.",
                        "url": "https://example.com/source",
                    },
                ],
                "total_results": 2,
                "page": 0,
            }
        )

    monkeypatch.setitem(module, "_open_request", fake_open)
    result = spec.execute_search(
        None,
        "tinyfish",
        _args(),
        "tinyfish-test-key",
        {"tinyfish": {"timeout": 17}},
        {},
    )

    request = captured["request"]
    parsed = urlsplit(request.full_url)
    params = parse_qs(parsed.query)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "api.search.tinyfish.ai",
        "/",
    )
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.headers["X-api-key"] == "tinyfish-test-key"
    assert request.headers["Accept"] == "application/json"
    assert captured["timeout"] == 17
    assert params == {
        "query": ["tinyfish contract query"],
        "location": ["AT"],
        "language": ["de"],
        "include_domains": ["docs.python.org,example.com"],
        "exclude_domains": ["spam.example"],
        "domain_type": ["web"],
        "recency_minutes": ["10080"],
    }
    assert "fetch" not in params
    assert "purpose" not in params
    assert "include_thumbnail" not in params
    assert "limit" not in params

    assert result == {
        "provider": "tinyfish",
        "query": "tinyfish contract query",
        "results": [
            {
                "url": "https://docs.python.org/3/",
                "title": "Python docs",
                "snippet": "Official Python documentation.",
                "date": "2026-07-01",
                "source": "docs.python.org",
                "position": 1,
            },
            {
                "url": "https://example.com/source",
                "title": "Example",
                "snippet": "Example source.",
                "source": "example.com",
                "position": 2,
            },
        ],
        "images": [],
        "metadata": {"total_results": 2, "page": 0},
    }
    assert validate_adapter_result("tinyfish", "search", result) is result


def test_tinyfish_maps_news_and_unified_freshness(monkeypatch):
    spec, module = _provider_globals()
    captured = {}

    def fake_open(request, _timeout):
        captured["params"] = parse_qs(urlsplit(request.full_url).query)
        return FakeResponse({"query": "news", "results": [], "total_results": 0, "page": 0})

    monkeypatch.setitem(module, "_open_request", fake_open)
    spec.execute_search(
        None,
        "tinyfish",
        _args(
            query="news",
            search_type="news",
            freshness="day",
            include_domains=[],
            exclude_domains=[],
            country=None,
            language=None,
        ),
        "tinyfish-test-key",
        {},
        {},
    )

    assert captured["params"] == {
        "query": ["news"],
        "domain_type": ["news"],
        "recency_minutes": ["1440"],
    }


def test_tinyfish_skips_malformed_and_unsafe_results_without_spending_slots(monkeypatch):
    spec, module = _provider_globals()

    monkeypatch.setitem(
        module,
        "_open_request",
        lambda *_args: FakeResponse(
            {
                "query": "safe",
                "results": [
                    None,
                    {"url": "javascript:alert(1)", "title": "bad"},
                    {"url": "https://user:pass@example.com/private", "title": "credential URL"},
                    {"url": "https://one.example/", "title": 123, "snippet": None, "position": True},
                    {"url": "http://two.example/", "title": "Two", "snippet": "ok", "position": 5},
                ],
                "total_results": 5,
                "page": 0,
            }
        ),
    )

    result = spec.execute_search(
        None,
        "tinyfish",
        _args(max_results=2, include_domains=[], exclude_domains=[], freshness=None),
        "tinyfish-test-key",
        {},
        {},
    )

    assert result["results"] == [
        {"url": "https://one.example/", "title": "", "snippet": ""},
        {
            "url": "http://two.example/",
            "title": "Two",
            "snippet": "ok",
            "position": 5,
        },
    ]


def test_tinyfish_requires_key_and_valid_timeout():
    spec, _module = _provider_globals()

    with pytest.raises(Exception, match="tinyfish_api_key_required"):
        spec.execute_search(None, "tinyfish", _args(), None, {}, {})
    with pytest.raises(Exception, match="tinyfish_timeout_invalid"):
        spec.execute_search(
            None,
            "tinyfish",
            _args(),
            "key",
            {"tinyfish": {"timeout": 0}},
            {},
        )


def test_tinyfish_refuses_redirects_to_keep_credentials_on_fixed_origin():
    _spec, module = _provider_globals()
    request = Request("https://api.search.tinyfish.ai?query=safe")

    redirected = module["_NoRedirectHandler"]().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_tinyfish_bounds_response_and_classifies_rate_limit(monkeypatch):
    spec, module = _provider_globals()

    monkeypatch.setitem(
        module,
        "_open_request",
        lambda *_args: FakeResponse(raw=b"x" * (module["_MAX_RESPONSE_BYTES"] + 1)),
    )
    with pytest.raises(ProviderRequestError, match="tinyfish_response_too_large"):
        spec.execute_search(None, "tinyfish", _args(), "key", {}, {})

    headers = Message()
    headers["Retry-After"] = "2.5"

    def rate_limited(*_args):
        raise HTTPError(
            "https://api.search.tinyfish.ai?query=safe",
            429,
            "Too Many Requests",
            headers,
            None,
        )

    monkeypatch.setitem(module, "_open_request", rate_limited)
    with pytest.raises(ProviderRequestError) as exc_info:
        spec.execute_search(None, "tinyfish", _args(), "key", {}, {})
    assert exc_info.value.status_code == 429
    assert exc_info.value.transient is True
    assert exc_info.value.retry_after == 2.5


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"results": None},
        {"results": "wrong"},
    ],
)
def test_tinyfish_rejects_malformed_top_level_payloads(monkeypatch, payload):
    spec, module = _provider_globals()
    monkeypatch.setitem(module, "_open_request", lambda *_args: FakeResponse(payload))

    with pytest.raises(ProviderRequestError, match="tinyfish_invalid_response"):
        spec.execute_search(None, "tinyfish", _args(), "key", {}, {})


def test_tinyfish_wildcard_deliberately_matches_root_and_subdomains(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_search is not None
    monkeypatch.setitem(
        module,
        "_open_request",
        lambda *_args: FakeResponse(
            {
                "results": [
                    {"url": "https://example.com/root", "title": "root", "snippet": "ok"},
                    {"url": "https://docs.example.com/page", "title": "sub", "snippet": "ok"},
                    {"url": "https://blocked.example.com/page", "title": "blocked", "snippet": "no"},
                    {"url": "https://example.com.evil.test/page", "title": "lookalike", "snippet": "no"},
                    {"url": "https://other.test/page", "title": "other", "snippet": "no"},
                ]
            }
        ),
    )

    result = spec.execute_search(
        None,
        "tinyfish",
        _args(
            max_results=5,
            include_domains=["*.example.com"],
            exclude_domains=["blocked.example.com"],
            freshness=None,
        ),
        "key",
        {},
        {},
    )

    assert [item["url"] for item in result["results"]] == [
        "https://example.com/root",
        "https://docs.example.com/page",
    ]


def test_tinyfish_keeps_punycode_and_ascii_hosts_distinct(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_search is not None
    monkeypatch.setitem(
        module,
        "_open_request",
        lambda *_args: FakeResponse(
            {
                "results": [
                    {
                        "url": "https://xn--fa-hia.de/source",
                        "title": "punycode host",
                    },
                    {"url": "https://fass.de/source", "title": "ASCII host"},
                    {"url": "https://faß.de/source", "title": "raw Unicode host"},
                ]
            }
        ),
    )

    ascii_included = spec.execute_search(
        None,
        "tinyfish",
        _args(
            max_results=3,
            include_domains=["fass.de"],
            exclude_domains=[],
            freshness=None,
        ),
        "key",
        {},
        {},
    )
    punycode_included = spec.execute_search(
        None,
        "tinyfish",
        _args(
            max_results=3,
            include_domains=["xn--fa-hia.de"],
            exclude_domains=[],
            freshness=None,
        ),
        "key",
        {},
        {},
    )

    assert [item["url"] for item in ascii_included["results"]] == [
        "https://fass.de/source"
    ]
    assert [item["url"] for item in punycode_included["results"]] == [
        "https://xn--fa-hia.de/source"
    ]
    assert module["_safe_url"]("https://faß.de/source") == ""


def test_tinyfish_rejects_unsafe_urls_and_bounds_untrusted_text(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_search is not None
    title = "\u202e" + "T" * (module["_MAX_TITLE_CHARS"] + 20)
    snippet = "\u009b" + "S" * (module["_MAX_SNIPPET_CHARS"] + 20)
    monkeypatch.setitem(
        module,
        "_open_request",
        lambda *_args: FakeResponse(
            {
                "results": [
                    {"url": "data:text/plain,bad", "title": "bad"},
                    {"url": "https://", "title": "bad"},
                    {"url": "/relative", "title": "bad"},
                    {"url": "https://example.com/white space", "title": "bad"},
                    {"url": "https://example.com/control\npath", "title": "bad"},
                    {"url": "https://bad_domain.example/path", "title": "bad host"},
                    {"url": "https://safe.example:99999/path", "title": "bad port"},
                    {"url": "\nhttps://safe.example/path", "title": "leading C0"},
                    {"url": "\u0085https://safe.example/path", "title": "leading C1"},
                    {"url": "https://safe.example/path\u0085", "title": "trailing C1"},
                    {"url": "https://safe.example/\u009bx", "title": "C1 control"},
                    {"url": "https://safe.example/\u202ereversed", "title": "bidi control"},
                    {"url": "https://safe.example/" + "x" * module["_MAX_URL_CHARS"], "title": "too long"},
                    {"url": "https://safe.example/page", "title": title, "snippet": snippet},
                ]
            }
        ),
    )

    result = spec.execute_search(
        None,
        "tinyfish",
        _args(max_results=10, include_domains=[], exclude_domains=[], freshness=None),
        "key",
        {},
        {},
    )

    assert len(result["results"]) == 1
    assert len(result["results"][0]["title"]) == module["_MAX_TITLE_CHARS"]
    assert len(result["results"][0]["snippet"]) == module["_MAX_SNIPPET_CHARS"]
    assert "\u202e" not in result["results"][0]["title"]
    assert "\u009b" not in result["results"][0]["snippet"]


@pytest.mark.parametrize(
    "domains",
    [
        ["example.com"] * 21,
        ["localhost"],
        [""],
        [123],
        ["https://example.com"],
        ["bad_domain.example"],
        ["safe.example\u202e.evil"],
        ["Münich.example"],
        ["faß.de"],
        [" example.com"],
        ["example.com "],
        ["\nexample.com"],
        ["example.com\u0085"],
        [f"{'a' * 64}.example"],
        [f"{'a' * 250}.example"],
        ["example.com" + "." * 5_000],
        [f"*.{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 60}"],
        [f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{index:02d}.example" for index in range(20)],
    ],
)
def test_tinyfish_rejects_invalid_or_unbounded_domain_filters(domains):
    spec, _module = _provider_globals()
    assert spec.execute_search is not None
    with pytest.raises(Exception, match="tinyfish_domains_invalid"):
        spec.execute_search(
            None,
            "tinyfish",
            _args(include_domains=domains, exclude_domains=[]),
            "key",
            {},
            {},
        )


def test_tinyfish_canonicalizes_and_deduplicates_domains(monkeypatch):
    spec, module = _provider_globals()
    assert spec.execute_search is not None
    captured = {}

    def fake_open(request, _timeout):
        captured["params"] = parse_qs(urlsplit(request.full_url).query)
        return FakeResponse({"results": []})

    monkeypatch.setitem(module, "_open_request", fake_open)
    spec.execute_search(
        None,
        "tinyfish",
        _args(
            include_domains=["Example.COM.", "example.com", "*.XN--MNICH-KVA.example"],
            exclude_domains=[],
        ),
        "key",
        {},
        {},
    )
    assert captured["params"]["include_domains"] == [
        "example.com,*.xn--mnich-kva.example"
    ]


def test_tinyfish_rejects_oversized_encoded_request():
    _spec, module = _provider_globals()
    with pytest.raises(Exception, match="tinyfish_request_too_large"):
        module["_request"](
            {"query": "?" * module["_MAX_REQUEST_URL_CHARS"]},
            "key",
            30,
        )


def test_tinyfish_public_docs_disclose_byok_and_training_contract():
    root = Path(__file__).resolve().parents[2]
    for relative in ("README.md", "docs/PROVIDERS.md"):
        text = (root / relative).read_text(encoding="utf-8").lower()
        assert "tinyfish" in text
        assert "your own" in text
        assert "does not provide, pool, proxy, or share" in text
        assert "training" in text
        assert "fine-tuning" in text


@pytest.mark.parametrize("query", [None, "", "   ", "x" * 2_001])
def test_tinyfish_enforces_official_query_limit(query):
    spec, _module = _provider_globals()
    assert spec.execute_search is not None
    with pytest.raises(Exception, match="tinyfish_query_invalid"):
        spec.execute_search(None, "tinyfish", _args(query=query), "key", {}, {})


@pytest.mark.parametrize(
    ("status", "transient"),
    [
        (400, False),
        (401, False),
        (402, False),
        (403, False),
        (404, False),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_tinyfish_http_status_classification_is_opaque(monkeypatch, status, transient):
    spec, module = _provider_globals()
    assert spec.execute_search is not None

    def fail(*_args):
        raise HTTPError(
            "https://api.search.tinyfish.ai/?query=must-not-leak",
            status,
            "upstream detail must not leak",
            Message(),
            None,
        )

    monkeypatch.setitem(module, "_open_request", fail)
    with pytest.raises(ProviderRequestError) as exc_info:
        spec.execute_search(None, "tinyfish", _args(), "secret-key", {}, {})

    assert exc_info.value.status_code == status
    assert exc_info.value.transient is transient
    rendered = str(exc_info.value)
    assert "must-not-leak" not in rendered
    assert "upstream detail" not in rendered
    assert "secret-key" not in rendered


@pytest.mark.parametrize("header", ["-1", "nan", "inf", "not-a-number"])
def test_tinyfish_ignores_invalid_retry_after(header):
    _spec, module = _provider_globals()
    headers = Message()
    headers["Retry-After"] = header
    error = HTTPError("https://api.search.tinyfish.ai/", 429, "rate", headers, None)
    assert module["_retry_after"](error) is None


@pytest.mark.parametrize("raw", [b"\xff", b"{not-json", b""])
def test_tinyfish_rejects_invalid_utf8_and_json(monkeypatch, raw):
    spec, module = _provider_globals()
    assert spec.execute_search is not None
    monkeypatch.setitem(module, "_open_request", lambda *_args: FakeResponse(raw=raw))
    with pytest.raises(ProviderRequestError, match="tinyfish_invalid_response"):
        spec.execute_search(None, "tinyfish", _args(), "key", {}, {})
