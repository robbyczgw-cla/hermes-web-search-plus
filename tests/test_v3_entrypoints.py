import json
from pathlib import Path
from unittest import mock

import jsonschema
import search
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability, ResponseV3


CONFIG = {
    "version": 1,
    "auto_routing": {
        "enabled": True,
        "provider_priority": ["serper"],
        "disabled_providers": [],
    },
}
RESPONSE_SCHEMA = json.loads(Path("schemas/v3/response.schema.json").read_text())


def _search_payload():
    return {
        "provider": "serper",
        "query": "q",
        "results": [
            {
                "title": "A",
                "url": "https://example.com/a",
                "snippet": "S",
                "published_at": "Yesterday",
            }
        ],
        "routing": {
            "auto_routed": False,
            "provider": "serper",
            "routing_policy": "classic-v2",
        },
        "metadata": {"dedup_count": 0},
        "cached": False,
        "deduplicated": False,
    }


def _extract_payload():
    return {
        "provider": "linkup",
        "results": [
            {
                "title": "A",
                "url": "https://example.com/a",
                "content": "Café",
            }
        ],
        "routing": {
            "provider": "linkup",
            "requested_provider": "linkup",
            "fallback_used": False,
            "fallback_errors": [],
        },
    }


def test_v3_execution_can_read_but_never_writes_legacy_cache(monkeypatch):
    dummy_key = "test-key-123456789012345678901234"
    monkeypatch.setenv("YOU_API_KEY", dummy_key)
    monkeypatch.setattr(search, "cache_get", lambda **_kwargs: None)
    cache_put = mock.Mock()
    monkeypatch.setattr(search, "cache_put", cache_put)
    monkeypatch.setattr(search, "provider_in_cooldown", lambda _provider: (False, 0))
    monkeypatch.setattr(
        search, "execute_provider_with_retry", lambda _provider, fn: fn()
    )
    monkeypatch.setattr(
        search,
        "search_you",
        lambda **_kwargs: _search_payload(),
    )

    runtime_config = {
        **CONFIG,
        "you": {"api_key": dummy_key},
    }
    result = search.run_search_request(
        query="q", provider="you", count=1, config=runtime_config
    )

    assert result["results"]
    cache_put.assert_not_called()


def test_b6_engine_adapter_plan_is_identical_for_legacy_and_native(monkeypatch):
    calls = []

    def fake_core(args, config):
        calls.append((args.query, args.provider))
        return _search_payload(), 0

    monkeypatch.setattr(search, "_execute_search_request_core", fake_core)
    legacy_request = legacy_request_to_v3(
        "search",
        {"query": "same", "provider": "serper", "count": 1},
        request_id="b6",
    )
    native_request = type(legacy_request).from_dict(legacy_request.to_dict())

    legacy_execution = search.execute_v3_request(
        legacy_request, search._search_adapter(), CONFIG
    )
    native_execution = search.execute_v3_request(
        native_request, search._search_adapter(), CONFIG
    )

    assert legacy_execution.plan == native_execution.plan
    assert (
        legacy_execution.response.routing_receipt
        == native_execution.response.routing_receipt
    )
    assert calls == [("same", "serper"), ("same", "serper")]


def test_search_legacy_and_native_use_same_v3_entry_and_one_core_call(monkeypatch):
    calls = []

    def fake_core(args, config):
        calls.append((args.query, args.provider, config))
        return _search_payload(), 0

    monkeypatch.setattr(search, "_execute_search_request_core", fake_core)
    legacy = search.run_search_request(
        query="q", provider="serper", count=1, config=CONFIG
    )
    assert legacy == _search_payload()
    assert len(calls) == 1

    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "q", "provider": "serper", "count": 1},
        request_id="native-search",
    )
    native = search.run_search_request_v3(request, config=CONFIG)

    assert isinstance(native, ResponseV3)
    assert native.request_id == "native-search"
    assert native.routing_receipt["candidate_order"] == ["serper"]
    assert native.routing_receipt["selected_provider"] == "serper"
    assert native.results[0]["url"] == "https://example.com/a"
    assert "published_at" not in native.results[0]
    jsonschema.validate(
        native.to_dict(), RESPONSE_SCHEMA, format_checker=jsonschema.FormatChecker()
    )
    assert len(calls) == 2


def test_extract_legacy_and_native_use_same_v3_entry_and_one_core_call(monkeypatch):
    calls = []

    def fake_core(**kwargs):
        calls.append(kwargs)
        return _extract_payload()

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    legacy = search.run_extract_request(
        ["https://example.com/a"], provider="linkup", config=CONFIG
    )
    assert legacy == _extract_payload()
    assert len(calls) == 1

    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://example.com/a"], "provider": "linkup"},
        request_id="native-extract",
    )
    native = search.run_extract_request_v3(request, config=CONFIG)

    assert isinstance(native, ResponseV3)
    assert native.request_id == "native-extract"
    assert native.routing_receipt["candidate_order"][0] == "linkup"
    assert native.results[0]["text"] == "Café"
    assert native.results[0]["text_normalization"] == "NFC"
    jsonschema.validate(
        native.to_dict(), RESPONSE_SCHEMA, format_checker=jsonschema.FormatChecker()
    )
    assert len(calls) == 2
