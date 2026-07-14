from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import bounded_context_v3 as bounded_context
import search
from bounded_context_v3 import FullTextStore
from cache_v3 import derive_cache_key
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability
from http_client import ProviderRequestError


ROOT = Path(__file__).resolve().parents[1]


def _routing(provider: str = "tavily") -> dict:
    return {
        "provider": provider,
        "confidence": 0.9,
        "confidence_level": "high",
        "reason": "entrypoint regression fixture",
        "routing_policy": "routing-v2",
        "top_signals": [],
        "scores": {provider: 1.0},
        "auto_allow_excluded": [],
        "analysis_summary": {"routing_class": "research"},
    }


def _provider_payload(provider: str) -> dict:
    return {
        "provider": provider,
        "query": "compare alpha beta",
        "results": [
            {
                "title": f"{provider} source",
                "url": f"https://{provider}.example/source",
                "snippet": f"{provider} evidence",
            }
        ],
        "images": [],
        "answer": "",
        "metadata": {},
    }


def _runtime_config(tmp_path: Path) -> dict:
    return {
        "version": 1,
        "auto_routing": {
            "enabled": True,
            "provider_priority": ["tavily", "linkup"],
            "disabled_providers": [],
            "auto_allow": {"tavily": True, "linkup": True},
        },
        "tavily": {"api_key": "tavily-test-key"},
        "linkup": {"api_key": "linkup-test-key"},
        "quality": {"filter_spam": False, "max_results_per_domain": 0},
        "routing": {"policy_mode": "classic"},
        "extract": {"allow_private_urls": True},
        "bounded_context": {
            "cache_root": str(tmp_path),
            "max_urls": 10,
            "max_context_chars": 60_000,
            "full_text_ttl_seconds": 604_800,
            "full_text_max_bytes": 268_435_456,
        },
        "v3": {
            "state_path": str(tmp_path / "state.sqlite3"),
            "cache_dir": str(tmp_path),
            "operator_receipt_journal": True,
            "default_max_provider_attempts": 3,
            "max_attempts_per_provider": 1,
        },
    }


def test_native_research_entrypoint_queries_multiple_providers_with_truthful_attempts(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    def fake_provider(provider: str):
        def execute(**_kwargs):
            calls.append(provider)
            return _provider_payload(provider)

        return execute

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(search, "search_tavily", fake_provider("tavily"))
    monkeypatch.setattr(search, "search_linkup", fake_provider("linkup"))
    monkeypatch.setattr(
        search,
        "extract_plus",
        lambda **_kwargs: {"provider": "fixture-extract", "results": []},
    )
    config = _runtime_config(tmp_path)
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "compare alpha beta",
            "provider": "auto",
            "mode": "research",
            "count": 5,
            "research_time_budget": 5.0,
            "no_cache": True,
        },
        request_id="research-entrypoint-regression",
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)

    assert set(calls) == {"tavily", "linkup"}
    assert execution.legacy_payload["routing"]["providers_queried"] == [
        "tavily",
        "linkup",
    ]
    assert execution.legacy_payload["metadata"]["providers_merged"] == [
        "tavily",
        "linkup",
    ]
    assert [attempt.provider for attempt in execution.response.provider_attempts] == [
        "tavily",
        "linkup",
    ]
    assert all(
        attempt.decision == "attempted"
        for attempt in execution.response.provider_attempts
    )
    provider_by_attempt = {
        attempt.attempt_id: attempt.provider
        for attempt in execution.response.provider_attempts
    }
    assert {item["provider"] for item in execution.response.observations} == {
        "tavily",
        "linkup",
    }
    assert all(
        provider_by_attempt[item["provider_attempt_id"]] == item["provider"]
        for item in execution.response.observations
    )


def test_public_research_is_json_clean_complete_and_never_uses_lossy_cache(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    def fake_provider(provider: str):
        def execute(**_kwargs):
            calls.append(provider)
            return _provider_payload(provider)

        return execute

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(search, "search_tavily", fake_provider("tavily"))
    monkeypatch.setattr(search, "search_linkup", fake_provider("linkup"))
    monkeypatch.setattr(
        search,
        "extract_plus",
        lambda **_kwargs: {
            "provider": "fixture-extract",
            "results": [
                {
                    "title": "Grounded source",
                    "url": "https://tavily.example/source",
                    "content": "FULL_BODY",
                }
            ],
        },
    )
    config = _runtime_config(tmp_path)

    first = search.run_search_request(
        query="compare alpha beta",
        provider="auto",
        mode="research",
        freshness="week",
        search_type="news",
        quality_report=True,
        research_time_budget=5.0,
        config=config,
    )
    second = search.run_search_request(
        query="compare alpha beta",
        provider="auto",
        mode="research",
        freshness="week",
        search_type="news",
        quality_report=True,
        research_time_budget=5.0,
        config=config,
    )

    json.dumps(first, allow_nan=False)
    json.dumps(second, allow_nan=False)
    assert not any(str(key).startswith("_v3_") for key in first)
    assert first["mode"] == second["mode"] == "research"
    assert first["provider"] == second["provider"] == "research"
    assert first["routing"]["providers_queried"] == ["tavily", "linkup"]
    assert first["source_summaries"][0]["content"] == "FULL_BODY"
    assert second["source_summaries"][0]["content"] == "FULL_BODY"
    assert first["metadata"]["freshness"]["requested"] == "week"
    assert first["metadata"]["search_type"]["requested"] == "news"
    assert "quality_report" in first
    assert calls.count("tavily") == 2
    assert calls.count("linkup") == 2
    assert not (tmp_path / "v3" / "response" / "search").exists()


def test_research_fanout_is_not_coupled_to_fallback_and_quality_runs_after_merge(
    tmp_path, monkeypatch
):
    payloads = {
        "tavily": {
            "provider": "tavily",
            "query": "compare alpha beta",
            "results": [
                {
                    "title": "A",
                    "url": "https://same.test/a",
                    "snippet": "A" * 50,
                },
                {
                    "title": "B",
                    "url": "https://same.test/b",
                    "snippet": "B" * 50,
                },
            ],
            "images": [],
            "answer": "",
            "metadata": {},
        },
        "linkup": {
            "provider": "linkup",
            "query": "compare alpha beta",
            "results": [
                {
                    "title": "C",
                    "url": "https://other.test/c",
                    "snippet": "C" * 50,
                }
            ],
            "images": [],
            "answer": "",
            "metadata": {},
        },
    }

    def fake_provider(provider: str):
        return lambda **_kwargs: payloads[provider]

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(search, "search_tavily", fake_provider("tavily"))
    monkeypatch.setattr(search, "search_linkup", fake_provider("linkup"))
    monkeypatch.setattr(
        search,
        "extract_plus",
        lambda **_kwargs: {"provider": "fixture-extract", "results": []},
    )
    config = _runtime_config(tmp_path)
    config["quality"]["max_results_per_domain"] = 1
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "compare alpha beta",
            "provider": "auto",
            "mode": "research",
            "count": 5,
            "no_cache": True,
        },
    )
    request = replace(
        request,
        routing={**request.routing, "allow_fallback": False},
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)
    public = search.v3_response_to_legacy_search(execution)

    assert execution.plan.candidate_order == ("tavily", "linkup")
    assert public["results"], json.dumps(public, sort_keys=True)
    assert [item["url"] for item in public["results"]] == [
        "https://same.test/a",
        "https://other.test/c",
        "https://same.test/b",
    ]
    assert public["metadata"]["domain_diversity_demoted"] == 1


def test_explicit_research_provider_stays_strict_without_fallback(
    tmp_path, monkeypatch
):
    calls: list[str] = []

    def fake_provider(provider: str):
        def execute(**_kwargs):
            calls.append(provider)
            return _provider_payload(provider)

        return execute

    monkeypatch.setattr(search, "search_tavily", fake_provider("tavily"))
    monkeypatch.setattr(search, "search_linkup", fake_provider("linkup"))
    monkeypatch.setattr(
        search,
        "extract_plus",
        lambda **_kwargs: {"provider": "fixture-extract", "results": []},
    )

    result = search.run_search_request(
        query="compare alpha beta",
        provider="tavily",
        mode="research",
        research_time_budget=5.0,
        config=_runtime_config(tmp_path),
    )

    assert calls == ["tavily"]
    assert result["routing"]["providers_queried"] == ["tavily"]


def test_fixed_default_research_provider_stays_strict(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_provider(provider: str):
        def execute(**_kwargs):
            calls.append(provider)
            return _provider_payload(provider)

        return execute

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(search, "search_tavily", fake_provider("tavily"))
    monkeypatch.setattr(search, "search_linkup", fake_provider("linkup"))
    monkeypatch.setattr(
        search,
        "extract_plus",
        lambda **_kwargs: {"provider": "fixture-extract", "results": []},
    )
    config = _runtime_config(tmp_path)
    config["default_provider"] = "tavily"
    config["auto_routing"]["enabled"] = False

    result = search.run_search_request(
        query="compare alpha beta",
        provider="auto",
        mode="research",
        research_time_budget=5.0,
        config=config,
    )

    assert calls == ["tavily"]
    assert result["routing"]["providers_queried"] == ["tavily"]


def test_research_total_failure_is_failed_and_not_cached(tmp_path, monkeypatch):
    calls: list[str] = []

    def failing_provider(provider: str):
        def execute(**_kwargs):
            calls.append(provider)
            raise ProviderRequestError(f"{provider} failed", transient=False)

        return execute

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(search, "search_tavily", failing_provider("tavily"))
    monkeypatch.setattr(search, "search_linkup", failing_provider("linkup"))
    config = _runtime_config(tmp_path)
    legacy_request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "compare alpha beta",
            "provider": "auto",
            "mode": "research",
            "research_time_budget": 5.0,
        },
    )
    native_request = replace(
        legacy_request,
        cache={"mode": "prefer", "ttl_seconds": 3600},
    )

    execution = search.execute_v3_request(
        native_request, search._search_adapter(), config
    )

    assert execution.response.status.value == "failed"
    assert execution.response.error is not None
    assert execution.response.results == []
    assert all(
        attempt.outcome.value == "failed"
        for attempt in execution.response.provider_attempts
    )
    public = search.v3_response_to_legacy_search(execution)
    json.dumps(public, allow_nan=False)
    assert public["error"] == "All research providers failed"
    assert set(calls) == {"tavily", "linkup"}
    assert not (tmp_path / "v3" / "response" / "search").exists()


def test_started_research_timeout_is_attempted_cancelled_and_snapshot_stable(
    tmp_path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    slow_calls = 0

    def slow_provider(**_kwargs):
        nonlocal slow_calls
        slow_calls += 1
        started.set()
        release.wait(timeout=3.0)
        raise ProviderRequestError("late transient failure", transient=True)

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(search, "search_tavily", lambda **_kwargs: _provider_payload("tavily"))
    monkeypatch.setattr(search, "search_linkup", slow_provider)
    monkeypatch.setattr(
        search,
        "extract_plus",
        lambda **_kwargs: {"provider": "fixture-extract", "results": []},
    )
    config = _runtime_config(tmp_path)
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "compare alpha beta",
            "provider": "auto",
            "mode": "research",
            "research_time_budget": 1.0,
            "no_cache": True,
        },
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)
    assert started.is_set()
    slow_attempt = next(
        attempt
        for attempt in execution.response.provider_attempts
        if attempt.provider == "linkup"
    )
    assert slow_attempt.outcome.value == "cancelled"
    assert slow_attempt.decision == "attempted"
    assert slow_attempt.skip_reason is None
    assert slow_attempt.error is not None
    assert slow_attempt.error.error_class.value == "timeout"
    assert slow_attempt.tries[0]["outcome"] == "error"
    assert execution.response.status.value == "degraded"
    assert {warning["code"] for warning in execution.response.warnings} == {
        "wsp.budget.limited"
    }
    slow_decision = next(
        item
        for item in execution.response.routing_receipt["candidate_decisions"]
        if item["provider"] == "linkup"
    )
    assert slow_decision["decision"] == "attempted_failed"
    snapshot = json.dumps(execution.response.to_dict(), sort_keys=True)

    release.set()
    time.sleep(0.05)
    assert slow_calls == 1
    assert json.dumps(execution.response.to_dict(), sort_keys=True) == snapshot


def test_research_extraction_timeout_is_budget_limited(tmp_path, monkeypatch):
    release = threading.Event()

    def slow_extract(**_kwargs):
        release.wait(timeout=3.0)
        return {"provider": "fixture-extract", "results": []}

    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(
        search, "search_tavily", lambda **_kwargs: _provider_payload("tavily")
    )
    monkeypatch.setattr(
        search, "search_linkup", lambda **_kwargs: _provider_payload("linkup")
    )
    monkeypatch.setattr(search, "extract_plus", slow_extract)
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "compare alpha beta",
            "provider": "auto",
            "mode": "research",
            "research_time_budget": 1.0,
            "no_cache": True,
        },
    )

    execution = search.execute_v3_request(
        request, search._search_adapter(), _runtime_config(tmp_path)
    )
    release.set()

    assert all(
        attempt.outcome.value == "success"
        for attempt in execution.response.provider_attempts
    )
    assert execution.response.status.value == "degraded"
    assert {warning["code"] for warning in execution.response.warnings} == {
        "wsp.budget.limited"
    }
    assert "research time budget exhausted" in execution.legacy_payload["routing"][
        "extraction_error"
    ]


def test_never_started_research_deadline_receipts_match_the_wire_schema(
    tmp_path, monkeypatch
):
    jsonschema = pytest.importorskip("jsonschema")
    monkeypatch.setattr(search, "auto_route_provider", lambda _query, _config: _routing())
    monkeypatch.setattr(
        search,
        "run_research_mode",
        lambda **_kwargs: {
            "mode": "research",
            "provider": "research",
            "query": "compare alpha beta",
            "results": [],
            "source_summaries": [],
            "routing": {
                "providers_queried": [],
                "provider_errors": [],
                "extraction_provider": None,
            },
            "metadata": {
                "dedup_count": 0,
                "providers_merged": [],
                "extracted_url_count": 0,
            },
        },
    )
    request = legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "compare alpha beta",
            "provider": "auto",
            "mode": "research",
            "research_time_budget": 1.0,
            "no_cache": True,
        },
    )

    execution = search.execute_v3_request(
        request, search._search_adapter(), _runtime_config(tmp_path)
    )

    assert all(
        attempt.outcome.value == "skipped"
        and attempt.skip_reason.value == "deadline_exceeded"
        and attempt.budget_decision == "unknown"
        for attempt in execution.response.provider_attempts
    )
    schema = json.loads((ROOT / "schemas" / "v3" / "response.schema.json").read_text())
    jsonschema.validate(execution.response.to_dict(), schema)


def test_public_extract_cache_hit_preserves_the_same_body(tmp_path, monkeypatch):
    calls = 0
    body = "BODY_SENTINEL_" + ("x" * 200)

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [
                {
                    "title": "Cached page",
                    "url": "https://example.com/page",
                    "content": body,
                    "raw_content": body,
                    "provider": "linkup",
                }
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)

    miss = search.run_extract_request(
        ["https://example.com/page"], provider="linkup", config=config
    )
    hit = search.run_extract_request(
        ["https://example.com/page"], provider="linkup", config=config
    )

    assert calls == 1
    assert miss["results"][0]["content"] == body
    assert hit["results"][0]["content"] == body
    assert miss["results"][0]["raw_content"] == body
    assert hit["results"][0]["raw_content"] == body
    assert hit["results"][0]["provider"] == "linkup"
    assert hit["routing"] == miss["routing"]


def test_empty_extract_cache_hit_preserves_provider(tmp_path, monkeypatch):
    calls = 0

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)

    miss = search.run_extract_request(
        ["https://example.com/empty"], provider="linkup", config=config
    )
    hit = search.run_extract_request(
        ["https://example.com/empty"], provider="linkup", config=config
    )

    assert calls == 1
    assert miss["provider"] == hit["provider"] == "linkup"
    assert hit["cached"] is True


def test_cache_identity_includes_attempt_budget_but_not_request_policy():
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://example.com/budget"], "provider": "linkup"},
        request_id="first-request",
    )
    one_attempt = replace(request, budget={"max_provider_attempts": 1})
    three_attempts = replace(request, budget={"max_provider_attempts": 3})

    assert derive_cache_key(one_attempt) != derive_cache_key(three_attempts)
    assert derive_cache_key(one_attempt) == derive_cache_key(
        replace(
            one_attempt,
            request_id="second-request",
            cache={"mode": "only", "ttl_seconds": 1},
        )
    )


def test_extract_cache_hit_rechecks_the_current_private_url_policy(
    tmp_path, monkeypatch
):
    calls = 0
    url = "http://127.0.0.1/private"

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [{"title": "Private", "url": url, "content": "secret"}],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    allowed_config = _runtime_config(tmp_path)
    blocked_config = {
        **allowed_config,
        "extract": {"allow_private_urls": False},
    }

    allowed = search.run_extract_request(
        [url], provider="linkup", config=allowed_config
    )
    blocked = search.run_extract_request(
        [url], provider="linkup", config=blocked_config
    )

    assert allowed["results"][0]["content"] == "secret"
    assert calls == 1
    assert blocked["results"] == []
    assert "private/internal" in blocked["error"]
    assert not blocked.get("cached")


def test_extract_cache_identity_includes_full_text_storage_policy(
    tmp_path, monkeypatch
):
    calls = 0
    url = "https://example.com/storage-policy"

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [{"title": "Long", "url": url, "content": "A" * 90_000}],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    roomy = _runtime_config(tmp_path)
    roomy["bounded_context"]["full_text_max_bytes"] = 200_000
    disabled = {
        **roomy,
        "bounded_context": {
            **roomy["bounded_context"],
            "full_text_max_bytes": 0,
        },
    }
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": [url], "provider": "linkup"},
    )

    first = search.run_extract_request_v3(request, config=roomy)
    second = search.run_extract_request_v3(request, config=disabled)

    assert calls == 2
    assert first.stored_content[0]["storage_succeeded"] is True
    assert second.cache_status["disposition"] == "miss"
    assert second.stored_content[0]["storage_succeeded"] is False
    assert not list((tmp_path / "web" / "v3").glob("*.md"))


def test_cached_full_text_reference_is_immutable_across_bypass_refresh(
    tmp_path, monkeypatch
):
    calls = 0
    url = "https://example.com/versioned"
    current = {"content": "A" * 90_000}

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [
                {"title": "Versioned", "url": url, "content": current["content"]}
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": [url], "provider": "linkup"},
    )

    first = search.run_extract_request_v3(request, config=config)
    first_reference = first.stored_content[0]["reference"]
    current["content"] = "B" * 90_000
    bypass = search.run_extract_request_v3(
        replace(request, cache={"mode": "bypass"}), config=config
    )
    cached = search.run_extract_request_v3(request, config=config)

    store = FullTextStore(tmp_path)
    retained = store.lookup(first_reference["key"])
    assert calls == 2
    assert bypass.stored_content[0]["reference"] != first_reference
    assert cached.cache_status["disposition"] == "fresh_hit"
    assert retained == "A" * 90_000
    assert cached.stored_content[0]["full_text_sha256"] == first.stored_content[0][
        "full_text_sha256"
    ]


def test_parallel_distinct_full_text_versions_get_immutable_references(
    tmp_path, monkeypatch
):
    store = FullTextStore(tmp_path, max_bytes=1_000_000)
    url = "https://example.com/concurrent"
    texts = ["A" * 90_000, "B" * 90_000]
    original_write = bounded_context._atomic_write_owned
    rendezvous = threading.Barrier(2)

    def synchronized_write(path, text):
        rendezvous.wait(timeout=5)
        original_write(path, text)

    monkeypatch.setattr(bounded_context, "_atomic_write_owned", synchronized_write)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.store, url, text) for text in texts]
        stored = [future.result(timeout=10) for future in futures]

    keys = [item["reference"]["key"] for item in stored]
    assert keys[0] != keys[1]
    assert [store.lookup(key) for key in keys] == texts


def test_extract_cache_rejects_the_previous_lossy_schema(tmp_path, monkeypatch):
    calls = 0

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [
                {
                    "title": "Schema fixture",
                    "url": "https://example.com/schema",
                    "content": "body",
                }
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)
    url = "https://example.com/schema"

    search.run_extract_request([url], provider="linkup", config=config)
    cache_file = next((tmp_path / "v3" / "response" / "extract").glob("*.json"))
    old_envelope = json.loads(cache_file.read_text())
    old_envelope["cache_schema_version"] = 2
    cache_file.write_text(json.dumps(old_envelope))

    result = search.run_extract_request([url], provider="linkup", config=config)

    assert calls == 2
    assert not result.get("cached")
    assert json.loads(cache_file.read_text())["cache_schema_version"] == 3


@pytest.mark.parametrize("first_count,second_count", [(10, 11), (11, 10)])
def test_extract_cache_identity_includes_all_original_urls(
    tmp_path, monkeypatch, first_count, second_count
):
    calls: list[list[str]] = []

    def fake_core(**kwargs):
        urls = list(kwargs["urls"])
        calls.append(urls)
        return {
            "provider": "linkup",
            "results": [
                {"title": url, "url": url, "content": "body"} for url in urls
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)
    all_urls = [f"https://example.com/{index}" for index in range(11)]

    def execute(count: int):
        request = legacy_request_to_v3(
            Capability.EXTRACT,
            {"urls": all_urls[:count], "provider": "linkup"},
        )
        return search.run_extract_request_v3(request, config=config)

    first = execute(first_count)
    second = execute(second_count)
    hit = execute(second_count)

    assert len(calls) == 2
    assert [len(urls) for urls in calls] == [min(first_count, 10), min(second_count, 10)]
    assert first.limits_applied["extract"]["requested_url_count"] == first_count
    assert second.limits_applied["extract"]["requested_url_count"] == second_count
    assert second.limits_applied["extract"]["omitted_urls"] == all_urls[10:second_count]
    assert hit.cache_status["disposition"] == "fresh_hit"
    assert hit.limits_applied == second.limits_applied
    assert len(list((tmp_path / "v3" / "response" / "extract").glob("*.json"))) == 2


def test_partial_extract_errors_are_never_projected_through_lossy_cache(
    tmp_path, monkeypatch
):
    calls = 0
    urls = ["https://example.com/ok", "https://example.com/fail"]

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [
                {"title": "OK", "url": urls[0], "content": "body"},
                {"title": "Fail", "url": urls[1], "error": "upstream failed"},
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)

    first = search.run_extract_request(urls, provider="linkup", config=config)
    second = search.run_extract_request(urls, provider="linkup", config=config)

    assert calls == 2
    assert first["results"][1]["error"] == "upstream failed"
    assert second["results"][1]["error"] == "upstream failed"
    assert not (tmp_path / "v3" / "response" / "extract").exists()


@pytest.mark.parametrize(
    "request_kwargs,result_field,result_value",
    [
        ({"include_raw_html": True}, "raw_html", "<p>raw</p>"),
        (
            {"include_images": True},
            "images",
            [{"url": "https://example.com/image.png"}],
        ),
        ({}, "metadata", {"provider_specific": True}),
    ],
)
def test_unprojectable_extract_options_bypass_cache(
    tmp_path, monkeypatch, request_kwargs, result_field, result_value
):
    calls = 0

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "linkup",
            "results": [
                {
                    "title": "Raw",
                    "url": "https://example.com/raw",
                    "content": "body",
                    result_field: result_value,
                }
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)

    first = search.run_extract_request(
        ["https://example.com/raw"],
        provider="linkup",
        config=config,
        **request_kwargs,
    )
    second = search.run_extract_request(
        ["https://example.com/raw"],
        provider="linkup",
        config=config,
        **request_kwargs,
    )

    assert calls == 2
    assert first["results"][0][result_field] == result_value
    assert second["results"][0][result_field] == result_value
    assert not (tmp_path / "v3" / "response" / "extract").exists()


def test_public_extract_uses_one_global_fair_share_budget(tmp_path, monkeypatch):
    urls = [f"https://example.com/{index}" for index in range(6)]

    def fake_core(**_kwargs):
        return {
            "provider": "linkup",
            "results": [
                {"title": f"Doc {index}", "url": url, "content": "X" * 30_000}
                for index, url in enumerate(urls)
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    config = _runtime_config(tmp_path)

    result = search.run_extract_request(urls, provider="linkup", config=config)
    lengths = [len(item["content"]) for item in result["results"]]

    assert lengths == [10_000] * 6
    assert sum(lengths) == 60_000


def test_bounded_extract_is_cached_and_receipted_after_limits(tmp_path, monkeypatch):
    url = "https://example.com/long"

    monkeypatch.setattr(
        search._extract,
        "_extract_plus_core",
        lambda **_kwargs: {
            "provider": "linkup",
            "results": [
                {"title": "Long", "url": url, "content": "Y" * 90_000}
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        },
    )
    config = _runtime_config(tmp_path)

    search.run_extract_request([url], provider="linkup", config=config)

    cache_files = list((tmp_path / "v3" / "response" / "extract").glob("*.json"))
    assert len(cache_files) == 1
    material = json.loads(cache_files[0].read_text())["payload"]
    assert material["limits_applied"]["extract"]["max_context_chars"] == 60_000
    assert len(material["projection"][0]["text"]["text"]) == 60_000

    native_request = legacy_request_to_v3(
        Capability.EXTRACT,
        {
            "urls": [url],
            "provider": "linkup",
            "format": "markdown",
            "no_cache": False,
        },
    )
    cached_response = search._extract.run_extract_request_v3(
        native_request,
        config=config,
    )
    assert cached_response.cache_status["disposition"] == "fresh_hit"
    assert cached_response.limits_applied == material["limits_applied"]
    assert cached_response.policy_actions == material["policy_actions"]
    assert cached_response.stored_content == material["stored_content"]

    receipt_lines = (tmp_path / "operator" / "v3" / "receipts.jsonl").read_text().splitlines()
    receipt = json.loads(receipt_lines[-1])["payload"]
    assert receipt["status"] == "degraded"
    assert "wsp.content.truncated" in receipt["warning_codes"]
    assert receipt["limits_applied"]["extract"] == {
        "requested_url_count": 1,
        "omitted_url_count": 0,
        "max_urls": 10,
        "max_context_chars": 60_000,
        "context_chars_returned": 60_000,
        "truncated": True,
    }


def test_public_metadata_does_not_advertise_rejected_answer_providers():
    plugin_yaml = (ROOT / "plugin.yaml").read_text()
    assert "PERPLEXITY_API_KEY" not in plugin_yaml
    assert "KILOCODE_API_KEY" not in plugin_yaml

    spec = importlib.util.spec_from_file_location(
        "wsp_v302_metadata_under_test", ROOT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Context:
        def __init__(self):
            self.tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

    context = Context()
    module.register(context)
    search_schema = json.dumps(context.tools["web_search_plus"]["schema"]).lower()
    assert "perplexity" not in search_schema
    assert "kilo-perplexity" not in search_schema

    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text().lower()
    assert "config disable perplexity" not in guide
    assert "config enable perplexity" not in guide
