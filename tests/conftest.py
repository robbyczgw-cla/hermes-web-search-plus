import os
import shutil
import socket

import pytest

from wsp_core import cache
from wsp_core import extract
from wsp_core import provider_stats
from wsp_core import search


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


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """Fail any test that opens a real connection beyond loopback.

    Provider HTTP must be mocked. A test whose mock silently stops applying
    would otherwise reach the real provider with a fake key and still "pass"
    on the resulting error, so violations are recorded and failed at teardown
    even when the engine swallowed the exception.
    """
    real_connect = socket.socket.connect
    violations = []

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if self.family == socket.AF_UNIX or (
            isinstance(host, str) and (host.startswith("127.") or host in {"::1", "localhost"})
        ):
            return real_connect(self, address, *args, **kwargs)
        violations.append(address)
        raise ConnectionRefusedError(f"test tried to open a real network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
    assert not violations, f"test opened real network connections: {violations}"
