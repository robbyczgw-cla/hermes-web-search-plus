"""An empty provider account is reported as out of credits, not a rate limit."""
from __future__ import annotations

import io
from urllib.error import HTTPError

import pytest

from wsp_core import http_client
from wsp_core.errors_v3 import ErrorClass, classify_provider_error


def _err(code, body):
    return HTTPError("https://api.example.test/search", code, "x", {}, io.BytesIO(body))


def test_linkup_style_429_without_funds_is_quota():
    body = b'{"error":{"code":"INSUFFICIENT_FUNDS_CREDITS","message":"does not have enough funds"},"statusCode":429}'
    with pytest.raises(http_client.ProviderRequestError) as info:
        http_client._raise_provider_http_error(_err(429, body))
    assert info.value.status_code == 402
    assert not info.value.transient
    assert "Out of credits" in str(info.value)
    assert classify_provider_error(info.value, provider="linkup").error_class is ErrorClass.QUOTA


def test_plain_429_stays_a_rate_limit():
    with pytest.raises(http_client.ProviderRequestError) as info:
        http_client._raise_provider_http_error(_err(429, b'{"error":"slow down"}'))
    assert info.value.status_code == 429
    assert classify_provider_error(info.value, provider="brave").error_class is ErrorClass.RATE_LIMIT


def test_tool_text_names_the_failing_provider():
    from plugin_loader import load_plugin

    wsp = load_plugin("wsp_plugin_out_of_credit_under_test")
    text = wsp._format_results({
        "error": "All providers failed",
        "results": [],
        "provider_errors": [{"provider": "linkup", "error": "Provider quota is exhausted"}],
    })
    assert text.splitlines()[0] == "Search error: All providers failed"
    assert "- linkup: Provider quota is exhausted" in text
