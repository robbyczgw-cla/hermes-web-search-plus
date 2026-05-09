from __future__ import annotations

import importlib.util
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("wsp_plugin_under_test", PLUGIN_PATH)
wsp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(wsp)


class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_detect_freshness_auto_maps_current_query_to_week():
    freshness = wsp._detect_answer_freshness("latest rsETH backing status this week", "auto")

    assert freshness["applied"] == "week"
    assert freshness["requested"] == "auto"
    assert "time-sensitive" in freshness["reason"]


def test_detect_locale_handles_common_non_english_eu_queries():
    locale = wsp._detect_answer_locale("meilleurs amplis hi-fi pas cher France", "auto", "auto")

    assert locale["language"] == "fr"
    assert locale["country"] == "FR"
    assert locale["language_confidence"] in {"medium", "high"}


def test_normalize_answer_sources_creates_citation_ready_records():
    sources = wsp._normalize_answer_sources([
        {
            "title": "Example Release Notes",
            "url": "https://docs.example.com/releases/v1",
            "snippet": "Release text",
            "date": "2026-05-08",
        }
    ])

    assert sources == [
        {
            "title": "Example Release Notes",
            "domain": "docs.example.com",
            "url": "https://docs.example.com/releases/v1",
            "published_date": "2026-05-08",
            "source_type": "docs",
            "provider": None,
            "extracted_status": "not_requested",
            "used_in_answer": True,
            "citation": "[Example Release Notes (docs.example.com, 2026-05-08)](https://docs.example.com/releases/v1)",
            "snippet": "Release text",
        }
    ]


def test_preferred_answer_extract_provider_prefers_linkup_then_auto(monkeypatch):
    for key in wsp._EXTRACT_PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    assert wsp._preferred_answer_extract_provider() is None

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    assert wsp._preferred_answer_extract_provider() == "auto"

    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    assert wsp._preferred_answer_extract_provider() == "linkup"


def test_answer_evidence_cleaner_strips_common_scrape_noise():
    raw = "# Title [Reload]() Skip to content ![logo](data:image/svg+xml;base64,abc) Tom &amp; Jerry <strong>bold</strong>"

    cleaned = wsp._clean_answer_evidence(raw)

    assert "Reload" not in cleaned
    assert "Skip to content" not in cleaned
    assert "data:image" not in cleaned
    assert "Tom & Jerry" in cleaned
    assert "bold" in cleaned


def test_web_answer_plus_tool_registered_with_simple_user_facing_schema():
    ctx = FakeCtx()

    wsp.register(ctx)

    schema = ctx.tools["web_answer_plus"]["schema"]
    props = schema["parameters"]["properties"]
    assert props["mode"]["enum"] == ["quick", "deep"]
    assert props["freshness"]["default"] == "auto"
    assert props["language"]["default"] == "auto"
    assert props["country"]["default"] == "auto"
    assert props["sources"]["default"] == 3
    assert props["max_extracts"]["maximum"] == 5
    assert "provider" not in props


def test_web_answer_plus_json_output_uses_quick_defaults_and_extracts_top_sources(monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    calls = {"search": [], "extract": []}

    def fake_search(**kwargs):
        calls["search"].append(kwargs)
        return {
            "provider": "brave",
            "results": [
                {"title": "Fresh Source", "url": "https://news.example/a", "snippet": "Fresh rsETH backing update", "date": "2026-05-08"},
                {"title": "Official Docs", "url": "https://docs.example/b", "snippet": "Official reserve note", "date": "2026-05-07"},
                {"title": "Forum Thread", "url": "https://forum.example/c", "snippet": "Community discussion"},
            ],
            "routing": {"provider": "brave"},
        }

    def fake_extract(**kwargs):
        calls["extract"].append(kwargs)
        return {
            "provider": "linkup",
            "results": [
                {"url": "https://news.example/a", "title": "Fresh Source", "content": "Detailed extracted source A."},
                {"url": "https://docs.example/b", "title": "Official Docs", "content": "Detailed extracted source B."},
            ],
        }

    monkeypatch.setattr(wsp, "_run_search", fake_search)
    monkeypatch.setattr(wsp, "_run_extract", fake_extract)
    ctx = FakeCtx()
    wsp.register(ctx)

    raw = ctx.tools["web_answer_plus"]["handler"]({
        "query": "latest rsETH backing status",
        "output": "json",
    })
    payload = wsp.json.loads(raw)

    assert payload["query"] == "latest rsETH backing status"
    assert payload["mode"] == "quick"
    assert payload["freshness"]["applied"] == "week"
    assert payload["confidence"] in {"medium", "high"}
    assert payload["confidence_reason"]["sources"] == 3
    assert len(payload["sources"]) == 3
    assert payload["sources"][0]["citation"].startswith("[Fresh Source (news.example, 2026-05-08)]")
    assert payload["answer"].startswith("Source-backed brief")
    assert "- [1] Fresh Source — Detailed extracted source A." in payload["answer"]
    assert not payload["answer"].lstrip().startswith("[1]")
    assert payload["cost_estimate"]["extracts_requested"] == 2
    assert calls["search"][0]["count"] == 3
    assert calls["search"][0]["time_range"] == "week"
    assert calls["search"][0]["language"] == "en"
    assert calls["extract"][0]["urls"] == ["https://news.example/a", "https://docs.example/b"]
    assert calls["extract"][0]["provider"] == "linkup"


def test_web_answer_plus_handler_modes_and_output_shapes(monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    calls = {"search": [], "extract": []}

    def fake_search(**kwargs):
        calls["search"].append(kwargs)
        return {
            "provider": "brave",
            "results": [
                {"title": f"Source {i}", "url": f"https://example.com/{i}", "snippet": f"Snippet {i}", "date": "2026-05-09"}
                for i in range(kwargs["count"])
            ],
        }

    def fake_extract(**kwargs):
        calls["extract"].append(kwargs)
        return {
            "provider": kwargs["provider"],
            "results": [
                {"url": url, "title": url, "content": f"Extracted {url}"}
                for url in kwargs["urls"]
            ],
        }

    monkeypatch.setattr(wsp, "_run_search", fake_search)
    monkeypatch.setattr(wsp, "_run_extract", fake_extract)
    ctx = FakeCtx()
    wsp.register(ctx)
    handler = ctx.tools["web_answer_plus"]["handler"]

    quick = wsp.json.loads(handler({"query": "quick topic", "mode": "quick", "output": "json"}))
    deep = wsp.json.loads(handler({"query": "deep topic", "mode": "deep", "output": "json"}))
    sources_only = handler({"query": "source topic", "output": "sources", "sources": 2, "max_extracts": 1})
    brief = handler({"query": "brief topic", "output": "brief", "freshness": "none", "language": "de", "country": "AT"})

    assert quick["mode"] == "quick"
    assert deep["mode"] == "deep"
    assert calls["search"][0]["mode"] == "normal"
    assert calls["search"][0]["count"] == 3
    assert calls["search"][1]["mode"] == "research"
    assert calls["search"][1]["count"] == 6
    assert calls["search"][3]["language"] == "de"
    assert calls["search"][3]["country"] == "AT"
    assert calls["extract"][0]["provider"] == "linkup"
    assert len(calls["extract"][0]["urls"]) == 2
    assert len(calls["extract"][1]["urls"]) == 2
    assert sources_only.startswith("- [Source 0")
    assert "**Answer**" in brief
    assert "**Freshness:** none" in brief


def test_web_answer_plus_caps_extracts_and_reports_cost(monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    calls = {"extract": []}

    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {
        "provider": "brave",
        "results": [
            {"title": f"Source {i}", "url": f"https://example.com/{i}", "snippet": "Snippet"}
            for i in range(10)
        ],
    })

    def fake_extract(**kwargs):
        calls["extract"].append(kwargs)
        return {"provider": "linkup", "results": []}

    monkeypatch.setattr(wsp, "_run_extract", fake_extract)

    payload = wsp._compose_answer_payload(
        query="deep topic",
        mode="deep",
        sources=10,
        max_extracts=9,
    )

    assert len(calls["extract"][0]["urls"]) == 5
    assert payload["cost_estimate"] == {
        "extract_provider": "linkup",
        "extracts_requested": 5,
        "approx_eur": 0.005,
    }
    assert any("max_extracts capped" in warning for warning in payload["warnings"])


def test_web_answer_plus_reports_actual_fallback_extractor_cost_model(monkeypatch):
    monkeypatch.delenv("LINKUP_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {
        "provider": "brave",
        "results": [{"title": "A", "url": "https://example.com/a", "snippet": "Snippet A"}],
    })
    monkeypatch.setattr(wsp, "_run_extract", lambda **kwargs: {
        "provider": "tavily",
        "results": [{"url": "https://example.com/a", "title": "A", "content": "Extracted A"}],
    })

    payload = wsp._compose_answer_payload(query="fallback extractor topic", sources=1, max_extracts=1)

    assert payload["cost_estimate"] == {
        "extract_provider": "tavily",
        "extracts_requested": 1,
        "approx_eur": None,
    }


def test_web_answer_plus_marks_failed_extractions(monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {
        "provider": "brave",
        "results": [
            {"title": "A", "url": "https://example.com/a", "snippet": "Snippet A"},
            {"title": "B", "url": "https://example.com/b", "snippet": "Snippet B"},
        ],
    })
    monkeypatch.setattr(wsp, "_run_extract", lambda **kwargs: {
        "provider": "linkup",
        "error": "quota exceeded",
        "results": [],
    })

    payload = wsp._compose_answer_payload(query="current thing", sources=2, max_extracts=2)

    assert [source["extracted_status"] for source in payload["sources"]] == ["failed", "failed"]
    assert any("quota exceeded" in warning for warning in payload["warnings"])
    assert payload["confidence"] == "medium"


def test_web_answer_plus_empty_result_is_explicit_and_query_scoped(monkeypatch):
    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {"provider": "brave", "results": []})

    payload = wsp._compose_answer_payload(query="obscure impossible thing")

    assert payload["answer"] == "No usable sources for: obscure impossible thing"
    assert payload["confidence"] == "low"
    assert any("Only 0 citation-ready sources" in warning for warning in payload["warnings"])


def test_web_answer_plus_degrades_to_snippets_without_extract_provider(monkeypatch):
    for key in wsp._EXTRACT_PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    calls = {"extract": 0}

    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {
        "provider": "brave",
        "results": [
            {"title": "Snippet Source", "url": "https://example.com/a", "snippet": "Readable snippet evidence"},
        ],
    })

    def fake_extract(**kwargs):
        calls["extract"] += 1
        return {"provider": "linkup", "results": []}

    monkeypatch.setattr(wsp, "_run_extract", fake_extract)

    payload = wsp._compose_answer_payload(query="snippet-only topic", sources=1, max_extracts=1)

    assert calls["extract"] == 0
    assert payload["cost_estimate"]["extract_provider"] is None
    assert "Readable snippet evidence" in payload["answer"]
    assert any("no extraction-capable provider configured" in warning for warning in payload["warnings"])


def test_web_answer_plus_skips_extraction_when_global_budget_is_exhausted(monkeypatch):
    monkeypatch.setenv("LINKUP_API_KEY", "lk-test")
    calls = {"extract": 0}

    class FakeClock:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return 0.0 if self.calls == 1 else 25.0

    monkeypatch.setattr(wsp.time, "monotonic", FakeClock())
    monkeypatch.setattr(wsp, "_run_search", lambda **kwargs: {
        "provider": "brave",
        "results": [{"title": "A", "url": "https://example.com/a", "snippet": "Snippet A"}],
    })

    def fake_extract(**kwargs):
        calls["extract"] += 1
        return {"provider": "linkup", "results": []}

    monkeypatch.setattr(wsp, "_run_extract", fake_extract)

    payload = wsp._compose_answer_payload(query="current thing", sources=1, max_extracts=1)

    assert calls["extract"] == 0
    assert any("wall-clock budget exhausted" in warning for warning in payload["warnings"])
