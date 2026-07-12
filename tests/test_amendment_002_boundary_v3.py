"""Amendment 002 (rev 3) boundary suite.

Part A validates the six golden fixtures mechanically (pure JSON checks —
GREEN, guarding the fixtures themselves against drift). Part B encodes the
contract-implementation boundaries and is expected RED until contract_v3 and
the generated schema implement Amendment 002.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v3"
FIXTURE_NAMES = (
    "01_search_success",
    "02_extract_success",
    "03_cache_hit",
    "04_fallback",
    "05_degraded",
    "06_total_failure",
)
CACHE_SERVED = {"fresh_hit", "stale_hit"}
DIVERSITY_KEYS = {
    "method", "method_version", "method_degraded",
    "provider_count", "host_count", "source_family_count",
    "unique_cluster_count",
}
ACTION_REASONS = {
    "excluded": {"spam_domain"},
    "reranked": {"intent_authority"},
    "demoted": {"domain_diversity"},
    "selected_as_representative": {"dedup_representative"},
    "truncated_by_limit": {"max_results", "max_content_bytes"},
}
BANNED_FIELDS = (
    "answer", "full_synthesis", "claim", "verification", "truth_confidence",
)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(params=FIXTURE_NAMES)
def fixture(request):
    return _load(request.param)


# ===========================================================================
# Part A — golden-fixture mechanical consistency (must be GREEN)
# ===========================================================================

def test_fixture_has_required_amendment_fields(fixture):
    assert isinstance(fixture["execution_id"], str) and fixture["execution_id"]
    assert isinstance(fixture["observations"], list)
    assert isinstance(fixture["policy_actions"], list)
    assert isinstance(fixture["source_diversity"], dict)
    assert "source_independence_estimate" not in fixture


def test_fixture_diversity_has_exact_component_keys(fixture):
    diversity = fixture["source_diversity"]
    assert set(diversity) == DIVERSITY_KEYS, (
        f"source_diversity keys must be exactly {sorted(DIVERSITY_KEYS)}, "
        f"got {sorted(diversity)}"
    )
    for key in ("provider_count", "host_count", "source_family_count",
                "unique_cluster_count"):
        assert isinstance(diversity[key], int) and diversity[key] >= 0
    assert isinstance(diversity["method_degraded"], bool)
    assert not any(
        isinstance(v, float) for v in diversity.values()
    ), "no scalar/estimate may hide in source_diversity"


def test_fixture_has_no_banned_fields_anywhere(fixture):
    blob = json.dumps(fixture)
    for banned in BANNED_FIELDS:
        assert f'"{banned}"' not in blob, f"banned field {banned!r} present"
    assert '"type": "synthesis"' not in blob


RESULT_ALLOWED_KEYS = {
    "result_id", "kind", "engine_rank", "representative_observation_id",
    "observation_ids", "dedup_cluster_id", "url", "title", "snippet", "text",
}
RESULT_FORBIDDEN_LEGACY_KEYS = {
    "status", "canonical_url", "cluster_id", "provenance", "published_at",
    "offset_unit", "text_normalization", "segments", "score",
}
TRANSFORMATIONS = {
    "whitespace_norm", "deterministic_truncation",
    "mechanical_segmentation", "image_base64_replace",
}


def _check_projected_text(ptv, observations, expect_field):
    assert set(ptv) == {
        "text", "text_sha256", "origin", "provenance", "segments"
    }, f"ProjectedTextV3 keys wrong: {sorted(ptv)}"
    text = ptv["text"]
    assert unicodedata.normalize("NFC", text) == text, "text must be NFC"
    assert ptv["text_sha256"] == hashlib.sha256(
        text.encode("utf-8")).hexdigest()
    assert ptv["origin"] in {"provider", "engine"}
    prov = ptv["provenance"]
    assert prov["observation_id"] in observations
    assert prov["source_field"] == expect_field
    assert set(prov["transformations"]) <= TRANSFORMATIONS
    obs = observations[prov["observation_id"]]
    if not prov["transformations"]:
        assert text == obs[expect_field], (
            "identity projection must equal the observation field exactly"
        )
    segments = ptv["segments"]
    assert segments, "ProjectedTextV3 requires segments"
    offset = 0
    for segment in segments:
        assert set(segment) == {"start", "end", "text"}
        assert segment["start"] == offset, "codepoint segments must be contiguous"
        assert segment["end"] > segment["start"], "half-open, non-empty"
        assert text[segment["start"]:segment["end"]] == segment["text"]
        offset = segment["end"]
    assert offset == len(text), "segments must cover the projected text"
    assert "".join(s["text"] for s in segments) == text


def test_fixture_observation_fk_and_projection_integrity(fixture):
    observations = {o["observation_id"]: o for o in fixture["observations"]}
    attempts = {a["attempt_id"] for a in fixture["provider_attempts"]}
    cache_served = fixture["cache_status"].get("disposition") in CACHE_SERVED

    for obs in fixture["observations"]:
        if cache_served:
            assert obs["provider_attempt_id"] not in attempts, (
                "cache-served observations must reference the origin execution, "
                "not a fabricated current attempt"
            )
        else:
            assert obs["provider_attempt_id"] in attempts, (
                f"dangling provider_attempt_id {obs['provider_attempt_id']!r}"
            )
        assert obs["provider_result_index"] >= 0
        assert obs["url"]["observed"]
        if obs["kind"] == "search_result":
            assert obs["text"] is None
        else:
            assert obs["snippet"] is None

    for result in fixture["results"]:
        assert set(result) <= RESULT_ALLOWED_KEYS, (
            f"forbidden result keys: {sorted(set(result) - RESULT_ALLOWED_KEYS)}"
        )
        assert not set(result) & RESULT_FORBIDDEN_LEGACY_KEYS
        assert result["kind"] in {"search_result", "extracted_document"}
        rep = result["representative_observation_id"]
        assert rep in observations
        assert rep in result["observation_ids"]
        for oid in result["observation_ids"]:
            assert oid in observations
        rep_obs = observations[rep]
        assert result["url"] == rep_obs["url"], "url must be the {observed, canonical} object"
        for field in ("title", "snippet", "text"):
            value = result.get(field)
            if value is not None:
                _check_projected_text(value, observations, field)
        assert result["dedup_cluster_id"], "every result requires a dedup cluster id"


def test_fixture_engine_rank_is_dense_and_sorted(fixture):
    ranks = [r["engine_rank"] for r in fixture["results"]]
    assert ranks == list(range(1, len(ranks) + 1))


def test_fixture_policy_actions_use_closed_combinations(fixture):
    observations = {o["observation_id"] for o in fixture["observations"]}
    for action in fixture["policy_actions"]:
        assert action["reason"] in ACTION_REASONS[action["action"]]
        assert action["observation_id"] in observations


def test_fixture_attempts_carry_endpoint_decision_and_tries(fixture):
    for attempt in fixture["provider_attempts"]:
        assert attempt["endpoint_id"].startswith(attempt["provider"] + ":")
        assert attempt["decision"] in {"attempted", "skipped"}
        if attempt["decision"] == "skipped":
            assert attempt["tries"] == []
            assert attempt.get("skip_reason")
        else:
            assert len(attempt["tries"]) == attempt.get("retry_count", 0) + 1
            for n, one_try in enumerate(attempt["tries"], start=1):
                assert one_try["try_number"] == n
                assert one_try["outcome"] in {"success", "error"}
                assert (one_try["error"] is not None) == (
                    one_try["outcome"] == "error"
                )


def test_fixture_diversity_counts_match_content(fixture):
    observations = fixture["observations"]
    diversity = fixture["source_diversity"]
    assert diversity["provider_count"] == len({o["provider"] for o in observations})
    hosts = {urlparse(o["url"]["canonical"]).netloc for o in observations}
    assert diversity["host_count"] == len(hosts)
    assert diversity["unique_cluster_count"] == len(fixture.get("dedup_clusters", []))


def test_fixture_extract_text_is_segmented_projected_object():
    doc = _load("02_extract_success")
    result = doc["results"][0]
    assert result["kind"] == "extracted_document"
    ptv = result["text"]
    assert ptv is not None and result["snippet"] is None
    assert len(ptv["segments"]) >= 2, "02 keeps its M0 two-segment structure"
    assert "mechanical_segmentation" in ptv["provenance"]["transformations"]


def test_fixture_cache_served_carries_origin_execution_id():
    for name in ("03_cache_hit", "05_degraded"):
        doc = _load(name)
        assert doc["cache_status"]["origin_execution_id"]
        assert doc["cache_status"]["source_contract_version"] == "3.0"


def test_fixture_engine_field_absent_or_complete(fixture):
    engine = fixture.get("engine")
    if engine is not None:
        assert set(engine) == {"name", "version", "build_commit"}
    assert "timing" not in fixture, "timing is deferred and must not be emitted in 3.0"


def test_fixture_no_bare_cross_provider_score(fixture):
    for result in fixture["results"]:
        assert "score" not in result
    for obs in fixture["observations"]:
        score = obs.get("provider_score")
        if score is not None:
            assert set(score) == {"value", "semantics"}


# ===========================================================================
# Part B — contract-implementation boundaries (expected RED until implemented)
# ===========================================================================

def _contract_response(fixture_name="01_search_success"):
    from contract_v3 import ResponseV3

    return ResponseV3.from_dict(_load(fixture_name))


def test_contract_roundtrips_amendment_fields():
    from contract_v3 import ResponseV3

    payload = _load("01_search_success")
    response = ResponseV3.from_dict(payload)
    out = response.to_dict()
    assert out["execution_id"] == payload["execution_id"]
    assert out["observations"] == payload["observations"]
    assert out["policy_actions"] == payload["policy_actions"]
    assert out["source_diversity"] == payload["source_diversity"]
    assert "source_independence_estimate" not in out


def test_contract_rejects_source_independence_estimate():
    from contract_v3 import ResponseV3

    payload = _load("01_search_success")
    payload["source_independence_estimate"] = {"score": 0.7}
    with pytest.raises(ValueError):
        ResponseV3.from_dict(payload)


def test_contract_rejects_diversity_scalar():
    from contract_v3 import ResponseV3

    payload = _load("01_search_success")
    payload["source_diversity"]["scalar"] = 0.8
    with pytest.raises(ValueError):
        ResponseV3.from_dict(payload)


def test_contract_rejects_partial_engine_object():
    from contract_v3 import ResponseV3

    payload = _load("01_search_success")
    payload["engine"] = {"name": "wsp", "version": "3.0"}  # build_commit missing
    with pytest.raises(ValueError):
        ResponseV3.from_dict(payload)


def test_contract_rejects_dangling_observation_fk():
    from contract_v3 import ResponseV3

    payload = _load("01_search_success")
    payload["results"][0]["representative_observation_id"] = "obs_missing"
    with pytest.raises(ValueError):
        ResponseV3.from_dict(payload)


def test_contract_requires_execution_id():
    from contract_v3 import ResponseV3

    payload = _load("01_search_success")
    del payload["execution_id"]
    with pytest.raises((ValueError, KeyError, TypeError)):
        ResponseV3.from_dict(payload)


# ===========================================================================
# Part C — JSON-schema boundary via Ajv (expected RED until schema regenerated)
# ===========================================================================

def test_schema_boundary_via_ajv():
    script = Path(__file__).parent / "schema_boundary_v3.mjs"
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"ajv schema boundary failed:\n{proc.stdout}\n{proc.stderr}"
    )
