"""Drift and coverage checks for the generated routing reference (docs/ROUTING.md)."""

import copy
import re
import sys
from pathlib import Path

import pytest

from scripts import gen_routing_docs
from wsp_core import cache, intents, provider_registry, routing
from wsp_core.config import DEFAULT_CONFIG, _replace_pre_5_default_priority
from wsp_core.quality import CANONICAL_DOMAIN_RULES

ROOT = Path(__file__).resolve().parents[1]
ROUTING_DOC_PATH = ROOT / "docs" / "ROUTING.md"
RESULTS_PATH = ROOT / "benchmarks" / "RESULTS.md"


def _doc() -> str:
    return ROUTING_DOC_PATH.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def _all_measured_configured(monkeypatch):
    monkeypatch.setattr(
        routing, "provider_configured", lambda provider, config=None: provider in routing.MEASURED_PROVIDER_ORDER
    )


def test_checked_in_routing_docs_match_generator_output():
    expected = gen_routing_docs.render_routing_docs()

    assert _doc() == expected, (
        "docs/ROUTING.md is stale; regenerate with: python scripts/gen_routing_docs.py"
    )


def test_check_mode_exits_1_on_drift(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ROUTING.md"
    target.write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gen_routing_docs.py", "--check", "--out", str(target)])

    assert gen_routing_docs.main() == 1
    assert "DRIFT" in capsys.readouterr().err

    target.write_text(gen_routing_docs.render_routing_docs(), encoding="utf-8")
    assert gen_routing_docs.main() == 0


def test_every_intent_is_documented():
    doc = _doc()
    section = _between(doc, "## The eight intents", "## Authority domains")

    for intent in intents.INTENTS:
        assert f"### `{intent}`" in section, f"intent {intent} has no section in docs/ROUTING.md"
        assert re.search(rf"^\| `{intent}` \| `\w+` \|", doc, re.MULTILINE), (
            f"intent {intent} is missing from the first-provider table"
        )


def test_every_first_provider_rule_and_the_measured_order_appear():
    doc = _doc()

    for intent, provider in routing.INTENT_FIRST_PROVIDER.items():
        assert f"| `{intent}` | `{provider}` | intent rule |" in doc
    measured = ", ".join(f"`{provider}`" for provider in routing.MEASURED_PROVIDER_ORDER)
    assert f"The measured order is {measured}" in doc
    for provider in routing.MEASURED_PROVIDER_ORDER:
        assert f"`{provider}`" in doc


def test_every_cue_is_listed_under_its_intent():
    section = _between(_doc(), "## The eight intents", "## Authority domains")

    for intent in intents.INTENTS:
        start = section.index(f"### `{intent}`")
        following = section.find("\n### ", start + 1)
        text = section[start:] if following == -1 else section[start:following]
        for spec in intents._SPECS:
            if spec.intent == intent:
                assert f"`{spec.name}`" in text, f"cue {intent}:{spec.name} missing from its section"


def test_authority_domains_and_cache_caps_appear():
    doc = _doc()
    section = _between(doc, "## Authority domains", "## Cache lifetime")

    for intent, rules in CANONICAL_DOMAIN_RULES.items():
        assert f"### `{intent}`" in section
        for domain in [*rules.get("boost", []), *rules.get("demote", [])]:
            assert f"`{domain}`" in section, f"{intent} domain {domain} missing"
    for intent, ttl in cache.CLASS_CACHE_TTL.items():
        assert f"| `{intent}` | {ttl} seconds |" in doc
    assert f"{cache.DEFAULT_CACHE_TTL} seconds by default" in doc


def test_config_keys_and_inspection_command_appear():
    doc = _doc()

    for key in (
        "auto_routing.enabled", "default_provider", "auto_routing.provider_priority",
        "auto_routing.disabled_providers", "auto_routing.auto_allow",
        "auto_routing.fallback_provider", "auto_routing.confidence_threshold",
        "auto_routing.adaptive_routing",
    ):
        assert re.search(rf"^\| `{re.escape(key)}` \|", doc, re.MULTILINE), f"config key {key} missing"
    assert 'python search.py --explain-routing -q "your query"' in doc
    for legacy in provider_registry.PRE_5_DEFAULT_PROVIDER_PRIORITY:
        assert f"`{legacy}`" in _between(doc, "#### Migration from the 4.x default", "## The eight intents")


def test_relative_links_and_anchors_resolve():
    doc = _doc()
    own_anchors = {gen_routing_docs.anchor(m) for m in re.findall(r"^#{2,4} (.+)$", doc, re.MULTILINE)}

    for target in re.findall(r"\]\(([^)\s]+)\)", doc):
        if target.startswith("http"):
            continue
        path, _, fragment = target.partition("#")
        if not path:
            assert fragment in own_anchors, f"anchor #{fragment} has no heading in docs/ROUTING.md"
            continue
        linked = (ROUTING_DOC_PATH.parent / path).resolve()
        assert linked.exists(), f"link target {path} does not exist"
        if fragment and linked.suffix == ".md":
            headings = re.findall(r"^#{1,6} (.+)$", linked.read_text(encoding="utf-8"), re.MULTILINE)
            assert fragment in {gen_routing_docs.anchor(h) for h in headings}, f"{path}#{fragment} has no heading"
    assert "## Routing 5.0 (intent table)" in RESULTS_PATH.read_text(encoding="utf-8")


def test_generator_refuses_intent_descriptions_that_disagree_with_the_code(monkeypatch):
    monkeypatch.delitem(gen_routing_docs.INTENT_DESCRIPTIONS, "news")
    with pytest.raises(SystemExit, match="INTENT_DESCRIPTIONS out of sync"):
        gen_routing_docs.render_routing_docs()


def test_generator_refuses_stale_intent_descriptions(monkeypatch):
    monkeypatch.setitem(gen_routing_docs.INTENT_DESCRIPTIONS, "retired_intent", "gone")
    with pytest.raises(SystemExit, match="retired_intent"):
        gen_routing_docs.render_routing_docs()


def test_generator_refuses_rules_for_unknown_intents(monkeypatch):
    monkeypatch.setitem(routing.INTENT_FIRST_PROVIDER, "retired_intent", "exa")
    with pytest.raises(SystemExit, match="retired_intent"):
        gen_routing_docs.render_routing_docs()


# --- the documented behavior, checked against the router itself -----------------


def test_documented_first_provider_matches_the_router(monkeypatch):
    _all_measured_configured(monkeypatch)

    for query in gen_routing_docs.EXAMPLE_QUERIES:
        intent = intents.classify_intent(query).intent
        routed = routing.route_query(query, copy.deepcopy(DEFAULT_CONFIG))
        assert routed["provider"] == gen_routing_docs._first_provider(intent), query
        assert routed["candidate_order"][0] == routed["provider"]


def test_provider_priority_does_not_change_the_first_provider(monkeypatch):
    _all_measured_configured(monkeypatch)
    config = copy.deepcopy(DEFAULT_CONFIG)
    baseline = routing.route_query("history of the Roman Empire", config)["provider"]
    config["auto_routing"]["provider_priority"] = ["tavily", "exa", "serper", "brave"]

    assert routing.route_query("history of the Roman Empire", config)["provider"] == baseline == "brave"


def test_ignored_config_keys_do_not_change_the_decision(monkeypatch):
    _all_measured_configured(monkeypatch)
    baseline = routing.route_query("how to read a file in python", copy.deepcopy(DEFAULT_CONFIG))
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["auto_routing"]["confidence_threshold"] = 0.99
    config["auto_routing"]["adaptive_routing"] = False

    assert routing.route_query("how to read a file in python", config) == baseline


def test_pre_5_default_priority_is_replaced_and_other_lists_are_kept():
    legacy = list(provider_registry.PRE_5_DEFAULT_PROVIDER_PRIORITY)
    current = list(DEFAULT_CONFIG["auto_routing"]["provider_priority"])

    assert _replace_pre_5_default_priority(legacy) == current
    assert _replace_pre_5_default_priority([*legacy, "extra"]) == [*current, "extra"]
    assert _replace_pre_5_default_priority(legacy[:-1]) == legacy[:-1]
    assert _replace_pre_5_default_priority(["tavily", "exa"]) == ["tavily", "exa"]


def test_documented_extra_cue_fires():
    decision = intents.classify_intent("restaurants in Vienna")

    assert decision.intent == "local" and "local:place_name" in decision.signals
    assert [name for name, _, _ in gen_routing_docs.EXTRA_CUES["local"]] == ["place_name"]
