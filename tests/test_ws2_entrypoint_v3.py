from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema

import extract
from bounded_context_v3 import apply_bounded_context, prepare_extract_request
from cache_v3 import cache_material_from_response, response_payload_from_cache_material
from contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from runtime_v3 import observations_from_legacy, project_results_from_observations


ROOT = Path(__file__).resolve().parents[1]


def response_for_urls(urls: list[str], length: int = 20) -> ResponseV3:
    raw = [
        {"url": url, "title": url, "content": "X" * length}
        for url in urls
    ]
    observations = observations_from_legacy(
        {"results": raw}, "fixture", Capability.EXTRACT, "attempt_ws2_entry"
    )
    return ResponseV3(
        request_id="req_ws2_entry",
        execution_id="exec_ws2_entry",
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


def test_native_extract_caps_before_provider_execution(monkeypatch, tmp_path: Path) -> None:
    urls = [f"https://e.example/{index:02d}" for index in range(1, 13)]
    request = RequestV3.extract(urls, max_urls=10, max_context_chars=60_000)
    captured = {}

    def fake_provider_execute(bounded_request, _plan, _config):
        captured["provider_request"] = bounded_request

    def fake_execute(original_request, adapter, config):
        captured["canonical_request"] = original_request
        provider_plan = adapter.plan(original_request, config)
        adapter.execute(original_request, provider_plan, config)
        response = response_for_urls(
            captured["provider_request"].input["urls"]
        )
        response = adapter.finalize_response(
            original_request, provider_plan, response, config
        )
        return SimpleNamespace(response=response)

    monkeypatch.setattr(extract, "_execute_extract_v3", fake_provider_execute)
    monkeypatch.setattr(extract, "execute_v3_request", fake_execute)
    response = extract.run_extract_request_v3(
        request,
        config={"bounded_context": {"cache_root": str(tmp_path)}},
    )

    assert captured["canonical_request"].input["urls"] == urls
    assert captured["provider_request"].input["urls"] == urls[:10]
    assert response.status is ResponseStatus.DEGRADED
    assert response.limits_applied["extract"]["processed_urls"] == urls[:10]
    assert response.limits_applied["extract"]["omitted_urls"] == urls[10:]
    assert {
        observation["url"]["observed"] for observation in response.observations
    }.isdisjoint(urls[10:])
    assert all(
        omitted not in json.dumps(response.to_dict()["provider_attempts"])
        for omitted in urls[10:]
    )


def test_bounded_response_is_schema_valid_and_round_trips(tmp_path: Path) -> None:
    request = RequestV3.extract(
        ["https://a.example/one", "https://b.example/two"],
        max_context_chars=1000,
    )
    plan = prepare_extract_request(request, {})
    response = response_for_urls(request.input["urls"], length=800)
    from bounded_context_v3 import FullTextStore

    bounded = apply_bounded_context(
        response, request, plan, store=FullTextStore(tmp_path)
    )
    wire = bounded.to_dict()
    schema = json.loads(
        (ROOT / "schemas/v3/response.schema.json").read_text(encoding="utf-8")
    )

    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(wire)
    assert ResponseV3.from_dict(wire).to_dict() == wire


def test_amendment_002_goldens_still_validate_after_additive_schema() -> None:
    schema = json.loads(
        (ROOT / "schemas/v3/response.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    for path in sorted((ROOT / "tests/fixtures/v3").glob("0*.json")):
        validator.validate(json.loads(path.read_text(encoding="utf-8")))


def test_schema_rejects_invalid_budget_and_dishonest_storage(tmp_path: Path) -> None:
    request_schema = json.loads(
        (ROOT / "schemas/v3/request.schema.json").read_text(encoding="utf-8")
    )
    invalid_request = RequestV3.extract(["https://a.example/doc"]).to_dict()
    invalid_request["options"]["max_context_chars"] = "unbounded"
    try:
        jsonschema.Draft202012Validator(request_schema).validate(invalid_request)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("request schema accepted non-integer context budget")

    request = RequestV3.extract(["https://a.example/doc"], max_context_chars=1000)
    plan = prepare_extract_request(request, {})
    from bounded_context_v3 import FullTextStore

    wire = apply_bounded_context(
        response_for_urls(request.input["urls"], length=1500),
        request,
        plan,
        store=FullTextStore(tmp_path),
    ).to_dict()
    wire["stored_content"][0]["reference"] = None
    response_schema = json.loads(
        (ROOT / "schemas/v3/response.schema.json").read_text(encoding="utf-8")
    )
    try:
        jsonschema.Draft202012Validator(response_schema).validate(wire)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("response schema accepted fake successful storage")


def test_evidence_cache_preserves_bounded_context_metadata(tmp_path: Path) -> None:
    request = RequestV3.extract(["https://a.example/one"], max_context_chars=1000)
    plan = prepare_extract_request(request, {})
    from bounded_context_v3 import FullTextStore

    bounded = apply_bounded_context(
        response_for_urls(request.input["urls"], length=1500),
        request,
        plan,
        store=FullTextStore(tmp_path),
    )
    material = cache_material_from_response(bounded.to_dict())
    cached = response_payload_from_cache_material(
        material,
        request_id="req_cache_read",
        execution_id="exec_cache_read",
        disposition="fresh_hit",
        entry_id="cache_ws2",
        age_seconds=1,
        ttl_seconds=60,
    )

    assert cached["stored_content"] == bounded.to_dict()["stored_content"]
    assert cached["limits_applied"] == bounded.to_dict()["limits_applied"]
    assert ResponseV3.from_dict(cached).execution_id == "exec_cache_read"
