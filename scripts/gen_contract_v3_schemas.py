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

request_defs = {
    "Capability": enum_schema(Capability),
    "SearchInput": obj({"query": {"type": "string", "minLength": 1}}, ("query",)),
    "ExtractInput": obj(
        {
            "urls": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
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
                "enum": ["normal", "deep", "deep-reasoning"],
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
                        "source_independence_estimate",
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
    "ProviderAttemptV3": attempt,
    "ProvenanceObservation": obj(
        {
            "provider": {"type": "string", "minLength": 1},
            "source_url": {"type": "string", "format": "uri"},
            "retrieved_at": {"type": "string", "format": "date-time"},
            "provider_rank": {"type": "integer", "minimum": 1},
            "provider_result_id": {"type": "string"},
        },
        ("provider", "source_url", "retrieved_at"),
    ),
    "TextSegmentV3": obj(
        {
            "segment_id": {"type": "string", "minLength": 1},
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 0},
        },
        ("segment_id", "start", "end"),
    ),
    "SearchResultV3": obj(
        {
            "result_id": {"type": "string", "minLength": 1},
            "status": {"const": "ok"},
            "title": {"type": "string"},
            "url": {"type": "string", "format": "uri"},
            "canonical_url": {"type": "string", "format": "uri"},
            "snippet": {"type": "string"},
            "published_at": {"type": "string", "format": "date-time"},
            "cluster_id": {"type": "string"},
            "provenance": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/ProvenanceObservation"},
            },
        },
        ("result_id", "status", "title", "url", "canonical_url", "provenance"),
    ),
    "ExtractResultV3": obj(
        {
            "result_id": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": ["ok", "failed"]},
            "title": {"type": "string"},
            "url": {"type": "string", "format": "uri"},
            "canonical_url": {"type": "string", "format": "uri"},
            "text": {"type": "string"},
            "offset_unit": {"const": "unicode_codepoint"},
            "text_normalization": {"const": "NFC"},
            "segments": {"type": "array", "items": {"$ref": "#/$defs/TextSegmentV3"}},
            "provenance": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/ProvenanceObservation"},
            },
            "error": {"$ref": "#/$defs/ErrorV3"},
        },
        ("result_id", "status", "url", "canonical_url", "provenance"),
    ),
    "CacheStatus": obj(
        {
            "disposition": {"$ref": "#/$defs/CacheDisposition"},
            "entry_id": {"type": "string"},
            "age_seconds": {"type": "integer", "minimum": 0},
            "ttl_seconds": {"type": "integer", "minimum": 0},
            "served_stale": {"type": "boolean"},
            "source_contract_version": {"type": "string", "enum": ["3.0", "2.x"]},
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
    "IndependenceEstimate": obj(
        {
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "unique_cluster_count": {"type": "integer", "minimum": 0},
            "result_count": {"type": "integer", "minimum": 0},
            "source_family_count": {"type": "integer", "minimum": 0},
            "provider_count": {"type": "integer", "minimum": 0},
            "method": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "method_degraded": {"type": "boolean"},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        (
            "score",
            "unique_cluster_count",
            "result_count",
            "source_family_count",
            "provider_count",
            "method",
            "confidence",
            "method_degraded",
            "limitations",
        ),
    ),
    "WarningV3": obj(
        {
            "code": {
                "type": "string",
                "pattern": "^wsp\\.[a-z0-9_]+(?:\\.[a-z0-9_]+)*$",
            },
            "message": {"type": "string", "minLength": 1},
            "details": {"type": "object"},
        },
        ("code", "message"),
    ),
}
response_defs["ExtractResultV3"]["allOf"] = [
    {
        "if": {"properties": {"status": {"const": "ok"}}, "required": ["status"]},
        "then": {"required": ["text", "offset_unit", "text_normalization", "segments"]},
    },
    {
        "if": {"properties": {"status": {"const": "failed"}}, "required": ["status"]},
        "then": {"required": ["error"]},
    },
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
        "capability",
        "status",
        "results",
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
        "capability": {"$ref": "#/$defs/Capability"},
        "status": {"$ref": "#/$defs/ResponseStatus"},
        "results": {"type": "array"},
        "provider_attempts": {
            "type": "array",
            "items": {"$ref": "#/$defs/ProviderAttemptV3"},
        },
        "routing_receipt": {"$ref": "#/$defs/RoutingReceipt"},
        "cache_status": {"$ref": "#/$defs/CacheStatus"},
        "limits_applied": {"type": "object"},
        "dedup_clusters": {"type": "array", "items": {"type": "object"}},
        "source_independence_estimate": {"$ref": "#/$defs/IndependenceEstimate"},
        "warnings": {"type": "array", "items": {"$ref": "#/$defs/WarningV3"}},
        "error": {"$ref": "#/$defs/ErrorV3"},
    },
    "allOf": [
        {
            "if": {
                "properties": {"capability": {"const": "search"}},
                "required": ["capability"],
            },
            "then": {
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/SearchResultV3"},
                    }
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
                    "results": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/ExtractResultV3"},
                    }
                }
            },
        },
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
