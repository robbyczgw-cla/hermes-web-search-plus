"""Release-blocking source-only Charter gates for the live v2 execution surface."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import provider_dispatch
import providers
from provider_registry import PROVIDER_SPECS
from request_gate_v3 import validate_outbound_body, validate_provider_mode


ROOT = Path(__file__).resolve().parents[1]
BANNED_LABELS = {"answer", "synthesis", "reasoning", "claim", "verification"}


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location("wsp_charter_plugin", ROOT / "__init__.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_executable_provider_declares_source_only_semantics():
    for provider, spec in PROVIDER_SPECS.items():
        if spec.supports_search:
            assert spec.search_output_semantics == "source_results", provider
            assert validate_provider_mode(provider, "search") == "source_results"
        if spec.supports_extract:
            assert spec.extract_output_semantics == "source_text", provider
            assert validate_provider_mode(provider, "extract") == "source_text"


def test_no_provider_advertises_answer_or_synthesis_capability():
    offenders = {}
    for provider, spec in PROVIDER_SPECS.items():
        labels = set(spec.capability_labels) | set(spec.upstream_capabilities)
        if labels & BANNED_LABELS:
            offenders[provider] = sorted(labels & BANNED_LABELS)
    assert offenders == {}


def test_answer_only_providers_are_explicitly_rejected_and_not_dispatched():
    for provider in ("perplexity", "kilo-perplexity"):
        spec = PROVIDER_SPECS[provider]
        assert spec.supports_search is False
        assert spec.rejected_reason == "no_verified_source_only_endpoint"
        assert provider not in provider_dispatch.SEARCH_DISPATCH
        with pytest.raises(ValueError, match="no_verified_source_only_endpoint"):
            validate_provider_mode(provider, "search")


def test_formatter_cannot_render_legacy_answer_payload():
    plugin = _load_plugin_module()
    rendered = plugin._format_results(
        {
            "provider": "serper",
            "query": "q",
            "answer": "SYNTHETIC_ANSWER_SENTINEL",
            "results": [
                {
                    "title": "Source",
                    "url": "https://example.com",
                    "snippet": "SOURCE_SNIPPET_SENTINEL",
                }
            ],
        }
    )
    assert "Answer:" not in rendered
    assert "SYNTHETIC_ANSWER_SENTINEL" not in rendered
    assert "SOURCE_SNIPPET_SENTINEL" in rendered


def test_plugin_surface_exposes_only_search_and_extract_tools():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    tools = [
        line.strip().removeprefix("- ")
        for line in text.splitlines()
        if line.strip().startswith("- web_")
    ]
    assert tools
    assert all("search" in tool or "extract" in tool for tool in tools)
    assert not any(
        banned in tool
        for tool in tools
        for banned in ("answer", "verify", "watch", "synthesis")
    )


def test_tavily_requests_sources_without_answer(monkeypatch):
    captured = {}

    def fake_request(_endpoint, _headers, body, *args, **kwargs):
        captured["body"] = body
        return {"results": []}

    monkeypatch.setattr(providers, "make_request", fake_request)
    result = providers.search_tavily("charter probe", "tvly-test")

    assert captured["body"]["include_answer"] is False
    assert "answer" not in result


def test_linkup_is_search_results_only(monkeypatch):
    captured = {}

    def fake_request(_endpoint, _headers, body, *args, **kwargs):
        captured["body"] = body
        return {"results": []}

    monkeypatch.setattr(providers, "make_request", fake_request)
    result = providers.search_linkup("charter probe", "linkup-test")

    assert captured["body"]["outputType"] == "searchResults"
    assert "answer" not in result
    with pytest.raises(ValueError, match="outputType=searchResults"):
        providers.search_linkup(
            "charter probe", "linkup-test", output_type="sourcedAnswer"
        )


def test_exa_synthesis_depths_fail_before_network(monkeypatch):
    called = False

    def no_network(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network reached")

    monkeypatch.setattr(providers, "make_request", no_network)
    for depth in ("deep", "deep-reasoning"):
        with pytest.raises(ValueError, match="not source-only"):
            providers.search_exa("charter probe", "exa-test", exa_depth=depth)
    assert called is False


def test_perplexity_variants_fail_before_network(monkeypatch):
    called = False

    def no_network(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network reached")

    monkeypatch.setattr(providers, "make_request", no_network)
    for provider in ("perplexity", "kilo-perplexity"):
        with pytest.raises(ValueError, match="no_verified_source_only_endpoint"):
            providers.search_perplexity(
                "charter probe", "secret", provider_name=provider
            )
    assert called is False


def test_central_gate_rejects_answer_shaped_or_prompt_bodies():
    banned = (
        ("tavily", {"query": "q", "include_answer": True}),
        ("linkup", {"q": "q", "outputType": "sourcedAnswer"}),
        ("exa", {"query": "q", "type": "deep"}),
        ("exa", {"query": "q", "type": "deep-reasoning"}),
        ("any", {"messages": [{"role": "system", "content": "answer the user"}]}),
    )
    for provider, body in banned:
        with pytest.raises(ValueError):
            validate_outbound_body(provider, body)
    validate_outbound_body("tavily", {"query": "q", "include_answer": False})


def test_router_can_never_select_rejected_or_non_source_modes():
    from routing import iter_all_selectable_provider_modes

    banned = {
        provider
        for provider, spec in PROVIDER_SPECS.items()
        if not spec.supports_search
        or spec.rejected_reason
        or spec.search_output_semantics != "source_results"
    }
    selectable = set(iter_all_selectable_provider_modes())
    assert selectable
    assert selectable.isdisjoint(banned)


def test_source_providers_do_not_emit_answer_field(monkeypatch):
    monkeypatch.setattr(
        providers,
        "make_request",
        lambda *_args, **_kwargs: {
            "organic": [
                {
                    "title": "Source",
                    "link": "https://example.com",
                    "snippet": "SOURCE_ONLY_SENTINEL",
                }
            ],
            "answerBox": {"answer": "BANNED_ANSWER_SENTINEL"},
        },
    )
    result = providers.search_serper("charter probe", "serper-test")
    assert "answer" not in result
    assert result["results"][0]["snippet"] == "SOURCE_ONLY_SENTINEL"
