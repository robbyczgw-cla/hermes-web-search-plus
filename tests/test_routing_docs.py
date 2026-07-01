"""Drift and coverage checks for the generated Routing v2 reference."""

from pathlib import Path

import routing
from scripts import gen_routing_docs

ROUTING_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "ROUTING.md"


def test_checked_in_routing_docs_match_generator_output():
    expected = gen_routing_docs.render_routing_docs()
    checked_in = ROUTING_DOC_PATH.read_text(encoding="utf-8")

    assert checked_in == expected, (
        "docs/ROUTING.md is stale; regenerate with: python scripts/gen_routing_docs.py"
    )


def test_every_routing_class_is_documented():
    doc = ROUTING_DOC_PATH.read_text(encoding="utf-8")

    classes = {name for name, _ in routing.ROUTING_CLASS_RULES}
    classes.add(routing.MULTILINGUAL_ROUTING_CLASS)
    classes.add(routing.DEFAULT_ROUTING_CLASS)
    classes.update(routing.ROUTING_CLASS_PROVIDER_BOOSTS)

    for routing_class in sorted(classes):
        assert f"### `{routing_class}`" in doc, f"routing class {routing_class} missing from docs/ROUTING.md"
