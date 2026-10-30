"""Recorded-shape Serper regression: positions 4–10 are opaque Google links.

Tokens/content are synthetic; the 3 direct + 7 wrapped shape is from the live
2026-10-30 report. No provider key or live Google token is stored here.
"""

import copy
import io
import json
import socket
import threading
import time
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from plugin_loader import load_plugin
from wsp_core import http_client, providers, result_redirects, search
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.config import _deepcopy_default_config
from wsp_core.contract_v3 import Capability

plugin = load_plugin("wsp_plugin_test_result_redirects")
FIXTURE = json.loads((Path(__file__).parent / "fixtures/serper_goto.json").read_text())
TARGETS = {
    "CAESduplicate": (302, "https://www.skins.example/guide/?utm_source=google"),
    "CAESresolved": (302, "https://download.example/skin"),
    "CAES200": (200, None),
    "CAESprivate": (302, "http://127.0.0.1/admin"),
    "CAESnested": (302, "https://www.google.com/url?q=https%3A%2F%2Fexample.com"),
    "CAESrelative": (302, "/relative"),
}


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
    ])


@pytest.fixture
def fake_http(monkeypatch, public_dns):
    calls = []

    def open_request(req, timeout):
        calls.append((req, timeout))
        token = parse_qs(urlsplit(req.full_url).query)["url"][0]
        if token == "CAEStimeout":
            raise TimeoutError("fake timeout")
        status, location = TARGETS[token]
        response = HTTPError(req.full_url, status, "fake", {"Location": location} if location else {}, io.BytesIO())
        if status >= 300:
            raise response
        return response

    opener = mock.Mock()
    opener.open.side_effect = open_request
    factory = mock.Mock(return_value=opener)
    monkeypatch.setattr(http_client, "_safe_opener", factory)
    return calls, factory


def payload():
    with mock.patch.object(providers, "make_request", return_value=copy.deepcopy(FIXTURE)):
        return providers.search_serper("Football Manager 27 skins modding", "fake", max_results=10)


def config(tmp_path):
    cfg = _deepcopy_default_config()
    cfg["v3"] = {"cache_dir": str(tmp_path), "state_path": str(tmp_path / "state.sqlite3"),
                 "operator_receipt_journal": False}
    cfg["bounded_context"] = {"cache_root": str(tmp_path)}
    return cfg


def test_recorded_shape_resolves_drops_and_deduplicates_before_display(fake_http):
    result = payload()
    result_redirects.clean_result_redirects(result, {})
    assert {key: result["metadata"][key] for key in result_redirects.COUNT_KEYS} == {
        "redirects_wrapped": 7, "redirects_resolved": 2, "redirects_dropped": 5,
    }
    assert [item["url"] for item in result["results"]] == [
        "https://skins.example/guide", "https://community.example/mods",
        "https://docs.example/modding", "https://download.example/skin",
    ]
    assert result["deduplicated"] and result["metadata"]["dedup_count"] == 1
    text = plugin._format_results({**result, "elapsed_ms": 123, "fallback_from": ["brave"]})
    assert "google.com/goto" not in text
    assert text.splitlines()[1].endswith("elapsed_ms=123 | fallback_from=brave | redirects_dropped=5]")
    assert len(text) < 1600
    calls, factory = fake_http
    assert len(calls) == 7
    assert all(req.get_method() == "GET" and 0 < timeout <= 2 for req, timeout in calls)
    assert all(req.get_header("User-agent") == http_client.DEFAULT_USER_AGENT for req, _ in calls)
    assert all(call.kwargs == {"follow_redirects": False} for call in factory.call_args_list)


@pytest.mark.parametrize("host", ["google.com", "www.google.de", "www.google.co.uk", "google.com.au"])
@pytest.mark.parametrize("param", ["q", "url"])
def test_classic_redirects_decode_offline(host, param, public_dns):
    target = "https://destination.example/page?a=1&b=two%20words"
    result = {"results": [{"url": f"https://{host}/url?{param}={quote(target, safe='')}"}]}
    with mock.patch.object(http_client, "redirect_location") as get:
        result_redirects.clean_result_redirects(result, {})
    get.assert_not_called()
    assert result["results"][0]["url"] == target
    assert result["metadata"]["redirects_resolved"] == 1
    assert "redirects_dropped=" not in plugin._format_results(result).splitlines()[1]


@pytest.mark.parametrize("target", [
    "http://10.0.0.1/", "http://[::1]/", "http://metadata.google.internal/",
    "ftp://example.com/file", "//example.com/page", "https://user:pass@example.com/",
    "https://www.google.de/goto?url=next", "https://google.com/url", "https://example.com/\npath",
])
def test_unsafe_locations_are_dropped(target, fake_http, monkeypatch):
    monkeypatch.setattr(http_client, "redirect_location", lambda *a, **kw: target)
    result = {"results": [{"url": "https://google.com/goto?url=CAESbad"}]}
    result_redirects.clean_result_redirects(result, {})
    assert result["results"] == [] and result["metadata"]["redirects_dropped"] == 1


def test_dns_private_destination_is_dropped(fake_http, monkeypatch):
    monkeypatch.setattr(http_client, "redirect_location", lambda *a, **kw: "https://private.example/")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3" if host == "private.example" else "93.184.216.34", port))
    ])
    result = {"results": [{"url": "https://google.com/goto?url=x"}]}
    result_redirects.clean_result_redirects(result, {})
    assert result["results"] == []


def test_config_off_drops_all_wrappers_without_http(fake_http):
    result = payload()
    result_redirects.clean_result_redirects(result, {"serper": {"resolve_redirects": False}})
    assert len(result["results"]) == 3
    assert result["metadata"]["redirects_dropped"] == 7
    assert result["metadata"]["redirects_resolved"] == 0
    assert fake_http[0] == []


def test_no_resolution_starts_after_deadline(fake_http):
    result = payload()
    with http_client.request_deadline_scope(time.monotonic() - 1):
        result_redirects.clean_result_redirects(result, {})
    assert fake_http[0] == []
    assert result["metadata"]["redirects_dropped"] == 7


def test_deadline_bounds_wait_and_prevents_queued_requests(fake_http, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked(*a, **kw):
        calls.append(time.monotonic())
        entered.set()
        release.wait(1)
        return "https://destination.example/"

    monkeypatch.setattr(http_client, "redirect_location", blocked)
    result = payload()
    started = time.monotonic()
    try:
        with http_client.request_deadline_scope(started + 0.15):
            result_redirects.clean_result_redirects(result, {})
        assert entered.is_set() and 1 <= len(calls) <= 5
        assert time.monotonic() - started < 0.6
        assert len(result["results"]) == 3
    finally:
        release.set()
    snapshot = copy.deepcopy(result)
    time.sleep(0.03)
    assert result == snapshot
    assert len(calls) <= 5


def test_dispatch_timeout_cap_applies_to_redirect_get(fake_http):
    with http_client.request_timeout_cap(0.25):
        result_redirects.clean_result_redirects(payload(), {})
    assert all(timeout <= 0.25 for _, timeout in fake_http[0])


@pytest.mark.parametrize("provider", ["serper", "serpbase", "you"])
def test_engine_cleans_every_provider_before_cache(provider, tmp_path, monkeypatch, fake_http):
    monkeypatch.setenv({"serper": "SERPER_API_KEY", "serpbase": "SERPBASE_API_KEY", "you": "YOU_API_KEY"}[provider], "fake-test-api-key")
    answer = payload()
    answer["provider"] = provider
    mock_search = mock.Mock(side_effect=lambda **kw: copy.deepcopy(answer))
    monkeypatch.setattr(providers, f"search_{provider}", mock_search)
    cfg = config(tmp_path)
    first = search.run_search_request(query="Football Manager 27 skins modding", provider=provider, count=8, config=cfg)
    assert first["metadata"]["redirects_dropped"] == 5
    assert len(first["results"]) == 4
    assert "google.com/goto" not in json.dumps(first)
    calls = len(fake_http[0])
    second = search.run_search_request(query="Football Manager 27 skins modding", provider=provider, count=8, config=cfg)
    assert second["cached"] is True
    assert len(fake_http[0]) == calls and mock_search.call_count == 1
    assert [item["url"] for item in second["results"]] == [item["url"] for item in first["results"]]
    assert second["metadata"]["redirects_resolved"] == 2
    # Turning resolution off has its own cache identity.
    cfg["serper"]["resolve_redirects"] = False
    third = search.run_search_request(query="Football Manager 27 skins modding", provider=provider, count=8, config=cfg)
    assert not third["cached"] and len(third["results"]) == 3
    assert third["metadata"]["redirects_dropped"] == 7


def test_native_json_receipt_keeps_counts_and_no_wrapper_observations(tmp_path, monkeypatch, fake_http):
    monkeypatch.setenv("SERPER_API_KEY", "fake-test-api-key")
    monkeypatch.setattr(providers, "make_request", lambda *a, **kw: copy.deepcopy(FIXTURE))
    request = legacy_request_to_v3(Capability.SEARCH, policy_mode="classic", payload={
        "query": "Football Manager 27 skins modding", "provider": "serper", "count": 8,
    })
    response = search.run_search_request_v3(request, config=config(tmp_path)).to_dict()
    receipt = next(item for item in response["warnings"] if item["code"] == "wsp.search.redirects")
    assert receipt["details"] == {"redirects_wrapped": 7, "redirects_resolved": 2, "redirects_dropped": 5}
    assert "google.com/goto" not in json.dumps(response)


def test_empty_wrapped_provider_keeps_drop_counts_after_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "fake-test-api-key")
    monkeypatch.setenv("BRAVE_API_KEY", "fake-test-api-key")
    answer = payload()
    answer["results"] = answer["results"][3:]
    monkeypatch.setattr(providers, "search_serper", lambda **kw: copy.deepcopy(answer))
    monkeypatch.setattr(providers, "search_brave", lambda **kw: {
        "provider": "brave", "query": kw["query"],
        "results": [{"title": "Direct", "url": "https://direct.example/page", "snippet": "Direct source"}],
    })
    monkeypatch.setattr(search, "auto_route_provider", lambda *a, **kw: {
        "provider": "serper", "confidence": 1.0, "routing_policy": "routing-v3",
    })
    cfg = config(tmp_path)
    cfg["auto_routing"]["provider_priority"] = ["serper", "brave"]
    cfg["serper"]["resolve_redirects"] = False
    result = search.run_search_request(query="Football Manager 27 skins modding", provider="auto", config=cfg)
    assert result["provider"] == "brave"
    assert result["metadata"]["redirects_dropped"] == 7
    assert plugin._format_results(result).splitlines()[1].endswith("redirects_dropped=7]")
