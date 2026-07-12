import json
from pathlib import Path
from unittest import mock

import jsonschema
import pytest
import search
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability, ResponseV3
from http_client import ProviderRequestError


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


def test_engine_owned_provider_call_bypasses_all_legacy_retry_and_health(monkeypatch):
    dummy_key = "test-key-123456789012345678901234"
    config = {**CONFIG, "you": {"api_key": dummy_key}}
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "q", "provider": "you", "count": 1},
        request_id="engine-owned",
    )
    args = search._search_args_from_v3(request, config)
    args.no_cache = True
    args._v3_engine_owned_attempt = True

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy health/retry seam was called")

    monkeypatch.setattr(search, "provider_in_cooldown", forbidden)
    monkeypatch.setattr(search, "execute_provider_with_retry", forbidden)
    monkeypatch.setattr(search, "mark_provider_failure", forbidden)
    monkeypatch.setattr(search, "reset_provider_health", forbidden)
    monkeypatch.setattr(search, "record_provider_outcome", forbidden)
    monkeypatch.setattr(
        search,
        "search_you",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderRequestError("upstream", status_code=503, transient=True)
        ),
    )

    with pytest.raises(ProviderRequestError):
        search._execute_search_request_core(args, config)


def test_engine_owned_extract_call_bypasses_legacy_retry_and_health(monkeypatch):
    config = {
        "linkup": {"api_key": "linkup-test-key-123456789012345678"},
        "extract": {"allow_private_urls": True},
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy extract health/retry seam was called")

    monkeypatch.setattr(search._extract, "provider_in_cooldown", forbidden)
    monkeypatch.setattr(search._extract, "execute_provider_with_retry", forbidden)
    monkeypatch.setattr(search._extract, "mark_provider_failure", forbidden)
    monkeypatch.setattr(search._extract, "reset_provider_health", forbidden)
    monkeypatch.setattr(
        search._extract,
        "extract_linkup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderRequestError("upstream", status_code=503, transient=True)
        ),
    )

    with pytest.raises(ProviderRequestError):
        search._extract._extract_plus_core(
            ["https://example.com/a"],
            provider="linkup",
            config=config,
            engine_owned_attempt=True,
        )


def test_extract_adapter_emits_engine_attempt_receipt(tmp_path, monkeypatch):
    calls = []

    def fake_core(**kwargs):
        calls.append((kwargs["provider"], kwargs["engine_owned_attempt"]))
        return _extract_payload()

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = {
        "version": 1,
        "linkup": {"api_key": "linkup-test-key-123456789012345678"},
        "extract": {"allow_private_urls": True},
        "v3": {"state_path": str(tmp_path / "state.sqlite3")},
    }
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://example.com/a"], "provider": "linkup"},
        request_id="extract-attempt",
    )

    execution = search.execute_v3_request(
        request, search._extract._extract_adapter(), config
    )

    assert calls == [("linkup", True)]
    assert len(execution.response.provider_attempts) == 1
    assert execution.response.provider_attempts[0].provider == "linkup"
    assert execution.response.provider_attempts[0].outcome.value == "success"
    assert "admission" in execution.stage_trace
    assert "dedup_fingerprint" in execution.stage_trace


def test_search_adapter_emits_engine_attempt_receipt(tmp_path, monkeypatch):
    calls = []

    def fake_core(args, _config):
        calls.append(
            (args.provider, args.no_cache, args._v3_engine_owned_attempt)
        )
        return _search_payload(), 0

    monkeypatch.setattr(search, "_execute_search_request_core", fake_core)
    config = {
        **CONFIG,
        "serper": {"api_key": "test-key-123456789012345678901234"},
        "v3": {"state_path": str(tmp_path / "state.sqlite3")},
    }
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "q", "provider": "serper", "count": 1},
        request_id="attempt-receipt",
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)

    assert calls == [("serper", True, True)]
    assert len(execution.response.provider_attempts) == 1
    assert execution.response.provider_attempts[0].provider == "serper"
    assert execution.response.provider_attempts[0].outcome.value == "success"
    assert execution.stage_trace == (
        "normalize",
        "validate",
        "cache_lookup",
        "candidate_plan",
        "admission",
        "provider_attempt",
        "retry_circuit_update",
        "result_normalization",
        "dedup_fingerprint",
        "cache_write",
        "response_v3",
    )


def test_search_engine_owns_typed_fallback_and_receipts(tmp_path, monkeypatch):
    calls = []

    def fake_core(args, _config):
        calls.append(args.provider)
        if args.provider == "serper":
            raise ProviderRequestError("bad key", status_code=401)
        payload = _search_payload()
        payload["provider"] = "you"
        return payload, 0

    monkeypatch.setattr(search, "_execute_search_request_core", fake_core)
    config = {
        "version": 1,
        "auto_routing": {
            "enabled": True,
            "provider_priority": ["serper", "you"],
            "disabled_providers": [],
        },
        "serper": {"api_key": "serper-test-key-12345678901234567890"},
        "you": {"api_key": "you-test-key-12345678901234567890123"},
        "v3": {"state_path": str(tmp_path / "state.sqlite3")},
    }
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "q", "provider": "serper", "count": 1},
        request_id="typed-fallback",
    )
    request = type(request).from_dict(
        {
            **request.to_dict(),
            "routing": {**request.routing, "allow_fallback": True},
        }
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)

    assert calls == ["serper", "you"]
    assert [attempt.outcome.value for attempt in execution.response.provider_attempts] == [
        "failed",
        "success",
    ]
    assert execution.response.provider_attempts[0].error.error_class.value == "auth"
    assert execution.legacy_payload["routing"]["fallback_used"] is True
    assert execution.legacy_payload["routing"]["original_provider"] == "serper"
    assert execution.legacy_payload["routing"]["provider"] == "you"
    assert "error_classification" in execution.stage_trace
    assert "fallback" in execution.stage_trace
    assert "bad key" not in json.dumps(execution.response.to_dict())


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
    assert calls == [("same", "serper")]
    assert native_execution.response.cache_status["disposition"] == "fresh_hit"


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
    assert len(calls) == 1
    assert native.cache_status["disposition"] == "fresh_hit"


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
    assert len(calls) == 1
    assert native.cache_status["disposition"] == "fresh_hit"
