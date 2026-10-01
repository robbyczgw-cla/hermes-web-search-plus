"""Legacy subprocess path: variadic values must not become CLI options."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import search

spec = importlib.util.spec_from_file_location(
    "subprocess_arg_plugin", Path(__file__).resolve().parents[1] / "__init__.py"
)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"results": []}), stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", run)
    return calls


BAD_DOMAIN_LISTS = [
    ["--clear-cache"],
    ["-x"],
    ["example.com", "--clear-cache"],
    ["example.com", "--provider=serper"],
    [""],
    ["   "],
    [123],
    [None],
    "--clear-cache",
]


@pytest.mark.parametrize("values", BAD_DOMAIN_LISTS)
@pytest.mark.parametrize("option", ["include_domains", "exclude_domains"])
def test_search_subprocess_rejects_option_like_domains_without_spawning(spawned, option, values):
    result = plugin._run_search_subprocess("query", provider="serper", **{option: values})
    assert spawned == []
    assert result["results"] == []
    assert result["error"]


BAD_URL_LISTS = [
    ["--clear-cache"],
    ["https://example.org", "--clear-cache"],
    ["https://example.org", "-h"],
    [""],
    [42],
]


@pytest.mark.parametrize("values", BAD_URL_LISTS)
def test_extract_subprocess_rejects_option_like_urls_without_spawning(spawned, values):
    result = plugin._run_extract_subprocess(values, provider="serper")
    assert spawned == []
    assert result["results"] == []
    assert result["error"]


def test_valid_variadic_values_still_reach_the_child_as_values(spawned):
    plugin._run_search_subprocess(
        "query",
        provider="serper",
        include_domains=["example.com", "docs.example.org"],
        exclude_domains=["spam.test"],
    )
    plugin._run_extract_subprocess(["https://example.org/a", "https://example.org/b"], provider="serper")
    assert len(spawned) == 2
    search_args = search.build_parser({}).parse_args(spawned[0][2:])
    assert search_args.include_domains == ["example.com", "docs.example.org"]
    assert search_args.exclude_domains == ["spam.test"]
    assert search_args.clear_cache is False
    extract_args = search.build_parser({}).parse_args(spawned[1][2:])
    assert extract_args.extract_urls == ["https://example.org/a", "https://example.org/b"]
    assert extract_args.clear_cache is False


def test_end_to_end_tool_call_does_not_clear_cache_via_legacy_path(monkeypatch, spawned):
    monkeypatch.setenv("WSP_FORCE_SUBPROCESS", "1")
    result = plugin._run_search_subprocess(
        "query", provider="serper", include_domains=["--clear-cache"]
    )
    assert spawned == []
    assert "domain" in result["error"].lower()
