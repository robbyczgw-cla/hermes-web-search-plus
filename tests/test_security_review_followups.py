"""Regression tests for reviewer follow-ups: IPv6 forms, urllib handlers, empty encoded body."""
import pytest

from wsp_core import extract
from wsp_core import http_client


@pytest.mark.parametrize("addr", ["::127.0.0.1", "::a9fe:a9fe", "::ffff:0:7f00:1", "fec0::1", "::1", "::"])
def test_ipv6_internal_forms_are_blocked(addr):
    assert extract._is_private_or_internal_ip(addr)


@pytest.mark.parametrize("addr", ["2606:4700:4700::1111", "8.8.8.8"])
def test_public_addresses_still_pass(addr):
    assert not extract._is_private_or_internal_ip(addr)


def test_urllib_opener_registers_only_http_schemes():
    opener = http_client._safe_opener()
    assert "file" not in opener.handle_open
    assert "ftp" not in opener.handle_open
    assert "data" not in opener.handle_open
    assert {"http", "https"} <= set(opener.handle_open)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", ""])
def test_empty_body_with_encoding_is_not_corrupt(encoding):
    class Resp:
        headers = {"Content-Encoding": encoding}

        def read(self, n=None):
            return b""

        def getheader(self, k, d=None):
            return self.headers.get(k, d)

    assert http_client._read_response_body(Resp()) == b""
