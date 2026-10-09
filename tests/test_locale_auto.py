"""Language "auto": request parameters of every provider that carries a language.

Each case drives the real dispatch adapter and provider function with a fake
HTTP layer and reads the language parameter off the outgoing request. Without
"auto" the parameters stay exactly as they were (English queries included);
with "auto" a confidently detected query language is sent and an unclear query
sends no language parameter at all. No network access.
"""
from urllib.parse import parse_qs, urlsplit

import pytest

from wsp_core import config as config_module
from wsp_core import providers, search
from wsp_core.provider_dispatch import SEARCH_DISPATCH

from plugin_loader import load_plugin

plugin = load_plugin("wsp_plugin_test_locale_auto")

GERMAN = "wie funktioniert eine Wärmepumpe im Winter"
ENGLISH = "what are the best coffee houses with long opening hours"
UNCLEAR = "PostgreSQL 17 release notes"

LOCALE_PROVIDERS = ("serper", "serpbase", "brave", "querit", "firecrawl", "you", "searxng")
LANGUAGE_PROVIDERS = tuple(p for p in LOCALE_PROVIDERS if p != "firecrawl")
SEARXNG_URL = "https://93.184.216.34"  # public-address literal: no DNS lookup


class _Sent(Exception):
    """Raised by the fake HTTP layer once the request has been recorded."""


@pytest.fixture
def sent(monkeypatch):
    """Record the outgoing provider request instead of making it."""
    record = {}

    def fake_post(url, headers, body, timeout=30):
        record["url"], record["body"] = url, body
        raise _Sent

    def fake_get(url, headers, timeout=30, **_kwargs):
        record["url"] = url
        raise _Sent

    def fake_urlopen(req, timeout=30):
        record["url"] = req.full_url
        raise _Sent

    monkeypatch.setattr(providers, "make_request", fake_post)
    monkeypatch.setattr(providers, "make_get_request", fake_get)
    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    return record


def _config(language=None):
    config = config_module._deepcopy_default_config()
    config["defaults"]["locale"]["language"] = language
    return config


def _request(provider, query, config, record, language=None):
    """Run one search through the dispatch adapter; return the language on the wire.

    ``("omitted", None)`` means the request carries no language parameter.
    """
    args = search.default_search_args(config)
    args.query = query
    args.language = language
    args.searxng_url = SEARXNG_URL
    key = SEARXNG_URL if provider == "searxng" else "test-key-123456"
    with pytest.raises(_Sent):
        SEARCH_DISPATCH[provider](providers, provider, args, key, config, {})

    if provider in ("serper", "serpbase"):
        body = record["body"]
        return ("sent", body["hl"]) if "hl" in body else ("omitted", None)
    if provider == "querit":
        languages = record["body"].get("filters", {}).get("languages")
        return ("sent", languages["include"]) if languages else ("omitted", None)
    if provider == "firecrawl":
        assert not {"language", "languages", "lang", "hl"} & set(record["body"])
        return ("omitted", None)
    param = "search_lang" if provider == "brave" else "language"
    values = parse_qs(urlsplit(record["url"]).query).get(param)
    return ("sent", values[0]) if values else ("omitted", None)


def _wire(provider, code):
    """How a provider writes a language code on the wire."""
    if provider == "querit":
        return [code]
    if provider == "you":
        return code.upper()
    return code


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
@pytest.mark.parametrize("query", [ENGLISH, UNCLEAR])
def test_without_auto_unclear_and_english_queries_send_en(provider, query, sent):
    assert _request(provider, query, _config(), sent) == ("sent", _wire(provider, "en"))


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
def test_without_any_language_setting_a_confident_detection_beats_the_en_fallback(provider, sent):
    # Nothing configured: a clearly German query is not searched as English.
    assert _request(provider, GERMAN, _config(), sent) == ("sent", _wire(provider, "de"))


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
def test_a_configured_default_language_still_beats_detection(provider, sent):
    config = _config("en")
    assert _request(provider, GERMAN, config, sent) == ("sent", _wire(provider, "en"))


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
@pytest.mark.parametrize("setting", ["config", "flag"])
def test_auto_sends_the_detected_language(provider, setting, sent):
    config = _config("auto" if setting == "config" else None)
    flag = "auto" if setting == "flag" else None
    assert _request(provider, GERMAN, config, sent, language=flag) == ("sent", _wire(provider, "de"))


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
def test_auto_sends_en_for_a_confidently_english_query(provider, sent):
    assert _request(provider, ENGLISH, _config("auto"), sent) == ("sent", _wire(provider, "en"))


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
@pytest.mark.parametrize("setting", ["config", "flag"])
def test_auto_omits_the_language_when_not_confident(provider, setting, sent):
    config = _config("auto" if setting == "config" else None)
    flag = "auto" if setting == "flag" else None
    assert _request(provider, UNCLEAR, config, sent, language=flag) == ("omitted", None)


@pytest.mark.parametrize("provider", LANGUAGE_PROVIDERS)
def test_explicit_language_beats_auto_config(provider, sent):
    assert _request(provider, GERMAN, _config("auto"), sent, language="fr") == ("sent", _wire(provider, "fr"))


def test_firecrawl_carries_a_country_but_no_language(sent):
    config = _config("auto")
    assert _request("firecrawl", GERMAN, config, sent) == ("omitted", None)
    assert sent["body"]["country"] == "US"


@pytest.mark.parametrize("provider", ["serper", "serpbase", "brave", "querit", "firecrawl", "you", "searxng"])
def test_auto_leaves_the_country_parameter_alone(provider, sent):
    _request(provider, UNCLEAR, _config("auto"), sent)
    if provider in ("serper", "serpbase"):
        assert sent["body"]["gl"] == "us"
    elif provider == "querit":
        assert sent["body"]["filters"]["geo"] == {"countries": {"include": ["US"]}}
    elif provider == "firecrawl":
        assert sent["body"]["country"] == "US"
    elif provider in ("brave", "you"):
        assert "country=US" in sent["url"]
    else:
        assert "country" not in sent["url"]  # searxng has no country parameter


def test_serper_image_request_also_omits_hl(monkeypatch):
    bodies = []

    def fake_post(url, headers, body, timeout=30):
        bodies.append((url, dict(body)))
        return {"organic": [], "images": []}

    monkeypatch.setattr(providers, "make_request", fake_post)
    providers.search_serper("q", "test-key-123456", language=None, include_images=True)
    assert [url.rsplit("/", 1)[1] for url, _ in bodies] == ["search", "images"]
    assert all("hl" not in body and body["gl"] == "us" for _, body in bodies)

    bodies.clear()
    providers.search_serper("q", "test-key-123456", language="de", include_images=True)
    assert all(body["hl"] == "de" for _, body in bodies)


def test_language_none_keeps_the_other_request_parameters(sent):
    # Dropping the language must not drop or reorder anything else.
    with pytest.raises(_Sent):
        providers.search_brave("q", "test-key-123456", language=None, time_range="week")
    query = parse_qs(urlsplit(sent["url"]).query)
    assert query == {
        "q": ["q"], "count": ["5"], "country": ["US"], "safesearch": ["moderate"],
        "spellcheck": ["1"], "freshness": ["pw"],
    }
    with pytest.raises(_Sent):
        providers.search_serper("q", "test-key-123456", language=None)
    assert list(sent["body"]) == ["q", "gl", "num", "autocorrect"]
    with pytest.raises(_Sent):
        providers.search_serper("q", "test-key-123456")
    assert list(sent["body"]) == ["q", "gl", "hl", "num", "autocorrect"]



def test_auto_and_plain_calls_do_not_share_response_cache_entries(monkeypatch):
    # A per-call "auto" is not part of the v3 request, so it must still change the cache key.
    bodies = []

    def fake_post(url, headers, body, timeout=30):
        bodies.append(dict(body))
        return {"organic": [{"title": "T", "link": "https://example.test/a", "snippet": "s"}]}

    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key-123456")
    monkeypatch.setattr(providers, "make_request", fake_post)
    config = _config()

    def run(**kwargs):
        return search.run_search_request(query=UNCLEAR, provider="serper", config=config, **kwargs)

    run(language="auto")
    run(language="auto")  # served from the cache
    run()  # different effective request: hl=en
    run()  # served from the cache
    assert ["hl" in body for body in bodies] == [False, True]


@pytest.mark.parametrize("query,expected", [(GERMAN, "de"), (UNCLEAR, None)])
def test_tool_handler_accepts_language_auto(query, expected, monkeypatch):
    bodies = []

    def fake_post(url, headers, body, timeout=30):
        bodies.append(dict(body))
        return {"organic": [{"title": "T", "link": "https://example.test/a", "snippet": "s"}]}

    registered = {}

    class Ctx:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs

    plugin.register(Ctx())
    schema = registered["web_search_plus"]["schema"]["parameters"]["properties"]["language"]
    assert schema["type"] == "string" and "enum" not in schema

    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key-123456")
    monkeypatch.setattr(providers, "make_request", fake_post)
    registered["web_search_plus"]["handler"]({"query": query, "provider": "serper", "language": "auto"})
    assert [body.get("hl") for body in bodies] == [expected]


@pytest.mark.parametrize("language, country, expected", [
    ("de", "AT", "de"),
    ("ja", "JP", "ja"),
    ("pt", "BR", "pt-br"),
    ("pt", "PT", "pt-pt"),
    ("zh", "TW", "zh-hant"),
    ("zh", "CN", "zh-hans"),
    ("xx", "US", None),
])
def test_brave_gets_a_language_from_its_enum(sent, language, country, expected):
    # Brave rejects search_lang values outside its list ("pt" and "zh" need a
    # region); an unknown code is left out instead of failing the request.
    with pytest.raises(_Sent):
        providers.search_brave("q", "test-key-123456", country=country, language=language)
    assert parse_qs(urlsplit(sent["url"]).query).get("search_lang") == ([expected] if expected else None)
