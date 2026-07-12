from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SEARCH_PATH = Path(__file__).resolve().parents[1] / "search.py"
search_spec = importlib.util.spec_from_file_location(
    "wsp_search_perplexity_under_test", SEARCH_PATH
)
search = importlib.util.module_from_spec(search_spec)
assert search_spec.loader is not None
search_spec.loader.exec_module(search)


def test_perplexity_fails_before_network(monkeypatch):
    called = False

    def fake_request(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(search, "make_request", fake_request)
    with pytest.raises(ValueError, match="no_verified_source_only_endpoint"):
        search.search_perplexity(query="latest ai news", api_key="pplx-test-key")
    assert called is False


def test_kilo_perplexity_fails_before_network(monkeypatch):
    called = False

    def fake_request(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(search, "make_request", fake_request)
    with pytest.raises(ValueError, match="no_verified_source_only_endpoint"):
        search.search_perplexity(
            query="latest ai news",
            api_key="kilo-test-key",
            provider_name="kilo-perplexity",
        )
    assert called is False


def test_answer_only_providers_are_not_search_capabilities():
    assert "perplexity" not in search.SEARCH_PROVIDER_IDS
    assert "kilo-perplexity" not in search.SEARCH_PROVIDER_IDS
    assert search.PROVIDER_SPECS["perplexity"].supports_search is False
    assert search.PROVIDER_SPECS["kilo-perplexity"].supports_search is False
