"""SearXNG credentials never enter durable endpoint diagnostics."""
import json
import sqlite3
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from wsp_core import provider_health, providers
from wsp_core.attempt_engine_v3 import provider_attempt_context
from wsp_core.contract_v3 import Capability, ErrorClass
from wsp_core.http_client import ProviderRequestError
from wsp_core.state_store_v3 import SQLiteStateStore

URL = "https://fixture-user:fixture-password@example.com:8443/private-token?token=fixture-query"
ORIGIN = "https://example.com:8443"


@pytest.mark.parametrize("field", ["endpoint", "base_url", "url"])
def test_circuit_endpoint_persists_only_origin(tmp_path, field):
    store = SQLiteStateStore(tmp_path / "state.sqlite3")
    context = provider_attempt_context(store, "searxng", Capability.SEARCH, {field: URL}, URL, budget_scope="fixture")
    store.record_failure(context.circuit_key, ErrorClass.AUTH, now=100)
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT endpoint, credential_fingerprint FROM circuit_state").fetchone()
    assert row[0] == ORIGIN
    assert "fixture" not in str(row)
    assert b"fixture-password" not in store.path.read_bytes()


def test_searxng_error_does_not_echo_url_or_transport_reason(monkeypatch):
    def fail(*args, **kwargs):
        raise URLError("connection failed for " + URL)

    monkeypatch.setattr(providers, "urlopen", fail)
    with pytest.raises(ProviderRequestError) as caught:
        providers.search_searxng("q", URL)
    assert ORIGIN in str(caught.value)
    assert "fixture" not in str(caught.value)
    assert "private-token" not in str(caught.value)
    provider_health.mark_provider_failure("searxng", str(caught.value))
    assert "fixture" not in provider_health.PROVIDER_HEALTH_FILE.read_text(encoding="utf-8")
    assert "private-token" not in provider_health.PROVIDER_HEALTH_FILE.read_text(encoding="utf-8")


def test_searxng_metadata_contains_only_origin(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps({"results": []}).encode()
    monkeypatch.setattr(providers, "urlopen", lambda *a, **k: response)
    assert providers.search_searxng("q", URL)["metadata"]["instance_url"] == ORIGIN
