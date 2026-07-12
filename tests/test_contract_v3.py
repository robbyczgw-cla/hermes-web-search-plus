import json
import unicodedata
import unittest
from pathlib import Path

from contract_v3 import (
    AttemptOutcome,
    CacheDisposition,
    Capability,
    DegradedReason,
    ErrorClass,
    FallbackReason,
    ProviderAttemptV3,
    RequestV3,
    ResponseStatus,
    ResponseV3,
    SkipReason,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "v3"
CLASSIC_RECEIPT = {
    "policy_id": "classic-fallback-chain",
    "policy_revision": "v2.9.1",
    "mode": "classic",
    "candidate_order": ["serper"],
    "selected_provider": "serper",
    "fallback_reason": "none",
}


class ContractV3Tests(unittest.TestCase):
    def test_enum_namespace_is_frozen(self):
        self.assertEqual([item.value for item in Capability], ["search", "extract"])
        self.assertEqual(
            [item.value for item in DegradedReason],
            [
                "wsp.cache.served_stale",
                "wsp.content.truncated",
                "wsp.extract.urls_omitted",
                "wsp.extract.partial",
                "wsp.budget.limited",
                "wsp.independence.method_degraded",
            ],
        )
        self.assertEqual(
            [item.value for item in ErrorClass],
            [
                "invalid_request",
                "unsupported",
                "config",
                "auth",
                "quota",
                "rate_limit",
                "transient",
                "timeout",
                "provider_contract",
                "content",
                "security",
                "budget",
                "cancelled",
                "internal",
            ],
        )
        self.assertEqual(
            [item.value for item in AttemptOutcome],
            ["success", "partial", "skipped", "failed", "cancelled"],
        )
        self.assertEqual(
            [item.value for item in SkipReason],
            [
                "disabled",
                "unsupported_capability",
                "not_configured",
                "missing_credentials",
                "auth_blocked",
                "quota_blocked",
                "rate_limited",
                "circuit_open",
                "budget_blocked",
                "policy_excluded",
                "deadline_exceeded",
            ],
        )
        self.assertEqual(
            [item.value for item in FallbackReason],
            [
                "none",
                "selected_failed",
                "selected_skipped",
                "insufficient_results",
                "partial_content",
                "budget_chain",
            ],
        )

    def test_search_request_roundtrip(self):
        request = RequestV3.search(
            query="latest model release",
            request_id="req_search_1",
            max_results=5,
            freshness="week",
            accept_features=["provider_attempts", "dedup_clusters"],
        )
        payload = request.to_dict()
        self.assertEqual(payload["contract_version"], "3.0")
        self.assertEqual(payload["capability"], "search")
        self.assertEqual(payload["input"]["query"], "latest model release")
        self.assertNotIn("urls", payload["input"])
        self.assertEqual(RequestV3.from_dict(payload), request)

    def test_extract_request_roundtrip(self):
        request = RequestV3.extract(
            urls=["https://example.com/a"],
            request_id="req_extract_1",
            output_format="markdown",
            include_images=False,
        )
        payload = request.to_dict()
        self.assertEqual(payload["capability"], "extract")
        self.assertEqual(payload["input"]["urls"], ["https://example.com/a"])
        self.assertNotIn("query", payload["input"])
        self.assertEqual(RequestV3.from_dict(payload), request)

    def test_cross_capability_input_is_rejected(self):
        with self.assertRaises(ValueError):
            RequestV3.from_dict(
                {
                    "contract_version": "3.0",
                    "capability": "search",
                    "input": {"urls": ["https://example.com"]},
                }
            )

    def test_attempt_error_and_skip_invariants(self):
        with self.assertRaises(ValueError):
            ProviderAttemptV3(
                attempt_id="a1",
                provider="serper",
                capability=Capability.SEARCH,
                outcome=AttemptOutcome.FAILED,
            )
        with self.assertRaises(ValueError):
            ProviderAttemptV3(
                attempt_id="a2",
                provider="serper",
                capability=Capability.SEARCH,
                outcome=AttemptOutcome.SKIPPED,
            )

    def test_response_roundtrip_and_failed_response_requires_error(self):
        response = ResponseV3(
            request_id="req_search_1",
            capability=Capability.SEARCH,
            status=ResponseStatus.OK,
            results=[],
            provider_attempts=[],
            routing_receipt=CLASSIC_RECEIPT,
            cache_status={"disposition": CacheDisposition.MISS.value},
        )
        self.assertEqual(ResponseV3.from_dict(response.to_dict()), response)
        with self.assertRaises(ValueError):
            ResponseV3(
                request_id="req_failed",
                capability=Capability.SEARCH,
                status=ResponseStatus.FAILED,
                results=[],
                provider_attempts=[],
                routing_receipt=CLASSIC_RECEIPT,
                cache_status={"disposition": CacheDisposition.MISS.value},
            )

    def test_degraded_response_requires_enumerated_warning(self):
        with self.assertRaises(ValueError):
            ResponseV3(
                request_id="req_degraded",
                capability=Capability.SEARCH,
                status=ResponseStatus.DEGRADED,
                results=[],
                provider_attempts=[],
                routing_receipt=CLASSIC_RECEIPT,
                cache_status={"disposition": CacheDisposition.STALE_HIT.value},
                warnings=[{"code": "wsp.warning.misc", "message": "not typed"}],
            )
        response = ResponseV3(
            request_id="req_degraded",
            capability=Capability.SEARCH,
            status=ResponseStatus.DEGRADED,
            results=[],
            provider_attempts=[],
            routing_receipt=CLASSIC_RECEIPT,
            cache_status={"disposition": CacheDisposition.STALE_HIT.value},
            warnings=[
                {
                    "code": DegradedReason.SERVED_STALE.value,
                    "message": "served stale cache",
                }
            ],
        )
        self.assertEqual(response.status, ResponseStatus.DEGRADED)

    def test_schema_enum_values_match_python(self):
        request_schema = json.loads((SCHEMA_DIR / "request.schema.json").read_text())
        response_schema = json.loads((SCHEMA_DIR / "response.schema.json").read_text())
        self.assertEqual(
            request_schema["$defs"]["Capability"]["enum"],
            [item.value for item in Capability],
        )
        defs = response_schema["$defs"]
        self.assertEqual(
            defs["Capability"]["enum"], [item.value for item in Capability]
        )
        self.assertIn("dedup_clusters", response_schema["required"])
        self.assertEqual(
            defs["DegradedReason"]["enum"],
            [item.value for item in DegradedReason],
        )
        self.assertEqual(
            defs["ErrorClass"]["enum"], [item.value for item in ErrorClass]
        )
        self.assertEqual(
            defs["AttemptOutcome"]["enum"], [item.value for item in AttemptOutcome]
        )
        self.assertEqual(
            defs["SkipReason"]["enum"], [item.value for item in SkipReason]
        )
        self.assertEqual(
            defs["FallbackReason"]["enum"], [item.value for item in FallbackReason]
        )

    def test_golden_skeletons_validate_when_jsonschema_is_available(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is optional")
        request_schema = json.loads((SCHEMA_DIR / "request.schema.json").read_text())
        response_schema = json.loads((SCHEMA_DIR / "response.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(request_schema)
        jsonschema.Draft202012Validator.check_schema(response_schema)
        jsonschema.validate(RequestV3.search("test query").to_dict(), request_schema)
        jsonschema.validate(
            ResponseV3(
                request_id="req_1",
                capability=Capability.SEARCH,
                status=ResponseStatus.OK,
                results=[],
                provider_attempts=[],
                routing_receipt=CLASSIC_RECEIPT,
                cache_status={"disposition": CacheDisposition.MISS.value},
            ).to_dict(),
            response_schema,
        )
        fixture_dir = ROOT / "tests" / "fixtures" / "v3"
        for fixture_path in sorted(fixture_dir.glob("*.json")):
            with self.subTest(fixture=fixture_path.name):
                jsonschema.validate(
                    json.loads(fixture_path.read_text()), response_schema
                )

    def test_schema_enforces_nfc_marker_and_typed_degrade_reason(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is optional")
        response_schema = json.loads((SCHEMA_DIR / "response.schema.json").read_text())
        fixture_dir = ROOT / "tests" / "fixtures" / "v3"

        extract_payload = json.loads(
            (fixture_dir / "02_extract_success.json").read_text()
        )
        self.assertEqual(
            unicodedata.normalize("NFC", extract_payload["results"][0]["text"]),
            extract_payload["results"][0]["text"],
        )
        extract_payload["results"][0].pop("text_normalization")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(extract_payload, response_schema)

        degraded_payload = json.loads((fixture_dir / "05_degraded.json").read_text())
        degraded_payload["warnings"][0]["code"] = "wsp.warning.misc"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(degraded_payload, response_schema)


if __name__ == "__main__":
    unittest.main()
