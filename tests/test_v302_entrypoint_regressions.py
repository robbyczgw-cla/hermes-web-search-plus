from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import search
from compat_v3 import legacy_request_to_v3
from contract_v3 import Capability


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
