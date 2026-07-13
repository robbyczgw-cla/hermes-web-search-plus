from __future__ import annotations

from typing import Any

import pytest

import provider_dispatch
import provider_registry
from errors_v3 import ProviderContractFailure
from provider_adapter_protocol import (
    EXTRACT_ADAPTER_PARAMETERS,
    SEARCH_ADAPTER_PARAMETERS,
    ExtractAdapter,
    SearchAdapter,
    dispatch_conformance_errors,
    validate_adapter_result,
)


def test_registered_dispatch_tables_satisfy_formal_protocol() -> None:
    assert dispatch_conformance_errors(
        provider_dispatch.SEARCH_DISPATCH,
        provider_dispatch.EXTRACT_DISPATCH,
        provider_registry.PROVIDER_SPECS,
    ) == ()
    assert all(
        isinstance(adapter, SearchAdapter)
        for adapter in provider_dispatch.SEARCH_DISPATCH.values()
    )
    assert all(
        isinstance(adapter, ExtractAdapter)
        for adapter in provider_dispatch.EXTRACT_DISPATCH.values()
    )


def test_dispatch_protocol_rejects_missing_extra_and_bad_signature() -> None:
    def bad_search_adapter(provider: str) -> dict[str, Any]:
        return {"provider": provider, "query": "q", "results": []}

    search_dispatch = dict(provider_dispatch.SEARCH_DISPATCH)
    search_dispatch.pop("serper")
    search_dispatch["not-registered"] = bad_search_adapter

    errors = dispatch_conformance_errors(
        search_dispatch,
        provider_dispatch.EXTRACT_DISPATCH,
        provider_registry.PROVIDER_SPECS,
    )

    assert "search:missing:serper" in errors
    assert "search:unexpected:not-registered" in errors
    assert (
        "search:signature:not-registered:"
        + ",".join(SEARCH_ADAPTER_PARAMETERS)
    ) in errors


def test_extract_signature_contract_is_explicit() -> None:
    assert EXTRACT_ADAPTER_PARAMETERS == (
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


@pytest.mark.parametrize(
    ("capability", "payload"),
    [
        ("search", None),
        ("search", {"provider": "serper", "query": "q", "results": {}}),
        ("search", {"provider": "brave", "query": "q", "results": []}),
        ("search", {"provider": "serper", "query": "q", "results": ["bad"]}),
        (
            "search",
            {
                "provider": "serper",
                "query": "q",
                "results": [{"url": "https://example.test"}],
                "answer": "synthetic prose",
            },
        ),
        ("extract", {"provider": "tavily", "results": [{"url": 42}]}),
    ],
)
def test_result_contract_rejects_malformed_or_non_source_payloads(
    capability: str, payload: Any
) -> None:
    with pytest.raises(ProviderContractFailure):
        validate_adapter_result("serper" if capability == "search" else "tavily", capability, payload)


def test_result_contract_accepts_capability_specific_source_envelopes() -> None:
    search_payload = {
        "provider": "serper",
        "query": "q",
        "results": [{"url": "https://example.test", "title": "Source"}],
        "images": [],
        "answer": "",
        "metadata": {},
    }
    extract_payload = {
        "provider": "tavily",
        "results": [
            {
                "url": "https://example.test",
                "title": "Source",
                "content": "Evidence",
                "raw_content": "Evidence",
            }
        ],
    }

    assert validate_adapter_result("serper", "search", search_payload) is search_payload
    assert validate_adapter_result("tavily", "extract", extract_payload) is extract_payload
