from wsp_core import search

import json
from types import SimpleNamespace

import pytest

from plugin_loader import load_plugin
boundary = load_plugin("query_boundary_plugin")


@pytest.mark.parametrize("text", ["--help", "-site:reddit.com", "--", "", "normal query"])
@pytest.mark.parametrize("capability", ["search", "extract"])
def test_subprocess_arguments_preserve_free_text(text, capability, monkeypatch):
    seen = []

    def run(cmd, **kwargs):
        args = search.build_parser({}).parse_args(cmd[2:])
        seen.append(args.query if capability == "search" else args.spans_query)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"results": [], "query": text}), stderr="")

    monkeypatch.setattr(boundary.subprocess, "run", run)
    if capability == "search":
        boundary._run_search_subprocess(text, provider="serper")
    else:
        boundary._run_extract_subprocess(["https://example.org"], provider="serper", spans=True, spans_query=text)
    assert seen == [text]
