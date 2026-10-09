"""Auto search ranks and reports with the routing it planned.

The v3 engine runs each provider attempt as a fixed-provider search, so the
core saw "explicit" routing: the intent reranker always got the class
"general" (no rules) and the quality report showed the fixed call's routing
instead of the auto-routing decision.
"""

from __future__ import annotations

from wsp_core import providers, search
from wsp_core.config import _deepcopy_default_config

BLOG = {"title": "CVE write-up", "url": "https://someblog.example/cve-2024-1", "snippet": "A blog post about the bug."}
NVD = {"title": "CVE-2024-1 Detail", "url": "https://nvd.nist.gov/vuln/detail/CVE-2024-1", "snippet": "NVD entry."}


def _setup(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-test-key-123456")
    monkeypatch.setattr(search, "auto_route_provider", lambda query, config: {
        "provider": "serper", "confidence": 0.3, "confidence_level": "low", "reason": "test",
        "routing_policy": "routing-v3", "scores": {"serper": 3.0}, "top_signals": [],
        "analysis_summary": {"routing_class": "security"},
    })
    monkeypatch.setattr(providers, "search_serper", lambda query, api_key, max_results=5, **_kw: {
        "provider": "serper", "query": query, "results": [dict(BLOG), dict(NVD)]})
    config = _deepcopy_default_config()
    config["auto_routing"]["provider_priority"] = ["serper"]
    return config


def test_auto_search_applies_the_intent_reranker_for_the_planned_class(monkeypatch):
    config = _setup(monkeypatch)

    result = search.run_search_request(query="CVE-2024-1 advisory", no_cache=True, config=config)

    assert [item["url"] for item in result["results"]] == [NVD["url"], BLOG["url"]]
    assert result["metadata"]["intent_rerank"]["routing_class"] == "security"


def test_quality_report_shows_the_auto_routing_decision(monkeypatch):
    config = _setup(monkeypatch)

    result = search.run_search_request(
        query="CVE-2024-1 advisory", no_cache=True, quality_report=True, config=config
    )
    report = result["quality_report"]

    assert report["routing_class"] == "security"
    assert report["confidence"] == "low"
    assert "low routing confidence" in report["extract_reasons"]
    assert report["authority_signals"]["canonical_top_result"] is True
    assert report["selected_provider"] == "serper"
