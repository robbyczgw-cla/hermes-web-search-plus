"""A2: the extract footer reports the real length and points at the real full text."""
from __future__ import annotations

import re
from unittest import mock


from wsp_core import search
from wsp_core.bounded_context_v3 import FullTextStore

from plugin_loader import load_plugin
from test_v302_entrypoint_regressions import _runtime_config

plugin = load_plugin("wsp_plugin_test_a2_full_text_pointer")


def _page(label: str, chars: int) -> str:
    lines = []
    total = 0
    n = 0
    while total < chars:
        n += 1
        line = f"{label} line {n:06d} " + "x" * 40
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)[:chars]


def _extract(monkeypatch, config, pages):
    def fake_core(**_kwargs):
        return {
            "provider": "linkup",
            "results": [
                {"title": f"T{i}", "url": url, "content": text}
                for i, (url, text) in enumerate(pages.items())
            ],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(search._extract, "_extract_plus_core", fake_core)
    return search.run_extract_request(list(pages), provider="linkup", config=config)


def _format(data):
    with mock.patch.object(plugin, "load_config", return_value={"web": {"extract_char_limit": 15000}}):
        return plugin._format_extract_results(data)


def _stored_file_text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    header, _, text = raw.partition("\n")
    assert header.startswith("<!-- wsp:web_text_v3 ")
    return text


def test_long_page_reports_real_length_and_points_at_full_text(tmp_path, monkeypatch):
    url = "https://example.com/graz"
    page = _page("graz", 186_802)
    output = _format(_extract(monkeypatch, _runtime_config(tmp_path), {url: page}))

    assert "original 186802 chars" in output
    assert "original 60000 chars" not in output
    path = re.search(r"Full cleaned text stored at: (\S+)", output).group(1)
    assert path.startswith(str(tmp_path / "web" / "v3"))
    stored = _stored_file_text(path)
    assert len(stored) == 186_802
    assert stored == page

    # The first read_file offset lands on the line where the shown head was cut.
    offset = int(re.search(r"offset=(\d+), limit=500", output).group(1))
    with open(path, encoding="utf-8") as handle:
        file_lines = handle.read().split("\n")
    head = output.split("\n\n[... omitted middle")[0].split("\n", 3)[-1]
    head_last_line = head.rstrip().split("\n")[-1]
    assert file_lines[offset - 1].startswith(head_last_line)

    # A continuation offset is given for the part after the shown tail, and it
    # lies beyond the omitted middle but inside the file.
    offsets = [int(m) for m in re.findall(r"offset=(\d+), limit=500", output)]
    assert offsets[0] < offsets[1] <= len(file_lines)


def test_three_url_batch_reports_each_real_length_and_file(tmp_path, monkeypatch):
    pages = {
        "https://example.com/a": _page("a", 100_000),
        "https://example.com/b": _page("b", 150_000),
        "https://example.com/c": _page("c", 90_000),
    }
    data = _extract(monkeypatch, _runtime_config(tmp_path), pages)
    output = _format(data)

    sections = re.split(r"\n\d+\. T\d", output)[1:]
    assert len(sections) == 3
    for section, (url, page) in zip(sections, pages.items()):
        assert f"original {len(page)} chars" in section
        path = re.search(r"Full cleaned text stored at: (\S+)", section).group(1)
        assert _stored_file_text(path) == page
    paths = {
        r["full_text"]["path"] for r in data["results"]
    }
    assert len(paths) == 3
    assert all(r["full_text"]["stored"] for r in data["results"])


def test_untruncated_page_still_uses_url_keyed_store(tmp_path, monkeypatch):
    url = "https://example.com/mid"
    page = _page("mid", 30_000)  # over the 15000 inline limit, under the 60000 budget
    data = _extract(monkeypatch, _runtime_config(tmp_path), {url: page})
    assert "full_text" not in data["results"][0]
    output = _format(data)
    assert "original 30000 chars" in output
    path = re.search(r"Full cleaned text stored at: (\S+)", output).group(1)
    assert open(path, encoding="utf-8").read() == page


def test_unstored_full_text_is_reported_honestly(tmp_path, monkeypatch):
    config = _runtime_config(tmp_path)
    config["bounded_context"]["full_text_max_bytes"] = 0
    url = "https://example.com/nostore"
    output = _format(_extract(monkeypatch, config, {url: _page("n", 100_000)}))

    assert "Full cleaned text stored" not in output
    assert "read_file(" not in output
    assert "only the shown part" in output
    assert "original 60000 chars" not in output
    assert "original length unknown" in output
    assert not list((tmp_path / "web").glob("*.md"))


def test_missing_stored_file_is_not_advertised(tmp_path, monkeypatch):
    config = _runtime_config(tmp_path)
    url = "https://example.com/gone"
    first = _extract(monkeypatch, config, {url: _page("g", 100_000)})
    path = first["results"][0]["full_text"]["path"]
    # Same content, cache hit, but the file vanished in between.
    import os

    os.unlink(path)
    again = _extract(monkeypatch, config, {url: _page("g", 100_000)})
    info = again["results"][0]["full_text"]
    assert info["stored"] is False
    output = _format(again)
    assert "Full cleaned text stored" not in output
    assert "only the shown part" in output


def test_tampered_stored_file_fails_integrity_check(tmp_path, monkeypatch):
    config = _runtime_config(tmp_path)
    url = "https://example.com/tamper"
    first = _extract(monkeypatch, config, {url: _page("t", 100_000)})
    path = first["results"][0]["full_text"]["path"]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("extra")
    again = _extract(monkeypatch, config, {url: _page("t", 100_000)})
    assert again["results"][0]["full_text"]["stored"] is False


def test_crlf_page_roundtrips_through_store_and_pointer(tmp_path, monkeypatch):
    store = FullTextStore(tmp_path)
    text = "one\r\ntwo\r\nthree\rfour\n"
    key = store.store("https://example.com/crlf", text)["reference"]["key"]
    assert store.lookup(key) == text

    url = "https://example.com/crlf-big"
    page = "\r\n".join(f"row {i:06d} " + "y" * 40 for i in range(3000))
    assert len(page) > 60_000
    data = _extract(monkeypatch, _runtime_config(tmp_path), {url: page})
    info = data["results"][0]["full_text"]
    assert info["stored"] is True
    assert info["original_chars"] == len(page)
    assert _stored_file_text(info["path"]) == page


def test_research_summary_reports_real_page_length():
    data = {
        "provider": "research",
        "query": "graz",
        "results": [],
        "source_summaries": [
            {
                "url": "https://a.test/x",
                "content": "graz " * 1000,
                "passages": [{"text": "graz graz", "start": 0, "end": 9}],
                "full_text": {"truncated": True, "original_chars": 186_802, "stored": False},
            },
            {
                "url": "https://a.test/y",
                "content": "plain " * 1000,
                "full_text": {"truncated": True, "original_chars": 150_000, "stored": False},
            },
        ],
    }
    output = plugin._format_results(data)
    assert "of 186802 characters" in output
    assert "of 150000 characters" in output
    assert "of 5000 characters" not in output


def test_native_backend_marks_budget_truncation_and_never_claims_full(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType

    abc = ModuleType("agent.web_search_provider")
    abc.WebSearchProvider = object
    web = ModuleType("tools.web_tools")
    web._load_web_config = lambda: {"backend": "wsp"}
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", abc)
    monkeypatch.setitem(sys.modules, "tools.web_tools", web)
    spec = importlib.util.spec_from_file_location(
        "_wsp_native_a2", Path(__file__).resolve().parents[1] / "native_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = SimpleNamespace(
        load_config=lambda: {"auto_routing": {}},
        provider_configured=lambda *args: True,
        SEARCH_PROVIDER_IDS=("fixture",), EXTRACT_PROVIDER_IDS=("fixture",),
        _provider_auto_allowed=lambda *args: True,
    )
    url = "https://example.org/big"
    row = {
        "url": url, "title": "Big", "content": "x" * 60_000,
        "full_text": {"truncated": True, "original_chars": 186_802, "stored": True, "path": "/tmp/f.md"},
    }
    plugin_stub = SimpleNamespace(
        _load_search_module=lambda: engine,
        _run_extract=Mock(return_value={"results": [row]}),
    )
    backend = module.WSPNativeBackend(plugin_stub)
    result = backend.extract([url])[0]
    assert result["metadata"]["truncated"] is True
    assert result["metadata"]["original_content_length"] == 186_802
    assert result["metadata"]["full_text_path"] == "/tmp/f.md"
    assert len(result["content"]) == 60_000
