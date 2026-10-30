"""Budget the UTF-8 text Hermes sends to the model, through registered handlers.

The eight-hit fixture models the observed ~3.3 KB search: 4 KiB allows modest
layout growth, but fails on verbose diagnostics or duplicated payloads.
An English extract gets 16 KiB per URL: the default 15,000-character preview
plus ~1 KiB for the notice, title, URL and full-text pointer. Short pages get
6 KiB (the observed ~5 KB Python page plus framing); CJK gets 48 KiB because
each preview character needs three UTF-8 bytes. These are regression budgets
for fixed fixtures and default settings, not a global cap on user content.
"""

import copy
import json
from pathlib import Path

import pytest

from plugin_loader import load_plugin
from wsp_core.config import _deepcopy_default_config

plugin = load_plugin("wsp_plugin_test_hermes_output_budget")
SEARCH_FIXTURE = json.loads((Path(__file__).parent / "fixtures/hermes_search_eight.json").read_text())


class Context:
    def __init__(self):
        self.handlers = {}

    def register_tool(self, **kwargs):
        self.handlers[kwargs["name"]] = kwargs["handler"]


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    cfg = _deepcopy_default_config()
    cfg["bounded_context"]["cache_root"] = str(tmp_path)
    monkeypatch.setattr(plugin, "load_config", lambda: cfg)
    monkeypatch.setattr(plugin, "_legacy_env_files", lambda: [])
    monkeypatch.setattr(plugin, "store_web_text", lambda *a, **kw: {
        "path": str(tmp_path / "full-text.txt"), "stored": True, "stored_chars": 100000,
    })
    context = Context()
    plugin.register(context)
    return context.handlers


def test_eight_result_hermes_search_costs_at_most_four_kib(hermes, monkeypatch):
    answer = copy.deepcopy(SEARCH_FIXTURE)
    answer["observations"] = [{"provider_fields": {"raw_html": "internal provider material " * 500}}]
    answer["quality_report"] = {"internal_diagnostics": "internal ranking diagnostics " * 500}
    monkeypatch.setattr(plugin, "_run_search", lambda **kw: copy.deepcopy(answer))
    text = hermes["web_search_plus"]({"query": SEARCH_FIXTURE["query"], "count": 8})
    size = len(text.encode("utf-8"))
    assert size <= 4096, f"Hermes search grew to {size} bytes (budget 4096)"
    assert "8. Python tutorial" in text
    assert "invalid_urls_dropped=1" in text
    assert "internal provider material" not in text
    assert "internal ranking diagnostics" not in text
    for item in SEARCH_FIXTURE["results"]:
        assert item["url"] in text and item["snippet"] in text


@pytest.mark.parametrize("content,budget", [
    (("Python documentation, examples and release notes.\n" * 110)[:5000], 6 * 1024),
    ("Python reference and migration details.\n" * 3000, 16 * 1024),
    ("漢字と日本語の資料を参照してください。\n" * 5000, 48 * 1024),
])
@pytest.mark.parametrize("count", [1, 2])
def test_hermes_extract_byte_budget_per_url(hermes, monkeypatch, content, budget, count):
    urls = [f"https://docs.example.org/reference/{i}" for i in range(count)]
    monkeypatch.setattr(plugin, "_run_extract", lambda **kw: {
        "provider": "linkup", "results": [
            {"title": "Reference documentation", "url": url, "content": content}
            for url in urls
        ],
    })
    text = hermes["web_extract_plus"]({"urls": urls, "provider": "linkup"})
    size = len(text.encode("utf-8"))
    assert size <= count * budget, f"Hermes extract grew to {size} bytes ({count} URLs, {budget} each)"
    for url in urls:
        assert url in text
    if len(content) > 15000:
        assert "omitted middle" in text
        assert "read_file(path=" in text and "full-text.txt" in text
    else:
        assert content.strip() in text
