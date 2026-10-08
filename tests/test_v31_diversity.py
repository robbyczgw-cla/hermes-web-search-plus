from __future__ import annotations
from wsp_core import providers

import json
from copy import deepcopy

import pytest

from wsp_core import operator_privacy_v3 as privacy
from wsp_core import search
from wsp_core.config import DEFAULT_CONFIG, _validate_runtime_config
from wsp_core.contract_v3 import Capability
from wsp_core.diversity_v3 import (
    canonical_url,
    registrable_domain,
    rerank_duplicate_candidates,
    score_diversity,
    snippet_similarity,
)
from wsp_core.operator_receipts_v3 import receipt_record_from_response
from wsp_core import research
from wsp_core.research import run_research_mode


def _result(
    identifier: str,
    url: str,
    snippet: str,
    provider: str = "fixture",
) -> dict:
    return {
        "id": identifier,
        "title": identifier,
        "url": url,
        "snippet": snippet,
        "provider": provider,
    }


def _provider_payload(provider: str, results: list[dict]) -> dict:
    return {
        "provider": provider,
        "query": "diversity fixture",
        "results": results,
        "images": [],
        "answer": "",
        "metadata": {},
    }


def _fixture_research_sources() -> dict[str, dict]:
    return {
        "alpha": _provider_payload(
            "alpha",
            [
                _result(
                    "a",
                    "https://same.example/a",
                    "alpha beta gamma delta epsilon zeta",
                    "alpha",
                ),
                _result(
                    "b",
                    "https://content.example/b",
                    "alpha beta gamma delta epsilon eta",
                    "alpha",
                ),
            ],
        ),
        "beta": _provider_payload(
            "beta",
            [
                _result(
                    "c",
                    "https://same.example/a",
                    "unrelated source words differ completely here",
                    "beta",
                ),
                _result(
                    "d",
                    "https://unique.example/d",
                    "orchid maple quartz lantern river summit",
                    "beta",
                ),
            ],
        ),
    }


def _run_fixture_research(*, diversity_rerank: bool = False) -> dict:
    sources = _fixture_research_sources()
    return run_research_mode(
        query="diversity fixture",
        research_providers=["alpha", "beta"],
        execute_search=lambda provider: sources[provider],
        extract_urls=lambda _urls: {"provider": None, "results": []},
        max_results=4,
        diversity_rerank=diversity_rerank,
    )


def test_registrable_domain_handles_multilabel_idn_ports_and_edges() -> None:
    assert registrable_domain("https://WWW.docs.Example.CO.UK:443/guide") == "example.co.uk"
    assert registrable_domain("https://sub.example.com.au/search") == "example.com.au"
    assert registrable_domain("https://service.gv.at/form") == "service.gv.at"
    assert registrable_domain("https://Bücher.co.uk/catalog") == "xn--bcher-kva.co.uk"
    assert registrable_domain("https://127.0.0.1:8080/status") == "127.0.0.1"
    assert registrable_domain("http://localhost:8080/") == "localhost"
    assert registrable_domain("not a URL") == ""


def test_canonical_url_removes_tracking_fragments_and_normalizes_case() -> None:
    assert canonical_url(
        "HTTPS://Example.COM:443/a/?b=2&utm_source=news&a=1&gclid=ignored#details"
    ) == "https://example.com/a?a=1&b=2"
    assert canonical_url("http://example.com:80/path/#fragment") == "http://example.com/path"
    assert canonical_url("https://example.com:8443/path/") == "https://example.com:8443/path"
    assert canonical_url("https://Bücher.example/?fbclid=ignored") == "https://xn--bcher-kva.example"


def test_shingle_similarity_handles_identical_near_unrelated_and_short_text() -> None:
    exact = "alpha beta gamma delta epsilon zeta"
    near = "alpha beta gamma delta epsilon eta"
    assert snippet_similarity(exact, exact) == 1.0
    assert snippet_similarity(exact, near) == pytest.approx(0.6)
    assert snippet_similarity(exact, "orchid maple quartz lantern river summit") == 0.0
    assert snippet_similarity("too short", "too short") == 0.0


def test_score_is_deterministic_bounded_and_rewards_diverse_fixture() -> None:
    seo_variants = [
        _result(
            f"seo-{index}",
            f"https://seo.example/article-{index}",
            "search marketing guide compares the same repeated advice today",
        )
        for index in range(10)
    ]
    diverse_results = [
        _result(
            f"source-{index}",
            f"https://source-{index}.example/article",
            f"orchid{index} maple{index} quartz{index} lantern{index} river{index} summit{index}",
        )
        for index in range(10)
    ]

    seo_report = score_diversity(seo_variants)
    diverse_report = score_diversity(diverse_results)

    assert seo_report == score_diversity(seo_variants)
    assert 0.0 <= seo_report["score"] <= 1.0
    assert 0.0 <= diverse_report["score"] <= 1.0
    assert seo_report["score"] < diverse_report["score"] - 0.5
    assert seo_report["components"]["domain_diversity"] == 0.1
    assert seo_report["components"]["content_diversity"] == 0.0
    assert diverse_report["components"] == {
        "domain_diversity": 1.0,
        "url_duplication": 1.0,
        "content_diversity": 1.0,
        "provider_mix": 1.0,
    }
    assert seo_report["dominant_domain"] == {"domain": "seo.example", "share": 1.0}


def test_score_explains_duplicate_indices_and_provider_mix() -> None:
    results = [
        _result("first", "https://same.example/a?utm_source=one", "alpha beta gamma delta epsilon zeta", "one"),
        _result("url-duplicate", "https://same.example/a", "orchid maple quartz lantern river summit", "one"),
        _result("content-duplicate", "https://other.example/c", "alpha beta gamma delta epsilon eta", "one"),
        _result("second-provider", "https://third.example/d", "marble velvet cedar amber silver cobalt", "two"),
    ]
    report = score_diversity(results)

    assert report["duplicates"] == [
        {"kind": "url", "kept": 0, "dropped_candidate": 1},
        {"kind": "content", "kept": 0, "dropped_candidate": 2},
    ]
    assert score_diversity(results[:3])["components"]["provider_mix"] == 1.0
    assert 0.0 < report["components"]["provider_mix"] < 1.0


def test_config_defaults_and_validation_cover_diversity_settings() -> None:
    config = _validate_runtime_config(deepcopy(DEFAULT_CONFIG))
    assert config["quality"]["diversity"] == {
        "rerank": False,
        "near_duplicate_threshold": 0.6,
    }

    config["quality"] = {"diversity": {"rerank": True, "near_duplicate_threshold": 0.75}}
    assert _validate_runtime_config(config)["quality"]["diversity"] == {
        "rerank": True,
        "near_duplicate_threshold": 0.75,
    }

    invalid = deepcopy(DEFAULT_CONFIG)
    invalid["quality"]["diversity"]["near_duplicate_threshold"] = 1.1
    with pytest.raises(ValueError, match="quality.diversity.near_duplicate_threshold"):
        _validate_runtime_config(invalid)


def test_quality_report_contains_diversity_and_operator_receipt_stays_safe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = deepcopy(DEFAULT_CONFIG)
    config["tavily"]["api_key"] = "tavily-test-key-123456789"
    config["v3"] = {
        "state_path": str(tmp_path / "state.sqlite3"),
        "cache_dir": str(tmp_path),
        "operator_receipt_journal": False,
    }
    payload = _provider_payload(
        "tavily",
        [
            _result("one", "https://one.example/a", "alpha beta gamma delta epsilon zeta", "tavily"),
            _result("two", "https://two.example/b", "orchid maple quartz lantern river summit", "tavily"),
        ],
    )
    monkeypatch.setattr(providers, "search_tavily", lambda **_kwargs: payload)
    request = search.legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "diversity integration",
            "provider": "tavily",
            "count": 2,
            "quality_report": True,
            "no_cache": True,
        },
        request_id="diversity-integration",
    )

    execution = search.execute_v3_request(request, search._search_adapter(), config)
    public = search.v3_response_to_legacy_search(execution)
    assert "diversity" in public["quality_report"]
    assert public["quality_report"]["diversity"]["dominant_domain"] is not None

    operator_payload = receipt_record_from_response(execution.response, timestamp=1.0)
    assert privacy.assert_operator_payload_safe(operator_payload) is None
    assert "diversity" not in json.dumps(operator_payload, sort_keys=True)


def test_default_research_merge_is_reciprocal_rank_fusion() -> None:
    sources = _fixture_research_sources()
    expected, expected_dedup_count = research.fuse_results(
        [("alpha", sources["alpha"]), ("beta", sources["beta"])], 4
    )

    result = _run_fixture_research()
    assert result["results"] == expected
    assert result["metadata"]["dedup_count"] == expected_dedup_count == 1
    assert "diversity_reranked" not in result["metadata"]


def test_opt_in_research_rerank_stably_demotes_url_and_content_duplicates() -> None:
    first = _run_fixture_research(diversity_rerank=True)
    second = _run_fixture_research(diversity_rerank=True)

    assert [item["id"] for item in first["results"]] == ["a", "d", "b", "c"]
    assert first["results"] == second["results"]
    assert first["metadata"]["dedup_count"] == 0
    assert first["metadata"]["diversity_reranked"] == 2

    reranked, duplicates = rerank_duplicate_candidates(
        _fixture_research_sources()["alpha"]["results"]
        + _fixture_research_sources()["beta"]["results"]
    )
    assert [item["id"] for item in reranked] == ["a", "d", "b", "c"]
    assert duplicates == [
        {"kind": "content", "kept": 0, "dropped_candidate": 1},
        {"kind": "url", "kept": 0, "dropped_candidate": 2},
    ]


def test_v3_research_config_keeps_default_merge_and_opt_in_reranks(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = _fixture_research_sources()
    config = deepcopy(DEFAULT_CONFIG)
    config["auto_routing"]["provider_priority"] = ["tavily", "linkup"]
    config["tavily"]["api_key"] = "tavily-test-key-123456789"
    config["linkup"]["api_key"] = "linkup-test-key-123456789"
    config["quality"]["filter_spam"] = False
    config["quality"]["max_results_per_domain"] = 0
    config["v3"] = {
        "state_path": str(tmp_path / "state.sqlite3"),
        "cache_dir": str(tmp_path),
        "operator_receipt_journal": False,
    }
    routing = {
        "provider": "tavily",
        "confidence": 0.9,
        "confidence_level": "high",
        "reason": "diversity fixture",
        "routing_policy": "routing-v2",
        "top_signals": [],
        "scores": {"tavily": 1.0, "linkup": 0.9},
        "auto_allow_excluded": [],
        "analysis_summary": {"routing_class": "research"},
    }
    tavily_payload = {
        **sources["alpha"],
        "provider": "tavily",
        "results": [{**item, "provider": "tavily"} for item in sources["alpha"]["results"]],
    }
    linkup_payload = {
        **sources["beta"],
        "provider": "linkup",
        "results": [{**item, "provider": "linkup"} for item in sources["beta"]["results"]],
    }
    monkeypatch.setattr(search, "auto_route_provider", lambda *_args: routing)
    monkeypatch.setattr(providers, "search_tavily", lambda **_kwargs: tavily_payload)
    monkeypatch.setattr(providers, "search_linkup", lambda **_kwargs: linkup_payload)
    monkeypatch.setattr(
        search, "extract_plus", lambda **_kwargs: {"provider": None, "results": []}
    )

    request = search.legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "diversity configuration",
            "provider": "auto",
            "mode": "research",
            "count": 4,
            "quality_report": True,
            "no_cache": True,
        },
        request_id="diversity-research-default",
    )
    default_execution = search.execute_v3_request(
        request, search._search_adapter(), config
    )
    default_public = search.v3_response_to_legacy_search(default_execution)
    assert [item["id"] for item in default_public["results"]] == ["a", "b", "d"]

    config["quality"]["diversity"]["rerank"] = True
    rerank_request = search.legacy_request_to_v3(
        Capability.SEARCH,
        {
            "query": "diversity configuration",
            "provider": "auto",
            "mode": "research",
            "count": 4,
            "quality_report": True,
            "no_cache": True,
        },
        request_id="diversity-research-rerank",
    )
    rerank_execution = search.execute_v3_request(
        rerank_request, search._search_adapter(), config
    )
    reranked_public = search.v3_response_to_legacy_search(rerank_execution)
    assert [item["id"] for item in reranked_public["results"]] == ["a", "d", "b", "c"]
    assert reranked_public["metadata"]["diversity_reranked"] == 2
