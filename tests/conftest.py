import os
import shutil
import socket

import pytest

import cache
import extract
import provider_stats
import search


@pytest.fixture(autouse=True)
def _example_dns_fixture(monkeypatch):
    """Mock the example.com/example.org hosts used by mocked extraction tests.

    Keep URL/IP security validation enabled, without requiring live DNS. Safety
    tests can still replace getaddrinfo themselves to exercise private addresses,
    resolution errors and rebinding. All other hosts retain the real resolver.
    """
    resolve = socket.getaddrinfo

    def fixture_address(host, port, *args, **kwargs):
        if host in {"example.com", "example.org"}:
            host = "93.184.216.34"  # Public-address fixture, not a live DNS claim.
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fixture_address)


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """Keep mutable runtime state out of real paths and isolate every test.

    Search tests record outcomes for mocked providers; without isolation those
    samples would pollute the operator's provider_stats.json and, worse, feed
    back into routing decisions and make routing tests order-dependent.
    """
    monkeypatch.setattr(provider_stats, "PROVIDER_STATS_FILE", tmp_path / "provider_stats.json")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(search, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(extract, "CACHE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _no_operator_donsetch(monkeypatch):
    """Never let a test find the developer's real DonSeTch binary.

    DonSeTch is resolved from DONSETCH_BIN or PATH. The real binary can start a
    local Chrome for browser-tier fetches, and setup/status tests probe it with
    ``donsetch --version`` - on a machine with DonSeTch installed every test
    run launched Chrome. Tests that need a binary pass an explicit path.
    """
    monkeypatch.delenv("DONSETCH_BIN", raising=False)
    real_which = shutil.which

    def which(cmd, *args, **kwargs):
        if os.path.basename(str(cmd)).lower().startswith("donsetch"):
            return None
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
