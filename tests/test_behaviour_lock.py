"""Behaviour lock: refactors must reproduce the frozen routing and tool output.

tests/fixtures/lock/*.expected.jsonl were generated from the v4.3.5 tag with
synthetic provider answers (no network). A pull request that changes
behaviour on purpose regenerates them with
``python scripts/behaviour_lock/lock.py --update`` and the fixture diff becomes part
of its review.
"""

import json

from scripts.behaviour_lock import lock


def test_lock_inputs_cover_every_intent_and_tool():
    queries = [json.loads(line) for line in lock.ROUTE_SOURCES[0].read_text(encoding="utf-8").splitlines()]
    assert len(queries) >= 200
    assert {q["intent"] for q in queries} == {
        "news", "docs", "academic", "security", "shopping", "local", "community", "general",
    }
    cases = [json.loads(line) for line in lock.TOOL_CASES.read_text(encoding="utf-8").splitlines()]
    assert {case.get("tool", "web_search_plus") for case in cases} == {"web_search_plus", "web_extract_plus"}
    assert any((case.get("args") or {}).get("mode") == "research" for case in cases)


def test_routing_and_tool_output_match_the_lock():
    route_rows, tool_rows = lock.generate(lock.REPO)
    problems = lock.compare(lock.ROUTE_EXPECTED.read_text(encoding="utf-8").splitlines(), route_rows, "routing")
    problems += lock.compare(lock.TOOL_EXPECTED.read_text(encoding="utf-8").splitlines(), tool_rows, "tool")
    assert not problems, "\n".join(problems)
