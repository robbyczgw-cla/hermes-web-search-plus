import importlib.util
from pathlib import Path
import search

import json
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("query_boundary_plugin", Path(__file__).resolve().parents[1] / "__init__.py")
assert spec is not None and spec.loader is not None
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)


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
