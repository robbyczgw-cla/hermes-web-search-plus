"""WSP 3.1 extraction-cache identity and losslessness contract tests."""

from __future__ import annotations
from wsp_core import extract

from copy import deepcopy
import json

import pytest

from wsp_core.cache_v3 import ResponseCacheV3, response_payload_from_cache_material
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.contract_v3 import Capability
from wsp_core.orchestrator_v3 import ProviderPlan


def _config(tmp_path):
    return {
        "linkup": {"api_key": "linkup-test-key"},
        "serper": {"api_key": "serper-test-key", "scrape_url": "https://one.test"},
        "auto_routing": {"disabled_providers": []},
        "extract": {"allow_private_urls": True},
        "bounded_context": {
            "cache_root": str(tmp_path),
            "max_urls": 10,
            "max_context_chars": 60_000,
            "full_text_ttl_seconds": 604_800,
            "full_text_max_bytes": 268_435_456,
        },
        "v3": {
            "cache_dir": str(tmp_path),
            "state_path": str(tmp_path / "state.sqlite3"),
            "default_max_provider_attempts": 3,
            "max_attempts_per_provider": 1,
            "operator_receipt_journal": False,
        },
    }


def _request():
    return legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://example.test/one"], "provider": "serper"},
    )


def _vary(request, config):
    return extract._extract_cache_vary(
        request, ProviderPlan(("serper",), "serper"), config
    )


def _payload():
    url = "https://example.test/one"
    text = "lossless evidence"
    return {
        "contract_version": "3.0",
        "request_id": "request-origin",
        "execution_id": "exec_origin",
        "capability": "extract",
        "status": "ok",
        "results": [
            {
                "result_id": "result_origin",
                "kind": "extracted_document",
                "engine_rank": 1,
                "representative_observation_id": "obs_origin",
                "observation_ids": ["obs_origin"],
                "dedup_cluster_id": "cluster_origin",
                "url": {"observed": url, "canonical": url},
                "title": None,
                "snippet": None,
                "text": {
                    "text": text,
                    "text_sha256": "8d6c0d177702ebb0e8b463f2bc1f2ca084c99db69644b3590e8a7a7472cd7d0c",
                    "origin": "provider",
                    "provenance": {
                        "observation_id": "obs_origin",
                        "source_field": "text",
                        "transformations": ["mechanical_segmentation"],
                    },
                    "segments": [{"start": 0, "end": 17, "text": text}],
                },
            }
        ],
        "observations": [
            {
                "observation_id": "obs_origin",
                "provider_attempt_id": "attempt_origin",
                "provider_result_index": 0,
                "provider": "serper",
                "endpoint_id": "serper:extract",
                "kind": "extracted_document",
                "url": {"observed": url, "canonical": url},
                "title": None,
                "snippet": None,
                "text": text,
                "provider_rank": 1,
                "provider_score": None,
                "published_at": None,
                "provider_fields": {},
            }
        ],
        "policy_actions": [
            {
                "action": "selected_as_representative",
                "observation_id": "obs_origin",
                "reason": "dedup_representative",
            }
        ],
        "source_diversity": {
            "method": "component_count",
            "method_version": "1",
            "method_degraded": False,
            "provider_count": 1,
            "host_count": 1,
            "source_family_count": 1,
            "unique_cluster_count": 1,
        },
        "provider_attempts": [],
        "routing_receipt": {
            "policy_id": "classic",
            "policy_revision": "v2.9.1",
            "mode": "classic",
            "candidate_order": ["serper"],
            "selected_provider": "serper",
            "fallback_reason": "none",
        },
        "cache_status": {"disposition": "miss"},
        "limits_applied": {},
        "stored_content": [],
        "dedup_clusters": [{"dedup_cluster_id": "cluster_origin"}],
        "warnings": [],
    }


@pytest.mark.parametrize(
    "name,mutate",
    [
        (
            "requested_urls",
            lambda identity: identity.update(
                {"requested_urls": ["https://example.test/two"]}
            ),
        ),
        (
            "attempt_budget",
            lambda identity: identity["attempt_budget"].update(
                {"effective_max_provider_attempts": 2}
            ),
        ),
        (
            "effective_context_limits",
            lambda identity: identity["effective_context_limits"].update(
                {"max_context_chars": 12_000}
            ),
        ),
        ("output_format", lambda identity: identity.update({"output_format": "html"})),
        ("include_images", lambda identity: identity.update({"include_images": True})),
        (
            "include_raw_html",
            lambda identity: identity.update({"include_raw_html": True}),
        ),
        ("render_js", lambda identity: identity.update({"render_js": True})),
        (
            "provider_selection",
            lambda identity: identity["provider_selection"].update(
                {"allow_fallback": False}
            ),
        ),
        (
            "provider_endpoint_config",
            lambda identity: identity["provider_endpoint_config"]["serper"].update(
                {"scrape_url": "https://two.test"}
            ),
        ),
        (
            "url_policy",
            lambda identity: identity["url_policy"].update(
                {"allow_private_urls": False}
            ),
        ),
        (
            "storage_policy",
            lambda identity: identity["storage_policy"].update({"max_bytes": 1}),
        ),
    ],
)
def test_each_extraction_identity_component_change_is_a_miss(
    tmp_path, name, mutate
):
    request = _request()
    config = _config(tmp_path)
    original_vary = _vary(request, config)
    changed_vary = deepcopy(original_vary)
    mutate(changed_vary["extraction_cache_identity"])
    cache = ResponseCacheV3(tmp_path)

    cache.put(request, _payload(), now=100, vary=original_vary)
    lookup = cache.get(
        request,
        ttl_seconds=60,
        allow_stale_seconds=0,
        now=101,
        vary=changed_vary,
    )

    assert lookup.disposition == "miss", name


def test_identical_identity_round_trips_lossless_evidence_and_cache_origin(tmp_path):
    request = _request()
    vary = _vary(request, _config(tmp_path))
    payload = _payload()
    legacy_payload = {
        "provider": "serper",
        "results": [
            {
                "title": None,
                "url": "https://example.test/one",
                "content": "lossless evidence",
                "raw_content": "lossless evidence",
                "provider": "serper",
            }
        ],
        "routing": {
            "provider": "serper",
            "requested_provider": "serper",
            "fallback_used": False,
            "fallback_errors": [],
        },
    }
    cache = ResponseCacheV3(tmp_path)

    cache.put(request, payload, now=100, legacy_payload=legacy_payload, vary=vary)
    lookup = cache.get(
        request, ttl_seconds=60, allow_stale_seconds=0, now=101, vary=vary
    )
    hit = response_payload_from_cache_material(
        lookup.payload,
        request_id="request-current",
        execution_id="exec_current",
        disposition="fresh_hit",
        entry_id=lookup.entry_id,
        age_seconds=lookup.age_seconds,
        ttl_seconds=60,
    )

    assert lookup.disposition == "fresh_hit"
    for field in (
        "results",
        "observations",
        "policy_actions",
        "source_diversity",
        "limits_applied",
        "stored_content",
        "dedup_clusters",
        "warnings",
    ):
        assert json.dumps(hit[field], sort_keys=True, ensure_ascii=False) == json.dumps(
            payload[field], sort_keys=True, ensure_ascii=False
        )
    assert hit["routing_receipt"]["cache_origin"]["execution_id"] == "exec_origin"
    assert lookup.legacy_payload["results"][0]["raw_content"] == "lossless evidence"
    assert lookup.legacy_payload["results"][0]["provider"] == "serper"


@pytest.mark.parametrize("identity_version", [3, 999])
def test_old_or_unknown_identity_version_is_a_miss(tmp_path, identity_version):
    request = _request()
    vary = _vary(request, _config(tmp_path))
    cache = ResponseCacheV3(tmp_path)
    cache.put(request, _payload(), now=100, vary=vary)
    path = cache.path_for(request, vary=vary)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["identity_version"] = identity_version
    envelope["identity"]["identity_version"] = identity_version
    path.write_text(json.dumps(envelope), encoding="utf-8")

    lookup = cache.get(
        request, ttl_seconds=60, allow_stale_seconds=0, now=101, vary=vary
    )

    assert lookup.disposition == "miss"
    assert path.exists()


def test_corrupt_entry_is_quarantined_and_misses_without_raising(tmp_path):
    request = _request()
    vary = _vary(request, _config(tmp_path))
    cache = ResponseCacheV3(tmp_path)
    path = cache.path_for(request, vary=vary)
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    lookup = cache.get(
        request, ttl_seconds=60, allow_stale_seconds=0, now=101, vary=vary
    )

    assert lookup.disposition == "miss"
    assert not path.exists()
    assert list((tmp_path / "v3" / "response" / "quarantine" / "extract").glob("*.json"))


@pytest.mark.parametrize(
    "request_options,result",
    [
        ({}, {"url": "https://example.test/partial", "error": "upstream failed"}),
        (
            {"include_raw_html": True},
            {
                "url": "https://example.test/raw",
                "content": "body",
                "raw_html": "<p>body</p>",
            },
        ),
        (
            {"include_images": True},
            {
                "url": "https://example.test/image",
                "content": "body",
                "images": [{"url": "https://example.test/image.png"}],
            },
        ),
        (
            {},
            {
                "url": "https://example.test/provider-field",
                "content": "body",
                "metadata": {"provider_specific": True},
            },
        ),
    ],
)
def test_lossy_extract_payload_classes_never_write_cache(
    tmp_path, monkeypatch, request_options, result
):
    calls = []

    def fake_core(**kwargs):
        calls.append(kwargs["provider"])
        return {
            "provider": "linkup",
            "results": [result],
            "routing": {
                "provider": "linkup",
                "requested_provider": "linkup",
                "fallback_used": False,
                "fallback_errors": [],
            },
        }

    monkeypatch.setattr(extract, "_extract_plus_core", fake_core)
    config = _config(tmp_path)
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {
            "urls": [result["url"]],
            "provider": "linkup",
            **request_options,
        },
    )

    extract.run_extract_request_v3(request, config=config)
    extract.run_extract_request_v3(request, config=config)

    # A failed URL is also retried on the fallback provider (per-URL fallback).
    assert calls.count("linkup") == 2
    assert not (tmp_path / "v3" / "response" / "extract").exists()


def test_transient_provider_health_changes_do_not_vary_cache_identity(
    tmp_path, monkeypatch
):
    """Two identical auto requests must share one cache entry even when a
    candidate provider entered cooldown between them (the step-8 live-test
    regression: quota -> quota_blocked must not bust the cache)."""
    calls = 0

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "exa",
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "content": "stable content",
                }
            ],
            "routing": {
                "provider": "exa",
                "requested_provider": "auto",
                "fallback_used": True,
                "fallback_errors": [{"provider": "tavily", "error": "quota"}],
            },
        }

    monkeypatch.setattr(extract, "_extract_plus_core", fake_core)
    config = _config(tmp_path)
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://example.com/a"], "provider": "auto"},
    )

    first = extract.run_extract_request_v3(request, config=config)
    # Simulate the live incident: a candidate enters cooldown between calls.
    import wsp_core.provider_health as health

    monkeypatch.setattr(health, "provider_in_cooldown", lambda _p: (True, 120.0))
    second = extract.run_extract_request_v3(
        legacy_request_to_v3(
            Capability.EXTRACT,
            {"urls": ["https://example.com/a"], "provider": "auto"},
        ),
        config=config,
    )

    assert calls == 1, "second identical request must be served from cache"
    assert second.cache_status.get("disposition") in {"fresh_hit", "stale_hit"}
    assert first.results[0]["url"] == second.results[0]["url"]


def test_realistic_exa_result_shape_writes_and_hits_the_cache(
    tmp_path, monkeypatch
):
    """The live-retest regression: real Exa results carry favicon and
    published_date; those benign scalars must not block the cache write,
    and a hit must reproduce them."""
    calls = 0
    exa_result = {
        "url": "https://tokio.rs/blog/2019-10-scheduler",
        "title": "Making the Tokio scheduler 10x faster",
        "content": "scheduler content",
        "raw_content": "scheduler content",
        "provider": "exa",
        "favicon": "https://tokio.rs/favicon.ico",
        "published_date": "2019-10-13T00:00:00.000Z",
    }

    def fake_core(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "provider": "exa",
            "results": [dict(exa_result)],
            # Full live top-level shape: per-execution provider metadata must
            # not block the write (release-gate regression).
            "request_id": f"live-request-{calls}",
            "cost_dollars": {"total": 0.001, "contents": {"text": 0.001}},
            "statuses": [
                {"id": exa_result["url"], "status": "success", "source": "cached"}
            ],
            "routing": {
                "provider": "exa",
                "requested_provider": "auto",
                "fallback_used": True,
                "fallback_errors": [{"provider": "tavily", "error": "quota"}],
            },
        }

    monkeypatch.setattr(extract, "_extract_plus_core", fake_core)
    config = _config(tmp_path)

    def _request():
        return legacy_request_to_v3(
            Capability.EXTRACT,
            {"urls": [exa_result["url"]], "provider": "auto"},
        )

    first = extract.run_extract_request_v3(_request(), config=config)
    second = extract.run_extract_request_v3(_request(), config=config)

    assert calls == 1, "identical realistic request must be served from cache"
    assert second.cache_status.get("disposition") == "fresh_hit"
    assert second.cache_status.get("origin_execution_id") == first.execution_id
    assert second.provider_attempts == []
    assert first.results and second.results
    hit_result = second.results[0]
    assert hit_result["url"]["observed"] == exa_result["url"]
