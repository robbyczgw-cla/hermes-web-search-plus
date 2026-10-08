"""Typed, versioned cache identity for request-exact v3 extraction evidence."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


# This is deliberately independent from the response-envelope schema version.
# Increment it whenever ``canonical_form`` changes. Version 4 is the first
# typed identity after the unversioned 3.0.2 extraction-cache material.
EXTRACTION_CACHE_IDENTITY_VERSION = 6


def _canonical_value(value: Any) -> Any:
    """Return a JSON-only, NFC-normalized value or fail closed."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cache identity cannot contain a non-finite float")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("cache identity object keys must be strings")
            normalized[unicodedata.normalize("NFC", key)] = _canonical_value(child)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise ValueError(f"cache identity cannot contain {type(value).__name__}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize an identity in the one form used for its SHA-256 key."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class ExtractionCacheIdentityV3:
    """Every execution input that can change extraction evidence or retention."""

    requested_urls: tuple[str, ...]
    attempt_budget: Mapping[str, Any]
    effective_context_limits: Mapping[str, Any]
    output_format: str
    include_images: bool
    include_raw_html: bool
    render_js: bool
    semantic_spans: Mapping[str, Any]
    provider_selection: Mapping[str, Any]
    provider_endpoint_config: Mapping[str, Any]
    url_policy: Mapping[str, Any]
    storage_policy: Mapping[str, Any]
    identity_version: int = EXTRACTION_CACHE_IDENTITY_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.identity_version, bool)
            or not isinstance(self.identity_version, int)
        ):
            raise ValueError("identity_version must be an integer")
        if self.identity_version != EXTRACTION_CACHE_IDENTITY_VERSION:
            raise ValueError("unsupported extraction cache identity_version")
        if not self.requested_urls or not all(
            isinstance(url, str) and url for url in self.requested_urls
        ):
            raise ValueError("requested_urls must be a non-empty string sequence")
        if not isinstance(self.output_format, str) or not self.output_format:
            raise ValueError("output_format must be a non-empty string")
        if not all(
            isinstance(value, bool)
            for value in (
                self.include_images,
                self.include_raw_html,
                self.render_js,
            )
        ):
            raise ValueError("extraction cache feature controls must be booleans")
        # Validate every nested value now rather than discovering it after a
        # provider result has already been obtained.
        self.canonical_form()

    def canonical_form(self) -> dict[str, Any]:
        """Return the complete, stable object hashed to form an entry key."""
        return _canonical_value(
            {
                "identity_version": self.identity_version,
                "capability": "extract",
                # The sequence is preserved. Request order affects both
                # provider input and bounded-context omitted-url reporting.
                "requested_urls": list(self.requested_urls),
                "attempt_budget": dict(self.attempt_budget),
                "effective_context_limits": dict(self.effective_context_limits),
                "output_format": self.output_format,
                "include_images": self.include_images,
                "include_raw_html": self.include_raw_html,
                "render_js": self.render_js,
                "semantic_spans": dict(self.semantic_spans),
                "provider_selection": dict(self.provider_selection),
                "provider_endpoint_config": dict(self.provider_endpoint_config),
                "url_policy": dict(self.url_policy),
                "storage_policy": dict(self.storage_policy),
            }
        )

    @property
    def key(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.canonical_form())).hexdigest()

    @classmethod
    def from_canonical_form(
        cls, value: Mapping[str, Any]
    ) -> "ExtractionCacheIdentityV3":
        expected = {
            "identity_version",
            "capability",
            "requested_urls",
            "attempt_budget",
            "effective_context_limits",
            "output_format",
            "include_images",
            "include_raw_html",
            "render_js",
            "semantic_spans",
            "provider_selection",
            "provider_endpoint_config",
            "url_policy",
            "storage_policy",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("extraction cache identity has invalid fields")
        if value.get("capability") != "extract":
            raise ValueError("extraction cache identity capability is invalid")
        version = value.get("identity_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("identity_version must be an integer")
        if version != EXTRACTION_CACHE_IDENTITY_VERSION:
            raise ValueError("unsupported extraction cache identity_version")
        urls = value.get("requested_urls")
        if not isinstance(urls, list):
            raise ValueError("requested_urls must be an array")
        mappings = (
            "attempt_budget",
            "effective_context_limits",
            "semantic_spans",
            "provider_selection",
            "provider_endpoint_config",
            "url_policy",
            "storage_policy",
        )
        if any(not isinstance(value.get(name), Mapping) for name in mappings):
            raise ValueError("extraction cache identity component must be an object")
        return cls(
            requested_urls=tuple(urls),
            attempt_budget=dict(value["attempt_budget"]),
            effective_context_limits=dict(value["effective_context_limits"]),
            output_format=value["output_format"],
            include_images=value["include_images"],
            include_raw_html=value["include_raw_html"],
            render_js=value["render_js"],
            semantic_spans=dict(value["semantic_spans"]),
            provider_selection=dict(value["provider_selection"]),
            provider_endpoint_config=dict(value["provider_endpoint_config"]),
            url_policy=dict(value["url_policy"]),
            storage_policy=dict(value["storage_policy"]),
            identity_version=version,
        )
