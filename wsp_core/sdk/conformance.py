"""Network-free conformance checks shared by built-in and SDK providers."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from .api import extract_result, search_result, source_result


def provider_conformance_errors() -> tuple[str, ...]:
    """Return deterministic conformance failures for every registered spec.

    This deliberately exercises metadata, formal dispatch membership, envelope
    validation, keyless/env consistency, and missing-key classification without
    making a provider request.  It is equally strict for built-ins and modules
    discovered from ``providers.d``.
    """
    from ..config import ProviderConfigError, validate_api_key
    from ..provider_adapter_protocol import dispatch_conformance_errors, validate_adapter_result
    from ..provider_dispatch import EXTRACT_DISPATCH, SEARCH_DISPATCH
    from ..provider_registry import PROVIDER_SPECS

    errors = list(dispatch_conformance_errors(SEARCH_DISPATCH, EXTRACT_DISPATCH, PROVIDER_SPECS))
    for provider, spec in PROVIDER_SPECS.items():
        if not all((spec.provider, spec.env_var, spec.display_name, spec.description, spec.config_section)):
            errors.append(f"{provider}:incomplete_spec")
        if spec.provider != provider or spec.id != provider:
            errors.append(f"{provider}:id_mismatch")
        if spec.kind not in {"search", "extract", "both", "disabled"}:
            errors.append(f"{provider}:invalid_kind")
        if spec.supports_search:
            try:
                validate_adapter_result(
                    provider,
                    "search",
                    search_result(provider, "conformance", [source_result("https://example.invalid/source")]),
                )
            except Exception:
                errors.append(f"{provider}:search_envelope")
        if spec.supports_extract:
            try:
                validate_adapter_result(
                    provider,
                    "extract",
                    extract_result(provider, [source_result("https://example.invalid/source", content="evidence")]),
                )
            except Exception:
                errors.append(f"{provider}:extract_envelope")
        if spec.keyless and not spec.env_var:
            errors.append(f"{provider}:keyless_missing_env_var")
        with patch.dict(os.environ, {}, clear=True):
            try:
                validate_api_key(provider, {spec.config_section: {}})
            except ProviderConfigError as exc:
                try:
                    payload = json.loads(str(exc))
                except json.JSONDecodeError:
                    errors.append(f"{provider}:missing_key_non_deterministic")
                else:
                    if payload.get("provider") != provider or payload.get("env_var") != spec.env_var:
                        errors.append(f"{provider}:missing_key_mapping")
            else:
                errors.append(f"{provider}:missing_key_not_rejected")
    return tuple(errors)


def assert_provider_conformance() -> None:
    """Raise one compact assertion if any registered provider drifts."""
    errors = provider_conformance_errors()
    if errors:
        raise AssertionError("provider conformance failed: " + ";".join(errors))
