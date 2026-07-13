"""Formal provider-adapter protocol and runtime conformance gates.

The provider registry remains metadata-only.  This module defines the callable
boundary implemented by ``provider_dispatch`` and validates provider result
envelopes before orchestration trusts or mutates them.
"""

from __future__ import annotations

import inspect
from typing import Any, Mapping, Protocol, runtime_checkable

from errors_v3 import ProviderContractFailure


SEARCH_ADAPTER_PARAMETERS = (
    "search_module",
    "prov",
    "args",
    "key",
    "config",
    "routing_info",
)
EXTRACT_ADAPTER_PARAMETERS = (
    "extract_module",
    "prov",
    "urls",
    "key",
    "output_format",
    "include_images",
    "include_raw_html",
    "render_js",
    "config",
    "keyless_allowed",
)


@runtime_checkable
class SearchAdapter(Protocol):
    """Callable contract for one search-provider dispatch adapter."""

    def __call__(
        self,
        search_module: Any,
        prov: str,
        args: Any,
        key: str | None,
        config: Mapping[str, Any],
        routing_info: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@runtime_checkable
class ExtractAdapter(Protocol):
    """Callable contract for one extraction-provider dispatch adapter."""

    def __call__(
        self,
        extract_module: Any,
        prov: str,
        urls: list[str],
        key: str | None,
        output_format: str,
        include_images: bool,
        include_raw_html: bool,
        render_js: bool,
        config: Mapping[str, Any],
        keyless_allowed: bool,
    ) -> dict[str, Any]: ...


def _signature_matches(adapter: Any, expected: tuple[str, ...]) -> bool:
    if not callable(adapter):
        return False
    try:
        parameters = tuple(inspect.signature(adapter).parameters.values())
    except (TypeError, ValueError):
        return False
    return tuple(parameter.name for parameter in parameters) == expected and all(
        parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        and parameter.default is inspect.Parameter.empty
        for parameter in parameters
    )


def dispatch_conformance_errors(
    search_dispatch: Mapping[str, Any],
    extract_dispatch: Mapping[str, Any],
    provider_specs: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return deterministic, path-free protocol violations for dispatch tables."""

    errors: list[str] = []
    contracts = (
        (
            "search",
            search_dispatch,
            {provider for provider, spec in provider_specs.items() if spec.supports_search},
            SEARCH_ADAPTER_PARAMETERS,
        ),
        (
            "extract",
            extract_dispatch,
            {provider for provider, spec in provider_specs.items() if spec.supports_extract},
            EXTRACT_ADAPTER_PARAMETERS,
        ),
    )
    for capability, dispatch, expected_providers, expected_parameters in contracts:
        actual_providers = set(dispatch)
        errors.extend(
            f"{capability}:missing:{provider}"
            for provider in sorted(expected_providers - actual_providers)
        )
        errors.extend(
            f"{capability}:unexpected:{provider}"
            for provider in sorted(actual_providers - expected_providers)
        )
        for provider, adapter in sorted(dispatch.items()):
            if not _signature_matches(adapter, expected_parameters):
                errors.append(
                    f"{capability}:signature:{provider}:"
                    + ",".join(expected_parameters)
                )
    return tuple(errors)


def assert_dispatch_conformance(
    search_dispatch: Mapping[str, Any],
    extract_dispatch: Mapping[str, Any],
    provider_specs: Mapping[str, Any],
) -> None:
    """Fail module initialization when registry and adapters drift apart."""

    errors = dispatch_conformance_errors(
        search_dispatch, extract_dispatch, provider_specs
    )
    if errors:
        raise RuntimeError("provider adapter protocol violation: " + ";".join(errors))


def _contract_failure(code: str) -> ProviderContractFailure:
    return ProviderContractFailure(code)


def validate_adapter_result(
    provider: str,
    capability: str,
    payload: Any,
) -> dict[str, Any]:
    """Validate a source-only provider envelope before orchestration consumes it.

    Errors contain stable codes only.  Provider payload fragments, URLs, query
    text, and upstream messages are never copied into exceptions.
    """

    if capability not in {"search", "extract"}:
        raise _contract_failure("unsupported_capability")
    if not isinstance(payload, dict):
        raise _contract_failure("envelope_not_mutable_mapping")
    if payload.get("provider") != provider:
        raise _contract_failure("provider_mismatch")

    results = payload.get("results")
    if not isinstance(results, list):
        raise _contract_failure("results_not_list")
    for item in results:
        if not isinstance(item, dict):
            raise _contract_failure("result_not_mapping")
        url = item.get("url")
        if not isinstance(url, str) or (capability == "search" and not url):
            raise _contract_failure("result_url_invalid")

    answer = payload.get("answer")
    if answer not in {None, ""}:
        raise _contract_failure("non_source_answer")

    if capability == "search":
        if not isinstance(payload.get("query"), str):
            raise _contract_failure("search_query_invalid")
        if "images" in payload and not isinstance(payload["images"], list):
            raise _contract_failure("search_images_invalid")
        if "metadata" in payload and not isinstance(payload["metadata"], dict):
            raise _contract_failure("search_metadata_invalid")

    return payload
