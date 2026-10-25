from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from wsp_core import provider_registry


def _module():
    spec = provider_registry.PROVIDER_SPECS["donsetch"]
    return spec.execute_search.__globals__


# A minimal JSON-RPC server: every request line gets one structured reply.
_SERVER = """
import json
import sys

sys.stdin.reconfigure(encoding="utf-8", errors="replace")
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if message.get("id") is None:
        continue
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {"content": [], "structuredContent": {"ok": True}},
    }), flush=True)
"""


def test_mcp_session_reads_responses_without_select(tmp_path, monkeypatch):
    # select() cannot wait on a stdio pipe on Windows (WinError 10038), so
    # the session must read replies through its own reader thread instead.
    module = _module()

    def _raise(*_args, **_kwargs):
        raise OSError("simulated WinError 10038: select() on a pipe")

    monkeypatch.setitem(module, "select", SimpleNamespace(select=_raise))
    server = tmp_path / "mcp"
    server.write_text(_SERVER, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    session_class = module["DonsetchSession"]
    with session_class(sys.executable, 30) as session:
        first = session.call("web_search", {"query": "first"})
        second = session.call("web_fetch", {"url": "https://example.org/"})

    assert first["structured"]["ok"] is True
    assert second["structured"]["ok"] is True


def test_mcp_session_reports_a_dead_child_process(tmp_path, monkeypatch):
    # A child that exits before answering must surface as a named failure,
    # not hang until the deadline.
    module = _module()
    server = tmp_path / "mcp"
    server.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    session_class = module["DonsetchSession"]
    with pytest.raises(RuntimeError, match=r"donsetch_(mcp_failed|process_failed)"):
        with session_class(sys.executable, 30):
            pass