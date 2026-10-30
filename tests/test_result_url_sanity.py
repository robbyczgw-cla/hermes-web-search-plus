"""Corrupt result URLs disappear before ranking, observations and caches."""

import copy
import json

import pytest

from plugin_loader import load_plugin
from wsp_core import providers, search
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.config import _deepcopy_default_config
from wsp_core.contract_v3 import Capability
from wsp_core.provider_registry import SEARCH_PROVIDER_IDS
from wsp_core.result_redirects import combined_redirect_counts
from wsp_core.urls import clean_result_urls, sane_result_url, strip_tracking_params

plugin = load_plugin("wsp_plugin_test_url_sanity")
BAD_QUERIT = "https://www.python.org/downloads/?2002zzp&1712aznkjÂ"


@pytest.mark.parametrize("url", [
    BAD_QUERIT, "https://example.org/path\npage", "https://example.org/?q=\tbad",
    "https://example.org/\x00", "https://example.org/\x7f", "https://example.org/\x85",
    "https://example.org/\ufffd", "https://example.org/?q=%00", "https://example.org/%EF%BF%BD",
    "https://example.org/ZÃ¼rich", "https://example.org/?q=badÂ©",
    "https://example.org/2002zzpÃ", "https://example.org/%FF",
    None, "",
])
def test_drop_obvious_corruption(url):
    assert not sane_result_url(url)
    if isinstance(url, str):
        # The provider helper must not silently erase the evidence before counting.
        assert strip_tracking_params(url) == url


@pytest.mark.parametrize("url", [
    "https://en.wikipedia.org/wiki/Zürich", "https://de.wikipedia.org/wiki/Z%C3%BCrich",
    "https://example.org/東京?q=日本語", "https://example.org/São_Paulo",
    "https://example.org/Ãngela", "https://example.org/Ângelo", "https://example.org/SÃO",
    "https://example.org/2026/Zürich?q=caf%C3%A9", "https://münchen.example/über-uns",
    "https://docs.python.org/sv/3/", "https://example.org/?q=François&price=20",
])
def test_keep_legitimate_iris_and_other_languages(url):
    assert sane_result_url(url)


def _config(tmp_path):
    cfg = _deepcopy_default_config()
    cfg["v3"] = {"cache_dir": str(tmp_path), "state_path": str(tmp_path / "state.sqlite3"),
                 "operator_receipt_journal": False}
    cfg["bounded_context"] = {"cache_root": str(tmp_path)}
    return cfg


def _answer(provider):
    return {"provider": provider, "query": "python downloads", "results": [
        {"title": "Broken", "url": BAD_QUERIT, "snippet": "Bad source"},
        {"title": "Python", "url": "https://www.python.org/downloads/", "snippet": "Official downloads"},
        {"title": "Swedish docs", "url": "https://docs.python.org/sv/3/", "snippet": "Keep other languages"},
    ]}


@pytest.mark.parametrize("provider", SEARCH_PROVIDER_IDS)
def test_shared_normalisation_covers_every_provider(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(search, "validate_api_key", lambda *a: "fake-key")
    monkeypatch.setitem(search.SEARCH_DISPATCH, provider, lambda *a: copy.deepcopy(_answer(provider)))
    result = search.run_search_request(query="python downloads", provider=provider, count=8, config=_config(tmp_path))
    assert result["metadata"]["invalid_urls_dropped"] == 1
    assert {item["url"] for item in result["results"]} == {
        "https://www.python.org/downloads/", "https://docs.python.org/sv/3/",
    }
    header = plugin._format_results(result).splitlines()[1]
    assert header.endswith("invalid_urls_dropped=1]")
    assert header.count("invalid_urls_dropped=") == 1


def test_native_receipt_and_cache_preserve_drop_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERIT_API_KEY", "fake-test-api-key")
    calls = []

    def fake_search(**kw):
        calls.append(kw)
        return _answer("querit")

    monkeypatch.setattr(providers, "search_querit", fake_search)
    cfg = _config(tmp_path)
    result = search.run_search_request(query="python downloads", provider="querit", config=cfg)
    cached = search.run_search_request(query="python downloads", provider="querit", config=cfg)
    assert cached["cached"] and len(calls) == 1
    assert cached["metadata"]["invalid_urls_dropped"] == 1
    assert plugin._format_results(cached).splitlines()[1].endswith("invalid_urls_dropped=1]")
    request = legacy_request_to_v3(Capability.SEARCH, policy_mode="classic", payload={
        "query": "python downloads", "provider": "querit",
    })
    response = search.run_search_request_v3(request, config=cfg).to_dict()
    warning = next(w for w in response["warnings"] if w["code"] == "wsp.search.invalid_urls")
    assert warning["details"] == {"invalid_urls_dropped": 1}
    assert BAD_QUERIT not in json.dumps(result, ensure_ascii=False)
    assert all("1712aznkj" not in str(obs) for obs in response["observations"])


def test_clean_results_add_no_header_bit_and_counts_survive_merges():
    answer = _answer("querit")
    clean_result_urls(answer)
    clean_result_urls(answer)
    assert answer["metadata"]["invalid_urls_dropped"] == 1
    assert combined_redirect_counts([("querit", answer), ("brave", {"metadata": {"invalid_urls_dropped": 2}})]) == {
        "invalid_urls_dropped": 3,
    }
    assert "invalid_urls_dropped=" not in plugin._format_results({"provider": "brave", "results": []})
    assert "invalid_urls_dropped=" not in plugin._format_results({"metadata": {"invalid_urls_dropped": 0}})


def test_all_corrupt_results_fall_back_and_keep_count(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERIT_API_KEY", "fake-test-api-key")
    monkeypatch.setenv("BRAVE_API_KEY", "fake-test-api-key")
    bad = _answer("querit")
    bad["results"] = bad["results"][:1]
    monkeypatch.setattr(providers, "search_querit", lambda **kw: copy.deepcopy(bad))
    monkeypatch.setattr(providers, "search_brave", lambda **kw: {
        "provider": "brave", "query": kw["query"], "results": _answer("brave")["results"][1:],
    })
    monkeypatch.setattr(search, "auto_route_provider", lambda *a, **kw: {
        "provider": "querit", "confidence": 1.0, "routing_policy": "routing-v3",
    })
    cfg = _config(tmp_path)
    cfg["auto_routing"]["provider_priority"] = ["querit", "brave"]
    cfg["auto_routing"]["auto_allow"]["querit"] = True
    result = search.run_search_request(query="python downloads", provider="auto", config=cfg)
    assert result["provider"] == "brave"
    assert result["metadata"]["invalid_urls_dropped"] == 1
