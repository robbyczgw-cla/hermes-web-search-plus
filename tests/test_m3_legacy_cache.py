"""v3 search is cached by the v3 cache only; the pre-v3 JSON cache is not read.

The pre-v3 cache (``<cache dir>/<key>.json``) stays what ``python search.py``
uses. A v3 search neither reads nor writes it.
"""

from __future__ import annotations

import json

import pytest

from wsp_core import cache
from wsp_core import providers
from wsp_core import search
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.contract_v3 import Capability, RequestV3


def _config(tmp_path):
    return {
        "version": 1,
        "auto_routing": {
            "enabled": True,
            "provider_priority": ["serper"],
            "disabled_providers": [],
        },
        "serper": {"api_key": "serper-test-key-123456"},
        "v3": {
            "cache_dir": str(tmp_path),
            "state_path": str(tmp_path / "v3" / "state.sqlite3"),
        },
    }


def _payload(url="https://example.com/live", snippet="from the provider"):
    return {
        "provider": "serper",
        "query": "q",
        "results": [{"title": "Live", "url": url, "snippet": snippet}],
        "routing": {"auto_routed": False, "provider": "serper"},
        "metadata": {"dedup_count": 0},
        "cached": False,
        "deduplicated": False,
    }


def _count_core_calls(monkeypatch, payload=None):
    calls = []

    def fake_core(args, _config):
        calls.append(args)
        return payload or _payload(), 0

    monkeypatch.setattr(search, "_execute_search_request_core", fake_core)
    return calls


def _request(payload, *, cache_mode="prefer", request_id="r"):
    request = legacy_request_to_v3(Capability.SEARCH, payload, request_id=request_id)
    if cache_mode == "prefer":
        return request
    return RequestV3.from_dict(
        {**request.to_dict(), "cache": {**request.cache, "mode": cache_mode}}
    )


def _run(request, config):
    return search.execute_v3_request(request, search._search_adapter(), config)


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "legacy cache", "provider": "serper", "count": 2},
        {"query": "legacy cache", "provider": "auto", "count": 5},
        {
            "query": "bookshelf speakers",
            "provider": "serper",
            "count": 3,
            "depth": "deep",
            "time_range": "week",
            "include_domains": ["example.com"],
            "exclude_domains": ["example.org"],
            "language": "de",
            "country": "at",
        },
        {
            "query": "open source licenses",
            "provider": "serper",
            "count": 4,
            "search_type": "news",
            "freshness": "month",
            "quality_report": True,
        },
    ],
    ids=["explicit-provider", "auto-provider", "locale-filters", "news-quality-report"],
)
def test_same_query_and_options_are_served_from_the_v3_cache(
    tmp_path, monkeypatch, payload
):
    calls = _count_core_calls(monkeypatch)
    config = _config(tmp_path)

    first = _run(_request(payload, request_id="first"), config)
    second = _run(_request(payload, request_id="second"), config)

    assert len(calls) == 1
    assert first.response.cache_status["disposition"] == "miss"
    assert "cache_write" in first.stage_trace
    assert second.response.cache_status["disposition"] == "fresh_hit"
    assert second.response.cache_status["source_contract_version"] == "3.0"
    assert second.response.provider_attempts == []
    assert [item["url"]["observed"] for item in second.response.results] == [
        "https://example.com/live"
    ]
    assert second.legacy_payload["cached"] is True
    assert list((tmp_path / "v3" / "response" / "search").glob("*.json"))

    # A different option is a different request: it reaches the provider.
    _run(_request({**payload, "count": payload["count"] + 1}), config)
    assert len(calls) == 2


def test_tool_entrypoint_repeat_is_a_v3_cache_hit(tmp_path, monkeypatch):
    calls = _count_core_calls(monkeypatch)
    config = _config(tmp_path)

    first = search.run_search_request(
        query="legacy cache", provider="serper", count=2, config=config
    )
    second = search.run_search_request(
        query="legacy cache", provider="serper", count=2, config=config
    )

    assert len(calls) == 1
    assert not first["cached"]
    assert second["cached"] is True
    assert second["results"][0]["url"] == "https://example.com/live"


def _write_legacy_entry(request, config, tmp_path):
    """Write the pre-v3 entry the old lookup would have matched byte for byte."""
    args = search._search_args_from_v3(request, config)
    args.provider = "serper"
    params = search._legacy_search_cache_context(args, "serper", config)
    legacy_payload = {
        "provider": "serper",
        "query": request.input["query"],
        "results": [
            {
                "title": "Cached",
                "url": "https://example.com/legacy",
                "snippet": "from v2",
            }
        ],
    }
    cache.cache_put(
        request.input["query"],
        "serper",
        request.options["max_results"],
        legacy_payload,
        params=params,
    )
    path = cache._get_cache_path(
        cache._get_cache_key(
            request.input["query"], "serper", request.options["max_results"], params
        )
    )
    assert path.parent == tmp_path
    return path


def test_v3_search_does_not_read_a_legacy_cache_file(tmp_path, monkeypatch):
    calls = _count_core_calls(monkeypatch)
    config = _config(tmp_path)
    request = _request({"query": "legacy cache", "provider": "serper", "count": 2})
    legacy_path = _write_legacy_entry(request, config, tmp_path)
    before = legacy_path.read_bytes()

    execution = _run(request, config)

    assert len(calls) == 1
    assert execution.response.cache_status == {"disposition": "miss"}
    assert [item["url"]["observed"] for item in execution.response.results] == [
        "https://example.com/live"
    ]
    assert execution.response.provider_attempts[0].provider == "serper"
    assert legacy_path.read_bytes() == before


def test_cache_only_request_does_not_use_a_legacy_cache_file(tmp_path, monkeypatch):
    calls = _count_core_calls(monkeypatch)
    config = _config(tmp_path)
    request = _request(
        {"query": "legacy cache", "provider": "serper", "count": 2},
        cache_mode="only",
    )
    legacy_path = _write_legacy_entry(request, config, tmp_path)
    before = legacy_path.read_bytes()

    execution = _run(request, config)

    assert calls == []
    assert execution.response.status.value == "failed"
    assert execution.response.error.code == "wsp.cache.miss"
    assert execution.response.cache_status == {"disposition": "miss"}
    assert legacy_path.read_bytes() == before
    assert not (tmp_path / "v3" / "response" / "search").exists()


def test_cli_search_keeps_using_the_legacy_cache(tmp_path, monkeypatch):
    """`python search.py` runs the core directly and keeps its own JSON cache."""
    monkeypatch.setattr(search, "provider_in_cooldown", lambda _provider: (False, 0))
    monkeypatch.setattr(
        search, "execute_provider_with_retry", lambda _provider, fn: fn()
    )
    provider_calls = []

    def search_serper(**_kwargs):
        provider_calls.append("serper")
        return _payload()

    monkeypatch.setattr(providers, "search_serper", search_serper)
    config = _config(tmp_path)

    def cli_args():
        args = search.default_search_args(config)
        args.query = "legacy cache"
        args.provider = "serper"
        args.max_results = 2
        return args

    first, first_code = search._execute_search_request_core(cli_args(), config)
    second, second_code = search._execute_search_request_core(cli_args(), config)

    assert (first_code, second_code) == (0, 0)
    assert provider_calls == ["serper"]
    assert first["cached"] is False
    assert second["cached"] is True
    entries = [
        path
        for path in tmp_path.glob("*.json")
        if "_cache_timestamp" in json.loads(path.read_text(encoding="utf-8"))
    ]
    assert len(entries) == 1
