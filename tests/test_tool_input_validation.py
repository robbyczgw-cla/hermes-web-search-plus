"""Search tool input is cleaned before any provider is called."""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def plugin():
    pkg_parent = ROOT.parent
    if str(pkg_parent) not in sys.path:
        sys.path.insert(0, str(pkg_parent))
    return importlib.import_module(ROOT.name)


@pytest.mark.parametrize("value,expected", [
    (0, 1), (-3, 1), (1, 1), (7, 7), ("7", 7), (100, None), ("abc", 5), (None, 5), (True, 5), (2.9, 2),
    (float("inf"), 5), (float("-inf"), 5), (float("nan"), 5), (10**400, None), ("1e999", 5),
])
def test_count_is_clamped(plugin, value, expected):
    result = plugin._clean_tool_count(value)
    assert result == (plugin._MAX_TOOL_COUNT if expected is None else expected)


@pytest.mark.parametrize("value,expected", [
    ("BRAVE", "brave"), (" Serper ", "serper"), ("auto", "auto"), ("AUTO", "auto"), (None, "auto"), ("", "auto"),
])
def test_provider_name_is_case_insensitive(plugin, value, expected):
    assert plugin._clean_tool_provider(value) == (expected, None)


def test_unknown_provider_names_valid_ones(plugin):
    provider, error = plugin._clean_tool_provider("google")
    assert error.startswith("Search error: unknown provider 'google'")
    assert "brave" in error and "auto" in error


def test_overlong_query_is_capped_before_search(plugin, monkeypatch):
    seen = {}
    monkeypatch.setattr(plugin, "_run_search", lambda **kw: seen.update(kw) or {"results": []})
    monkeypatch.setattr(plugin, "_format_results", lambda data: "ok")
    ctx = type("C", (), {"tools": {}, "register_tool": lambda self, **kw: self.tools.__setitem__(kw["name"], kw)})()
    try:
        plugin.register(ctx)
    except Exception:
        pytest.skip("register needs host context")
    ctx.tools["web_search_plus"]["handler"]({"query": "x" * 50_000})
    assert len(seen["query"]) == plugin._MAX_TOOL_QUERY_CHARS
