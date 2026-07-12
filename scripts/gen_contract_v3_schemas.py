#!/usr/bin/env python3
"""Generate self-contained Draft 2020-12 schemas for the frozen v3 contract."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contract_v3 import (  # noqa: E402
    AttemptOutcome,
    CacheDisposition,
    Capability,
    CircuitState,
    DegradedReason,
    ErrorClass,
    FallbackReason,
    ResponseStatus,
    SkipReason,
)

OUT = ROOT / "schemas" / "v3"


def enum_schema(enum_cls):
    return {"type": "string", "enum": [item.value for item in enum_cls]}


def obj(properties, required=(), *, additional=False):
    value = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional,
    }
    if required:
        value["required"] = list(required)
    return value


error = obj(
    {
        "error_class": {"$ref": "#/$defs/ErrorClass"},
        "code": {"type": "string", "pattern": "^wsp\\.[a-z0-9_]+(?:\\.[a-z0-9_]+)*$"},
        "message": {"type": "string", "minLength": 1},
        "retryable": {"type": "boolean"},
        "provider": {"type": "string", "minLength": 1},
        "http_status": {"type": "integer", "minimum": 100, "maximum": 599},
        "retry_after_seconds": {"type": "number", "minimum": 0},
        "details": {"type": "object"},
    },
    ("error_class", "code", "message", "retryable"),
)

attempt = obj(
    {
        "attempt_id": {"type": "string", "minLength": 1},
        "provider": {"type": "string", "minLength": 1},
        "capability": {"$ref": "#/$defs/Capability"},
        "outcome": {"$ref": "#/$defs/AttemptOutcome"},
        "retry_count": {"type": "integer", "minimum": 0},
        "result_count": {"type": "integer", "minimum": 0},
        "started_at": {"type": "string", "format": "date-time"},
        "duration_ms": {"type": "integer", "minimum": 0},
        "error": {"$ref": "#/$defs/ErrorV3"},
        "skip_reason": {"$ref": "#/$defs/SkipReason"},
        "budget_decision": {
            "type": "string",
            "enum": ["allowed", "reserved", "blocked", "unknown"],
        },
        "circuit_state_before": {"$ref": "#/$defs/CircuitState"},
        "circuit_state_after": {"$ref": "#/$defs/CircuitState"},
    },
    (
        "attempt_id",
        "provider",
        "capability",
        "outcome",
        "retry_count",
        "result_count",
        "circuit_state_before",
        "circuit_state_after",
    ),
)
attempt["allOf"] = [
    {
        "if": {"properties": {"outcome": {"const": "failed"}}, "required": ["outcome"]},
        "then": {"required": ["error"]},
    },
    {
        "if": {
            "properties": {"outcome": {"const": "skipped"}},
            "required": ["outcome"],
        },
        "then": {"required": ["skip_reason"]},
    },
]
provider_try_error = obj(
    {
        "error_class": {"$ref": "#/$defs/ErrorClass"},
        "code": {"type": "string", "pattern": "^wsp\\.[a-z0-9_]+(?:\\.[a-z0-9_]+)*$"},
        "http_status": {"type": ["integer", "null"], "minimum": 100, "maximum": 599},
        "retryable": {"type": "boolean"},
        "retry_after_ms": {"type": ["integer", "null"], "minimum": 0},
    },
    ("error_class", "code", "http_status", "retryable", "retry_after_ms"),
)
provider_try = obj(
    {
        "try_number": {"type": "integer", "minimum": 1},
        "started_at": {"type": "string", "format": "date-time"},
        "duration_ms": {"type": "integer", "minimum": 0},
        "outcome": {"type": "string", "enum": ["success", "error"]},
        "error": {"anyOf": [{"$ref": "#/$defs/ProviderTryErrorV3"}, {"type": "null"}]},
    },
    ("try_number", "started_at", "duration_ms", "outcome", "error"),
)
attempt["properties"].update(
    {
        "endpoint_id": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "enum": ["attempted", "skipped"]},
        "tries": {"type": "array", "items": {"$ref": "#/$defs/ProviderTryV3"}},
    }
)
attempt["required"].extend(["endpoint_id", "decision", "tries"])
attempt["allOf"].extend(
    [
        {
            "if": {"properties": {"decision": {"const": "skipped"}}, "required": ["decision"]},
            "then": {
                "required": ["skip_reason"],
                "properties": {"tries": {"maxItems": 0}},
            },
        },
        {
            "if": {"properties": {"decision": {"const": "attempted"}}, "required": ["decision"]},
            "then": {"properties": {"tries": {"minItems": 1}}},
        },
    ]
)

request_defs = {
    "Capability": enum_schema(Capability),
    "SearchInput": obj({"query": {"type": "string", "minLength": 1}}, ("query",)),
    "ExtractInput": obj(
        {
            "urls": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {"type": "string", "format": "uri"},
                "uniqueItems": True,
            }
        },
        ("urls",),
    ),
    "SearchOptions": obj(
        {
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
            "freshness": {"type": "string", "enum": ["day", "week", "month", "year"]},
            "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
            "search_type": {"type": "string", "enum": ["search", "news"]},
            "depth": {
                "type": "string",
                "enum": ["normal"],
            },
            "mode": {"type": "string", "enum": ["normal", "research"]},
            "quality_report": {"type": "boolean"},
            "research_time_budget": {
                "type": "number",
                "minimum": 1,
                "maximum": 75,
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "locale": obj(
                {
                    "country": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
                    "language": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
                }
            ),
        }
    ),
    "ExtractOptions": obj(
        {
            "output_format": {"type": "string", "enum": ["markdown", "html"]},
            "include_images": {"type": "boolean"},
            "include_raw_html": {"type": "boolean"},
            "render_js": {"type": "boolean"},
            "max_urls": {"type": "integer"},
            "max_context_chars": {"type": "integer"},
        }
    ),
    "CacheRequest": obj(
        {
            "mode": {"type": "string", "enum": ["prefer", "bypass", "only"]},
            "ttl_seconds": {"type": "integer", "minimum": 0},
            "allow_stale_seconds": {"type": "integer", "minimum": 0},
        }
    ),
    "RoutingRequest": obj(
        {
            "mode": {"type": "string", "enum": ["auto", "fixed"]},
            "provider": {"type": "string", "minLength": 1},
            "allow_fallback": {"type": "boolean"},
            "policy_mode": {"type": "string", "enum": ["classic", "shadow"]},
        }
    ),
    "BudgetRequest": obj(
        {
            "max_provider_attempts": {"type": "integer", "minimum": 1, "maximum": 32},
            "max_wall_time_ms": {"type": "integer", "minimum": 1},
            "max_cost_microunits": {"type": "integer", "minimum": 0},
        }
    ),
    "ClientNegotiation": obj(
        {
            "accept_contract_versions": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": ["3.0", "2.x"]},
                "uniqueItems": True,
            },
            "accept_features": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "provider_attempts",
                        "dedup_clusters",
                        "observations",
                        "policy_actions",
                        "source_diversity",
                        "mechanical_text_offsets",
                        "stale_cache",
                    ],
                },
                "uniqueItems": True,
            },
        }
    ),
}

request_schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://websearchplus.xyz/schema/v3/request.schema.json",
    "title": "RequestV3",
    "type": "object",
    "additionalProperties": False,
    "required": ["contract_version", "capability", "input"],
    "properties": {
        "contract_version": {"const": "3.0"},
        "request_id": {"type": "string", "minLength": 1},
        "capability": {"$ref": "#/$defs/Capability"},
        "input": {"type": "object"},
        "options": {"type": "object"},
        "cache": {"$ref": "#/$defs/CacheRequest"},
        "routing": {"$ref": "#/$defs/RoutingRequest"},
        "budget": {"$ref": "#/$defs/BudgetRequest"},
        "client": {"$ref": "#/$defs/ClientNegotiation"},
    },
    "allOf": [
        {
            "if": {
                "properties": {"capability": {"const": "search"}},
                "required": ["capability"],
            },
            "then": {
                "properties": {
                    "input": {"$ref": "#/$defs/SearchInput"},
                    "options": {"$ref": "#/$defs/SearchOptions"},
                }
            },
        },
        {
            "if": {
                "properties": {"capability": {"const": "extract"}},
                "required": ["capability"],
            },
            "then": {
                "properties": {
                    "input": {"$ref": "#/$defs/ExtractInput"},
                    "options": {"$ref": "#/$defs/ExtractOptions"},
                }
            },
        },
    ],
    "$defs": request_defs,
}

response_defs = {
    "Capability": enum_schema(Capability),
    "ResponseStatus": enum_schema(ResponseStatus),
    "DegradedReason": enum_schema(DegradedReason),
    "ErrorClass": enum_schema(ErrorClass),
    "AttemptOutcome": enum_schema(AttemptOutcome),
    "SkipReason": enum_schema(SkipReason),
    "FallbackReason": enum_schema(FallbackReason),
    "CacheDisposition": enum_schema(CacheDisposition),
    "CircuitState": enum_schema(CircuitState),
    "ErrorV3": error,
    "ProviderTryErrorV3": provider_try_error,
    "ProviderTryV3": provider_try,
    "ProviderAttemptV3": attempt,
    "UrlV3": obj(
        {
            "observed": {"type": "string", "format": "uri"},
            "canonical": {"type": "string", "format": "uri"},
        },
        ("observed", "canonical"),
    ),
    "ProviderScoreV3": obj(
        {
            "value": {"type": "number"},
            "semantics": {
                "type": "string",
                "enum": ["provider_local_relevance", "unknown"],
            },
        },
        ("value", "semantics"),
    ),
    "PublishedAtV3": obj(
        {
            "raw": {"type": "string"},
            "normalized": {"type": ["string", "null"], "format": "date-time"},
        },
        ("raw", "normalized"),
    ),
    "ObservationV3": obj(
        {
            "observation_id": {"type": "string", "pattern": "^obs_"},
            "provider_attempt_id": {"type": "string", "minLength": 1},
            "provider_result_index": {"type": "integer", "minimum": 0},
            "provider": {"type": "string", "minLength": 1},
            "endpoint_id": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": ["search_result", "extracted_document"]},
            "url": {"$ref": "#/$defs/UrlV3"},
            "title": {"type": ["string", "null"]},
            "snippet": {"type": ["string", "null"]},
            "text": {"type": ["string", "null"]},
            "provider_rank": {"type": ["integer", "null"], "minimum": 1},
            "provider_score": {
                "anyOf": [{"$ref": "#/$defs/ProviderScoreV3"}, {"type": "null"}]
            },
            "published_at": {
                "anyOf": [{"$ref": "#/$defs/PublishedAtV3"}, {"type": "null"}]
            },
            "provider_fields": {
                "type": "object",
                "maxProperties": 1,
                "additionalProperties": {"type": "object"},
            },
        },
        (
            "observation_id", "provider_attempt_id", "provider_result_index",
            "provider", "endpoint_id", "kind", "url", "title", "snippet",
            "text", "provider_rank", "provider_score", "published_at",
            "provider_fields",
        ),
    ),
    "SegmentV3": obj(
        {
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "minLength": 1},
        },
        ("start", "end", "text"),
    ),
    "ProjectedProvenanceV3": obj(
        {
            "observation_id": {"type": "string", "pattern": "^obs_"},
            "source_field": {"type": "string", "enum": ["title", "snippet", "text"]},
            "transformations": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "whitespace_norm", "deterministic_truncation",
                        "mechanical_segmentation", "image_base64_replace",
                    ],
                },
            },
        },
        ("observation_id", "source_field", "transformations"),
    ),
    "ProjectedTextV3": obj(
        {
            "text": {"type": "string"},
            "text_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "origin": {"type": "string", "enum": ["provider", "engine"]},
            "provenance": {"$ref": "#/$defs/ProjectedProvenanceV3"},
            "segments": {"type": "array", "items": {"$ref": "#/$defs/SegmentV3"}},
        },
        ("text", "text_sha256", "origin", "provenance", "segments"),
    ),
    "ResultV3": obj(
        {
            "result_id": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": ["search_result", "extracted_document"]},
            "engine_rank": {"type": "integer", "minimum": 1},
            "representative_observation_id": {"type": "string", "pattern": "^obs_"},
            "observation_ids": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "pattern": "^obs_"},
            },
            "dedup_cluster_id": {"type": "string", "minLength": 1},
            "url": {"$ref": "#/$defs/UrlV3"},
            "title": {"anyOf": [{"$ref": "#/$defs/ProjectedTextV3"}, {"type": "null"}]},
            "snippet": {"anyOf": [{"$ref": "#/$defs/ProjectedTextV3"}, {"type": "null"}]},
            "text": {"anyOf": [{"$ref": "#/$defs/ProjectedTextV3"}, {"type": "null"}]},
        },
        (
            "result_id", "kind", "engine_rank", "representative_observation_id",
            "observation_ids", "dedup_cluster_id", "url", "title", "snippet", "text",
        ),
    ),
    "CacheStatus": obj(
        {
            "disposition": {"$ref": "#/$defs/CacheDisposition"},
            "entry_id": {"type": "string"},
            "age_seconds": {"type": "integer", "minimum": 0},
            "ttl_seconds": {"type": "integer", "minimum": 0},
            "served_stale": {"type": "boolean"},
            "source_contract_version": {"type": "string", "enum": ["3.0", "2.x"]},
            "origin_execution_id": {"type": "string", "minLength": 1},
            "write_error": {"type": "string"},
        },
        ("disposition",),
    ),
    "RoutingReceipt": obj(
        {
            "policy_id": {"type": "string", "minLength": 1},
            "policy_revision": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "enum": ["classic", "shadow"]},
            "candidate_order": {"type": "array", "items": {"type": "string"}},
            "selected_provider": {"type": ["string", "null"]},
            "fallback_reason": {"$ref": "#/$defs/FallbackReason"},
            "shadow": {"type": "object"},
        },
        (
            "policy_id",
            "policy_revision",
            "mode",
            "candidate_order",
            "selected_provider",
            "fallback_reason",
        ),
    ),
    "SourceDiversityV3": obj(
        {
            "method": {"type": "string", "minLength": 1},
            "method_version": {"type": "string", "minLength": 1},
            "method_degraded": {"type": "boolean"},
            "provider_count": {"type": "integer", "minimum": 0},
            "host_count": {"type": "integer", "minimum": 0},
            "source_family_count": {"type": "integer", "minimum": 0},
            "unique_cluster_count": {"type": "integer", "minimum": 0},
        },
        (
            "method", "method_version", "method_degraded", "provider_count",
            "host_count", "source_family_count", "unique_cluster_count",
        ),
    ),
    "ExtractLimitsV3": obj(
        {
            "requested_url_count": {"type": "integer", "minimum": 1},
            "processed_urls": {
                "type": "array",
                "items": {"type": "string", "format": "uri"},
            },
            "omitted_urls": {
                "type": "array",
                "items": {"type": "string", "format": "uri"},
            },
            "omitted_url_count": {"type": "integer", "minimum": 0},
            "max_urls": {"type": "integer", "minimum": 1, "maximum": 50},
            "max_context_chars": {
                "type": "integer", "minimum": 1000, "maximum": 200000,
            },
            "context_chars_returned": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
        },
        (
            "requested_url_count", "processed_urls", "omitted_urls",
            "omitted_url_count", "max_urls", "max_context_chars",
            "context_chars_returned", "truncated",
        ),
    ),
    "LimitsAppliedV3": obj(
        {
            "max_results": {"type": ["integer", "null"], "minimum": 1},
            "extract": {"$ref": "#/$defs/ExtractLimitsV3"},
        }
    ),
    "StoredContentReferenceV3": obj(
        {
            "store": {"const": "web_text_v3"},
            "key": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "media_type": {"const": "text/markdown"},
        },
        ("store", "key", "media_type"),
    ),
    "StoredContentV3": obj(
        {
            "observation_id": {"type": "string", "pattern": "^obs_"},
            "storage_attempted": {"const": True},
            "storage_succeeded": {"type": "boolean"},
            "reference": {
                "anyOf": [
                    {"$ref": "#/$defs/StoredContentReferenceV3"},
                    {"type": "null"},
                ]
            },
            "full_text_sha256": {
                "type": ["string", "null"],
                "pattern": "^[a-f0-9]{64}$",
            },
            "full_text_chars": {"type": ["integer", "null"], "minimum": 0},
        },
        (
            "observation_id", "storage_attempted", "storage_succeeded",
            "reference", "full_text_sha256", "full_text_chars",
        ),
    ),
    "PolicyActionV3": obj(
        {
            "action": {
                "type": "string",
                "enum": [
                    "excluded", "reranked", "demoted",
                    "selected_as_representative", "truncated_by_limit",
                ],
            },
            "observation_id": {"type": "string", "pattern": "^obs_"},
            "reason": {
                "type": "string",
                "enum": [
                    "spam_domain", "intent_authority", "domain_diversity",
                    "dedup_representative", "max_results", "max_content_bytes",
                    "max_context_chars",
                ],
            },
        },
        ("action", "observation_id", "reason"),
    ),
    "EngineV3": obj(
        {
            "name": {"type": "string", "minLength": 1},
            "version": {"type": "string", "minLength": 1},
            "build_commit": {"type": "string", "minLength": 1},
        },
        ("name", "version", "build_commit"),
    ),
    "WarningV3": obj(
        {
            "code": {
                "type": "string",
                "pattern": "^wsp\\.[a-z0-9_]+(?:\\.[a-z0-9_]+)*$",
            },
            "message": {"type": "string", "minLength": 1},
            "reason": {"type": "string"},
            "details": {"type": "object"},
        },
        ("code", "message"),
    ),
}

response_defs["StoredContentV3"]["allOf"] = [
    {
        "if": {
            "properties": {"storage_succeeded": {"const": True}},
            "required": ["storage_succeeded"],
        },
        "then": {
            "properties": {
                "reference": {"$ref": "#/$defs/StoredContentReferenceV3"},
                "full_text_sha256": {
                    "type": "string", "pattern": "^[a-f0-9]{64}$",
                },
                "full_text_chars": {"type": "integer", "minimum": 0},
            }
        },
        "else": {
            "properties": {
                "reference": {"type": "null"},
                "full_text_sha256": {"type": "null"},
                "full_text_chars": {"type": "null"},
            }
        },
    }
]

response_schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://websearchplus.xyz/schema/v3/response.schema.json",
    "title": "ResponseV3",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version",
        "request_id",
        "execution_id",
        "capability",
        "status",
        "results",
        "observations",
        "policy_actions",
        "source_diversity",
        "provider_attempts",
        "routing_receipt",
        "cache_status",
        "limits_applied",
        "dedup_clusters",
        "warnings",
    ],
    "properties": {
        "contract_version": {"const": "3.0"},
        "request_id": {"type": "string", "minLength": 1},
        "execution_id": {"type": "string", "minLength": 1},
        "capability": {"$ref": "#/$defs/Capability"},
        "status": {"$ref": "#/$defs/ResponseStatus"},
        "results": {"type": "array", "items": {"$ref": "#/$defs/ResultV3"}},
        "observations": {"type": "array", "items": {"$ref": "#/$defs/ObservationV3"}},
        "policy_actions": {"type": "array", "items": {"$ref": "#/$defs/PolicyActionV3"}},
        "source_diversity": {"$ref": "#/$defs/SourceDiversityV3"},
        "engine": {"$ref": "#/$defs/EngineV3"},
        "provider_attempts": {
            "type": "array",
            "items": {"$ref": "#/$defs/ProviderAttemptV3"},
        },
        "routing_receipt": {"$ref": "#/$defs/RoutingReceipt"},
        "cache_status": {"$ref": "#/$defs/CacheStatus"},
        "limits_applied": {"$ref": "#/$defs/LimitsAppliedV3"},
        "stored_content": {
            "type": "array", "items": {"$ref": "#/$defs/StoredContentV3"},
        },
        "dedup_clusters": {"type": "array", "items": {"type": "object"}},
        "warnings": {"type": "array", "items": {"$ref": "#/$defs/WarningV3"}},
        "error": {"$ref": "#/$defs/ErrorV3"},
    },
    "allOf": [
        {
            "if": {
                "properties": {"status": {"const": "failed"}},
                "required": ["status"],
            },
            "then": {"required": ["error"]},
            "else": {"not": {"required": ["error"]}},
        },
        {
            "if": {
                "properties": {"status": {"const": "degraded"}},
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "warnings": {
                        "contains": {
                            "type": "object",
                            "required": ["code"],
                            "properties": {"code": {"$ref": "#/$defs/DegradedReason"}},
                        },
                        "minContains": 1,
                    }
                }
            },
        },
    ],
    "$defs": response_defs,
}

parser = argparse.ArgumentParser()
parser.add_argument(
    "--check",
    action="store_true",
    help="fail when committed schemas differ from generated output",
)
args = parser.parse_args()

OUT.mkdir(parents=True, exist_ok=True)
stale = []
for name, schema in (
    ("request.schema.json", request_schema),
    ("response.schema.json", response_schema),
):
    path = OUT / name
    rendered = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not path.exists() or path.read_text(encoding="utf-8") != rendered:
            stale.append(str(path))
    else:
        path.write_text(rendered, encoding="utf-8")
        print(f"generated {path}")

if stale:
    parser.error("stale generated schemas: " + ", ".join(stale))
if args.check:
    print("contract v3 schemas are current")
