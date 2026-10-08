"""WSP 3.1 semantic span extraction contract tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unicodedata

import pytest

from wsp_core import extract
from wsp_core.bounded_context_v3 import apply_bounded_context, prepare_extract_request
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from wsp_core.orchestrator_v3 import ProviderPlan
from wsp_core.runtime_v3 import observations_from_legacy, project_results_from_observations
from wsp_core.span_extraction_v3 import nfc_text, select_spans


TRICKY_TEXTS = [
    "Cafe\u0301 opens early. Straße and decomposed u\u0308 are here.",
    "Emoji family 👩\u200d👩\u200d👧\u200d👦 stays one grapheme but several codepoints. Next sentence.",
    "First line\r\nSecond line.\r\n\r\nA new paragraph follows.",
    "NBSP\u00a0separates these words. Ordinary spaces follow.",
    "Fu\u0308r Wien gilt das. Schönes NFC follows.",
]


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def store(self, url: str, text: str) -> dict:
        self.calls.append((url, text))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {
            "storage_attempted": True,
            "storage_succeeded": True,
            "reference": {
                "store": "web_text_v3",
                "key": digest,
                "media_type": "text/markdown",
            },
            "full_text_sha256": digest,
            "full_text_chars": len(text),
        }


def _response(text: str) -> ResponseV3:
    raw = [{"url": "https://example.test/doc", "title": "Doc", "content": text}]
    observations = observations_from_legacy(
        {"results": raw}, "fixture", Capability.EXTRACT, "attempt_spans"
    )
    return ResponseV3(
        request_id="req_spans",
        execution_id="exec_spans",
        capability=Capability.EXTRACT,
        status=ResponseStatus.OK,
        results=project_results_from_observations(observations, raw),
        observations=observations,
        policy_actions=[],
        provider_attempts=[],
        routing_receipt={
            "policy_id": "classic",
            "policy_revision": "fixture",
            "mode": "classic",
            "candidate_order": ["fixture"],
            "selected_provider": "fixture",
            "fallback_reason": "none",
        },
        cache_status={"disposition": "miss"},
    )


@pytest.mark.parametrize("text", TRICKY_TEXTS)
def test_offset_invariants_are_codepoint_based_and_deterministic(text: str) -> None:
    normalized = nfc_text(text)
    first = select_spans(text, "Wien emoji paragraph words", max_spans=3)
    second = select_spans(text, "Wien emoji paragraph words", max_spans=3)

    assert first == second
    previous_end = 0
    for span in first:
        assert 0 <= span["start"] < span["end"] <= len(normalized)
        assert span["text"] == normalized[span["start"]:span["end"]]
        assert span["start"] >= previous_end
        previous_end = span["end"]


def test_decomposed_offsets_address_nfc_not_raw_input() -> None:
    raw = "Start. The city of Du\u0308sseldorf has museums. End."
    normalized = unicodedata.normalize("NFC", raw)
    [span] = select_spans(raw, "Düsseldorf museums", max_spans=1)

    assert span["text"] == normalized[span["start"]:span["end"]]
    assert len(normalized) == len(raw) - 1
    # Offsets after the composition point are intentionally not raw-string offsets.
    assert raw[span["start"]:span["end"]] != span["text"]


def test_query_conditioning_selects_different_passages() -> None:
    document = (
        "Astronomy notes describe telescope mirrors and distant galaxies.\n\n"
        "Cooking notes explain sourdough starter and bread fermentation."
    )

    astronomy = select_spans(document, "telescope galaxies", max_spans=1)
    cooking = select_spans(document, "sourdough fermentation", max_spans=1)

    assert astronomy != cooking
    assert "telescope" in astronomy[0]["text"]
    assert "sourdough" in cooking[0]["text"]


def test_custom_ranker_seam_scores_candidate_text_and_query() -> None:
    calls = []

    def ranker(candidate: str, query: str) -> float:
        calls.append((candidate, query))
        return 10.0 if "second" in candidate.casefold() else 0.0

    spans = select_spans("First sentence. Second sentence.", "pick", max_spans=1, ranker=ranker)

    assert calls
    assert all(query == "pick" for _, query in calls)
    assert "Second" in spans[0]["text"]


def test_query_matching_heading_keeps_its_body_without_query_terms() -> None:
    document = (
        "# Alpine Reservoirs\n\n"
        "This body deliberately uses only neutral prose and measurement details.\n\n"
        "# Other Notes\n\n"
        "Unrelated material follows."
    )

    [span] = select_spans(document, "alpine reservoirs", max_spans=1)
    span_text = str(span["text"])

    assert span_text.startswith("# Alpine Reservoirs")
    assert "neutral prose and measurement details" in span_text
    assert "Other Notes" not in span_text


def test_heading_section_stops_before_parent_or_sibling_section() -> None:
    document = (
        "# Parent Background\n\n"
        "Parent-only context stays outside the target subsection.\n\n"
        "## Orbital Calibration\n\n"
        "Neutral calibration prose deliberately omits the query vocabulary.\n\n"
        "# Sibling Appendix\n\n"
        "Sibling-only context must never leak in."
    )

    [span] = select_spans(document, "orbital", max_spans=1)
    span_text = str(span["text"])

    assert span_text.startswith("## Orbital Calibration")
    assert "Neutral calibration prose" in span_text
    assert "Parent Background" not in span_text
    assert "Sibling Appendix" not in span_text


def test_heading_section_includes_deeper_subheadings() -> None:
    document = (
        "# Aurora Field Guide\n\n"
        "The overview body intentionally contains no query terms.\n\n"
        "## Packing Notes\n\n"
        "Bring insulated layers and a tripod.\n\n"
        "# Separate Topic\n\n"
        "This must be excluded."
    )

    [span] = select_spans(document, "aurora", max_spans=1)
    span_text = str(span["text"])

    assert "## Packing Notes" in span_text
    assert "Bring insulated layers" in span_text
    assert "Separate Topic" not in span_text


def test_heading_section_respects_character_budget() -> None:
    document = "# Mineral Atlas\n\n" + ("neutral body text " * 100) + "\n\n# Later\n\nIgnored."

    [span] = select_spans(document, "mineral atlas", max_spans=1, max_span_chars=10_000)
    span_text = str(span["text"])

    assert span_text.startswith("# Mineral Atlas")
    assert "neutral body text" in span_text
    assert int(span["end"]) - int(span["start"]) <= 1_200
    assert "Later" not in span_text


def test_non_heading_selection_remains_unchanged() -> None:
    document = (
        "Astronomy notes describe telescope mirrors and distant galaxies.\n\n"
        "Cooking notes explain sourdough starter and bread fermentation."
    )

    [span] = select_spans(document, "telescope galaxies", max_spans=1)

    assert str(span["text"]) == "Astronomy notes describe telescope mirrors and distant galaxies."


def test_spans_false_is_byte_identical_to_default() -> None:
    response = _response("Alpha sentence. Beta sentence.")
    default_request = RequestV3.extract(["https://example.test/doc"])
    explicit_false = RequestV3(
        capability=Capability.EXTRACT,
        input={"urls": ["https://example.test/doc"]},
        options={"output_format": "markdown", "include_images": False, "spans": False},
    )

    default = apply_bounded_context(
        response,
        default_request,
        prepare_extract_request(default_request, {}),
        store=RecordingStore(),
    )
    disabled = apply_bounded_context(
        response,
        explicit_false,
        prepare_extract_request(explicit_false, {}),
        store=RecordingStore(),
    )

    assert json.dumps(default.to_dict(), sort_keys=True, ensure_ascii=False) == json.dumps(
        disabled.to_dict(), sort_keys=True, ensure_ascii=False
    )
    assert "spans" not in default.results[0]


def test_legacy_extract_uses_carried_query_when_spans_query_is_absent() -> None:
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {
            "urls": ["https://example.test/doc"],
            "provider": "linkup",
            "spans": True,
            "query": "u\u0308ber query",
        },
    )

    assert request.options["spans"] is True
    assert request.options["spans_query"] == "über query"


def test_span_options_are_part_of_typed_extraction_cache_identity(tmp_path) -> None:
    config = {
        "linkup": {"api_key": "test"},
        "bounded_context": {"cache_root": str(tmp_path)},
        "v3": {"default_max_provider_attempts": 3, "max_attempts_per_provider": 2},
    }
    plan = ProviderPlan(("linkup",), "linkup")
    default = RequestV3.extract(["https://example.test/doc"])
    enabled = RequestV3.extract(
        ["https://example.test/doc"], spans=True, spans_query="first query"
    )
    other_query = RequestV3.extract(
        ["https://example.test/doc"], spans=True, spans_query="second query"
    )

    default_vary = extract._extract_cache_vary(default, plan, config)
    enabled_vary = extract._extract_cache_vary(enabled, plan, config)
    other_vary = extract._extract_cache_vary(other_query, plan, config)

    assert default_vary != enabled_vary
    assert enabled_vary != other_vary


def test_spans_true_adds_well_formed_result_spans() -> None:
    text = "Alpha overview. Semantic offsets cite exact source passages."
    response = _response(text)
    request = RequestV3.extract(
        ["https://example.test/doc"], spans=True, spans_query="exact source"
    )

    bounded = apply_bounded_context(
        response,
        request,
        prepare_extract_request(request, {}),
        store=RecordingStore(),
    )

    result = bounded.results[0]
    assert result["span_contract_version"] == 1
    assert result["spans"]
    assert all(span["within_preview"] is True for span in result["spans"])
    assert ResponseV3.from_dict(deepcopy(bounded.to_dict())) == bounded


def test_truncate_and_store_keeps_full_text_offsets_and_preview_flag() -> None:
    text = (
        ("General background material without the target. " * 28)
        + "\n\nQuantum zebras provide the uniquely relevant target passage."
    )
    response = _response(text)
    request = RequestV3.extract(
        ["https://example.test/doc"],
        max_context_chars=1000,
        spans=True,
        spans_query="quantum zebras target",
    )
    store = RecordingStore()

    bounded = apply_bounded_context(
        response,
        request,
        prepare_extract_request(request, {}),
        store=store,
    )

    result = bounded.results[0]
    preview = result["text"]["text"]
    full_text = nfc_text(text)
    target = next(span for span in result["spans"] if "Quantum zebras" in span["text"])
    assert len(preview) == 1000
    assert target["within_preview"] is False
    assert target["start"] >= len(preview)
    assert target["text"] == full_text[target["start"]:target["end"]]
    assert store.calls == [("https://example.test/doc", full_text)]
