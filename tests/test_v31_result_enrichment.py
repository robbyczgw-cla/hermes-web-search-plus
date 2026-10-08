"""v3.1 result enrichment: attributed multi-observation snippets and fetch cues."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wsp_core.contract_v3 import (
    AttemptOutcome,
    Capability,
    ProviderAttemptV3,
    RequestV3,
    ResponseStatus,
    ResponseV3,
)
from wsp_core.orchestrator_v3 import ProviderPlan
from wsp_core.runtime_v3 import (
    observations_from_legacy,
    project_results_from_observations,
    response_from_legacy,
)


ROOT = Path(__file__).resolve().parents[1]


_ROUTING = {
    "policy_id": "classic",
    "policy_revision": "fixture",
    "mode": "classic",
    "candidate_order": ["fixture"],
    "selected_provider": "fixture",
    "fallback_reason": "none",
}


def _observations(raw, provider="fixture", attempt="attempt_enrichment"):
    return observations_from_legacy(
        {"results": raw}, provider, Capability.SEARCH, attempt
    )


def _response(results, observations):
    return ResponseV3(
        request_id="req_enrichment",
        execution_id="exec_enrichment",
        capability=Capability.SEARCH,
        status=ResponseStatus.OK,
        results=results,
        observations=observations,
        policy_actions=[],
        provider_attempts=[],
        routing_receipt=_ROUTING,
        cache_status={"disposition": "miss"},
    )


def test_cluster_snippet_merge_keeps_each_fragment_attributed_and_deduplicates_containment():
    raw = [
        {
            "url": "https://docs.example.test/guide",
            "title": "Guide",
            "snippet": "Short statement.",
        },
        {
            "url": "https://docs.example.test/guide",
            "title": "Guide from another provider",
            "snippet": "Short statement. Additional verified detail.",
        },
        {
            "url": "https://docs.example.test/guide",
            "title": "Guide third provider",
            "snippet": "Independent operational detail.",
        },
    ]
    observations = _observations(raw)
    result = project_results_from_observations(observations, raw)[0]

    snippet = result["snippet"]
    assert snippet["text"] == (
        "Short statement. Additional verified detail.\n\n"
        "Independent operational detail."
    )
    assert snippet["provenance"] == {
        "aggregation": "concat",
        "separator": "\n\n",
        "fragments": [
            {
                "observation_id": observations[1]["observation_id"],
                "source_field": "snippet",
                "text": "Short statement. Additional verified detail.",
                "transformations": ["mechanical_segmentation"],
            },
            {
                "observation_id": observations[2]["observation_id"],
                "source_field": "snippet",
                "text": "Independent operational detail.",
                "transformations": ["mechanical_segmentation"],
            },
        ],
    }
    _response([result], observations)


def test_aggregate_validator_reconstructs_exact_text_from_fragments_and_separator():
    raw = [
        {"url": "https://example.test/a", "title": "A", "snippet": "First."},
        {"url": "https://example.test/a", "title": "A", "snippet": "Second."},
    ]
    observations = _observations(raw)
    result = project_results_from_observations(observations, raw)[0]
    valid = _response([result], observations)
    assert valid.results[0]["snippet"]["provenance"]["aggregation"] == "concat"

    malformed = valid.to_dict()
    malformed["results"][0]["snippet"]["text"] = "First. Second."
    with pytest.raises(ValueError, match="aggregate projected text"):
        ResponseV3.from_dict(malformed)


def test_aggregate_snippet_cap_truncates_only_a_named_fragment_and_never_exceeds_600_chars():
    raw = [
        {"url": "https://example.test/a", "title": "A", "snippet": "A" * 700},
        {"url": "https://example.test/a", "title": "A", "snippet": "second fragment"},
    ]
    observations = _observations(raw)
    result = project_results_from_observations(observations, raw)[0]
    snippet = result["snippet"]

    assert snippet["text"] == "A" * 600
    assert len(snippet["text"]) == 600
    assert snippet["provenance"]["fragments"] == [
        {
            "observation_id": observations[0]["observation_id"],
            "source_field": "snippet",
            "text": "A" * 600,
            "transformations": ["mechanical_segmentation", "deterministic_truncation"],
        }
    ]
    _response([result], observations)


def test_cluster_projection_is_deterministic_when_observation_input_order_changes():
    raw = [
        {"url": "https://example.test/a", "title": "A", "snippet": "One."},
        {"url": "https://example.test/a", "title": "A", "snippet": "Two."},
    ]
    observations = _observations(raw)
    first = project_results_from_observations(observations, raw)[0]
    second = project_results_from_observations(list(reversed(observations)), raw)[0]

    assert first["snippet"] == second["snippet"]
    assert first["source_type"] == second["source_type"]
    assert first["fetch_priority"] == second["fetch_priority"]


def test_source_type_is_typed_heuristic_and_fetch_priority_has_only_stable_reason_codes():
    raw = [
        {
            "url": "https://github.com/example/project",
            "title": "Project",
            "snippet": "Repository evidence.",
            "source_type": "official-docs",
        },
        {
            "url": "https://github.com/example/project",
            "title": "Project duplicate",
            "snippet": "Independent provider evidence.",
            "source_type": "repo",
        },
        {
            "url": "https://random.example.test/post",
            "title": "Post",
            "snippet": "General commentary.",
        },
    ]
    observations = _observations(raw)
    observations[1]["provider"] = "fixture-secondary"
    results = project_results_from_observations(observations, raw)

    assert results[0]["source_type"] == {
        "value": "repo",
        "method": "url_heuristic",
        "method_version": "1",
        "confidence": "high",
    }
    assert results[0]["fetch_priority"] == {
        "tier": "high",
        "reason_codes": [
            "cluster_consensus",
            "rank_top_3",
            "source_type_authoritative",
        ],
    }
    assert results[1]["source_type"] == {
        "value": "other",
        "method": "url_heuristic",
        "method_version": "1",
        "confidence": "low",
    }
    assert results[1]["fetch_priority"] == {
        "tier": "medium",
        "reason_codes": [
            "cluster_single_observation",
            "rank_top_3",
            "source_type_general",
        ],
    }
    _response(results, observations)


def test_single_source_projection_shape_remains_unchanged_for_non_clustered_snippet():
    raw = [{"url": "https://example.test/a", "title": "A", "snippet": "Only source."}]
    observation = _observations(raw)[0]
    result = project_results_from_observations([observation], raw)[0]

    assert result["snippet"]["provenance"] == {
        "observation_id": observation["observation_id"],
        "source_field": "snippet",
        "transformations": ["mechanical_segmentation"],
    }
    _response([result], [observation])


def test_generated_schema_declares_aggregate_provenance_and_typed_enrichment_fields():
    schema = json.loads((ROOT / "schemas" / "v3" / "response.schema.json").read_text())
    definitions = schema["$defs"]

    assert definitions["ProjectedAggregateProvenanceV31"]["properties"]["separator"] == {
        "type": "string",
        "minLength": 1,
    }
    result_fields = definitions["ResultV3"]["properties"]
    assert result_fields["source_type"] == {"$ref": "#/$defs/SourceTypeV31"}
    assert result_fields["fetch_priority"] == {"$ref": "#/$defs/FetchPriorityV31"}


def test_research_projection_preserves_real_provider_consensus_end_to_end():
    request = RequestV3(capability=Capability.SEARCH, input={"query": "rate limits"})
    plan = ProviderPlan(
        candidate_order=("research",),
        selected_provider="research",
        execution_id="exec_research_enrichment",
    )
    attempts = [
        ProviderAttemptV3(
            attempt_id="attempt_alpha",
            provider="alpha",
            capability=Capability.SEARCH,
            outcome=AttemptOutcome.SUCCESS,
            result_count=1,
        ),
        ProviderAttemptV3(
            attempt_id="attempt_beta",
            provider="beta",
            capability=Capability.SEARCH,
            outcome=AttemptOutcome.SUCCESS,
            result_count=1,
        ),
    ]
    payload = {
        "provider": "research",
        "results": [
            {
                "url": "https://docs.example.test/rate-limits",
                "title": "Rate limits",
                "snippet": "Retry with backoff.",
            }
        ],
        "_v3_raw_results": [
            {
                "provider": "alpha",
                "url": "https://docs.example.test/rate-limits",
                "title": "Rate limits",
                "snippet": "Retry with backoff.",
            },
            {
                "provider": "beta",
                "url": "https://docs.example.test/rate-limits",
                "title": "Rate limits documentation",
                "snippet": "Do not immediately replay the full batch.",
            },
        ],
        "_v3_provider_attempts": attempts,
    }

    response = response_from_legacy(request, plan, payload)

    assert [observation["provider"] for observation in response.observations] == [
        "alpha",
        "beta",
    ]
    result = response.results[0]
    assert result["observation_ids"] == [
        response.observations[0]["observation_id"],
        response.observations[1]["observation_id"],
    ]
    assert result["fetch_priority"]["reason_codes"][0] == "cluster_consensus"
    assert [
        fragment["observation_id"]
        for fragment in result["snippet"]["provenance"]["fragments"]
    ] == result["observation_ids"]
