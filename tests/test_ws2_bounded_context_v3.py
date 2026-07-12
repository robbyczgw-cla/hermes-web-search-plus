from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bounded_context_v3 import (
    DEFAULT_MAX_CONTEXT_CHARS,
    FullTextStore,
    apply_bounded_context,
    prepare_extract_request,
)
from contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from runtime_v3 import observations_from_legacy, project_results_from_observations


FIXTURES = Path(__file__).parent / "fixtures" / "v3" / "ws2"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_response(lengths: list[int]) -> ResponseV3:
    raw = [
        {
            "url": f"https://source-{index}.example/doc",
            "title": f"Source {index}",
            "content": chr(64 + index) * length,
        }
        for index, length in enumerate(lengths, 1)
    ]
    observations = observations_from_legacy(
        {"results": raw}, "fixture", Capability.EXTRACT, "attempt_ws2_fixture"
    )
    return ResponseV3(
        request_id="req_fixture",
        execution_id="exec_fixture",
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


class RecordingStore:
    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[tuple[str, str]] = []

    def store(self, url: str, text: str) -> dict:
        self.calls.append((url, text))
        if not self.succeed:
            return {
                "observation_id": "unused",
                "storage_attempted": True,
                "storage_succeeded": False,
                "reference": None,
                "full_text_sha256": None,
                "full_text_chars": None,
            }
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return {
            "observation_id": "unused",
            "storage_attempted": True,
            "storage_succeeded": True,
            "reference": {
                "store": "web_text_v3",
                "key": key,
                "media_type": "text/markdown",
            },
            "full_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "full_text_chars": len(text),
        }


def test_ws2_acceptance_fixture_inventory_is_frozen() -> None:
    assert sorted(path.name for path in FIXTURES.glob("*.json")) == [
        "01_extract_within_limits.json",
        "02_urls_capped.json",
        "03_content_truncated.json",
        "04_truncated_stored.json",
        "05_storage_failed.json",
        "06_no_starvation.json",
        "07_budget_bounds.json",
        "09_single_source_after_truncation.json",
    ]


def test_prepare_extract_request_caps_urls_truthfully() -> None:
    fixture = load_fixture("02_urls_capped.json")
    request = RequestV3.from_dict(fixture["request"])

    plan = prepare_extract_request(request, {})

    expected = fixture["expected"]
    assert plan.request.input["urls"] == request.input["urls"][:10]
    assert plan.processed_urls == request.input["urls"][:10]
    assert plan.omitted_urls == expected["omitted_urls"]
    assert plan.max_urls == expected["max_urls"]
    assert plan.max_context_chars == DEFAULT_MAX_CONTEXT_CHARS


def test_request_budget_bounds_are_deterministic() -> None:
    fixture = load_fixture("07_budget_bounds.json")
    for case in fixture["cases"][:2]:
        request = RequestV3.extract(
            ["https://a.example/doc"], max_context_chars=case["input"]
        )
        assert prepare_extract_request(request, {}).max_context_chars == case["expected_effective"]

    for case in fixture["cases"][2:]:
        with pytest.raises(ValueError, match=case["expected_error"]):
            RequestV3.extract(
                ["https://a.example/doc"], max_context_chars=case["input"]
            )


def test_operator_url_ceiling_beats_request_and_hard_max() -> None:
    request = RequestV3.extract(
        [f"https://e.example/{index}" for index in range(30)], max_urls=50
    )
    plan = prepare_extract_request(request, {"bounded_context": {"max_urls": 7}})
    assert plan.max_urls == 7
    assert len(plan.processed_urls) == 7
    assert len(plan.omitted_urls) == 23


def test_within_limits_stays_ok_without_storage(tmp_path: Path) -> None:
    fixture = load_fixture("01_extract_within_limits.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    response = make_response(fixture["source_lengths"])

    bounded = apply_bounded_context(
        response, request, plan, store=FullTextStore(tmp_path)
    )

    limits = bounded.limits_applied["extract"]
    assert bounded.status is ResponseStatus.OK
    assert limits == {
        "requested_url_count": 1,
        "processed_urls": ["https://a.example/doc"],
        "omitted_urls": [],
        "omitted_url_count": 0,
        "max_urls": 10,
        "max_context_chars": 60000,
        "context_chars_returned": 42,
        "truncated": False,
    }
    assert bounded.stored_content == []


def test_fair_share_truncation_matches_frozen_fixture() -> None:
    fixture = load_fixture("03_content_truncated.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    response = make_response(fixture["source_lengths"])
    store = RecordingStore()

    bounded = apply_bounded_context(response, request, plan, store=store)

    lengths = [len(result["text"]["text"]) for result in bounded.results]
    assert lengths == fixture["expected"]["allocations"]
    assert bounded.limits_applied["extract"]["context_chars_returned"] == 1000
    assert bounded.limits_applied["extract"]["truncated"] is True
    assert bounded.status is ResponseStatus.DEGRADED
    assert {warning["code"] for warning in bounded.warnings} >= {
        "wsp.content.truncated"
    }
    assert len(bounded.stored_content) == 2
    assert len(store.calls) == 2


def test_url_omission_degrades_even_without_content_truncation() -> None:
    fixture = load_fixture("02_urls_capped.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    response = make_response([10] * 10)

    bounded = apply_bounded_context(
        response, request, plan, store=RecordingStore()
    )

    assert bounded.status is ResponseStatus.DEGRADED
    assert bounded.limits_applied["extract"]["omitted_urls"] == fixture["expected"]["omitted_urls"]
    assert {warning["code"] for warning in bounded.warnings} >= {
        "wsp.extract.urls_omitted"
    }


def test_storage_reference_is_opaque_and_full_hash_is_truthful() -> None:
    fixture = load_fixture("04_truncated_stored.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    response = make_response(fixture["source_lengths"])

    bounded = apply_bounded_context(
        response, request, plan, store=RecordingStore()
    )
    stored = bounded.stored_content[0]

    assert stored["storage_succeeded"] is True
    assert stored["reference"]["store"] == "web_text_v3"
    assert len(stored["reference"]["key"]) == 64
    assert "/" not in stored["reference"]["key"]
    assert stored["full_text_chars"] == 1500
    assert stored["full_text_sha256"] == hashlib.sha256(
        ("A" * 1500).encode("utf-8")
    ).hexdigest()


def test_storage_failure_never_invents_reference() -> None:
    fixture = load_fixture("05_storage_failed.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    response = make_response(fixture["source_lengths"])

    bounded = apply_bounded_context(
        response, request, plan, store=RecordingStore(succeed=False)
    )
    stored = bounded.stored_content[0]

    assert stored["storage_succeeded"] is False
    assert stored["reference"] is None
    assert stored["full_text_sha256"] is None
    assert stored["full_text_chars"] is None
    assert {warning["code"] for warning in bounded.warnings} >= {
        "wsp.content.truncated",
        "wsp.storage.full_text_unavailable",
    }


def test_long_result_cannot_starve_later_results() -> None:
    fixture = load_fixture("06_no_starvation.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    bounded = apply_bounded_context(
        make_response(fixture["source_lengths"]),
        request,
        plan,
        store=RecordingStore(),
    )
    assert [len(result["text"]["text"]) for result in bounded.results] == [
        1000,
        1000,
        1000,
    ]


def test_truncation_preserves_single_source_prefix_segments_and_hash() -> None:
    fixture = load_fixture("09_single_source_after_truncation.json")
    request = RequestV3.from_dict(fixture["request"])
    plan = prepare_extract_request(request, {})
    response = make_response(fixture["source_lengths"])
    full_text = response.observations[0]["text"]

    bounded = apply_bounded_context(
        response, request, plan, store=RecordingStore()
    )
    projected = bounded.results[0]["text"]

    assert full_text.startswith(projected["text"])
    assert projected["text"] == full_text[:1000]
    assert projected["text_sha256"] == hashlib.sha256(
        projected["text"].encode("utf-8")
    ).hexdigest()
    assert projected["provenance"]["transformations"] == [
        "mechanical_segmentation",
        "deterministic_truncation",
    ]
    assert projected["segments"] == [
        {"start": 0, "end": 1000, "text": full_text[:1000]}
    ]
    assert bounded.policy_actions[-1]["reason"] == "max_context_chars"
    ResponseV3.from_dict(bounded.to_dict())
