"""Extract-URL validation must agree with the WHATWG/Chromium parser.

The validator runs in Python, but the URL is handed unchanged to a remote or
browser-based fetcher. Every URL whose host could be read differently by the
two parsers is rejected before any provider or fallback runs.
"""
from __future__ import annotations

import socket
from unittest import mock

import pytest

import extract
import search


def _resolver(*addresses):
    def fake(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (addr, port)) for addr in addresses]

    return fake


@pytest.fixture
def no_dns(monkeypatch):
    """Fail the test if a syntactically ambiguous URL ever reaches DNS."""
    calls = []

    def forbidden(host, port, *args, **kwargs):
        calls.append(host)
        raise AssertionError(f"DNS resolution reached for {host!r}")

    monkeypatch.setattr(extract.socket, "getaddrinfo", forbidden)
    return calls


AMBIGUOUS_URLS = [
    # Python sees host example.com, Chromium sees 127.0.0.1 (backslash ends authority).
    "http://127.0.0.1\\@example.com/collect",
    "http://example.com\\@127.0.0.1/",
    "https://example.com\\.evil.test/",
    # Non-canonical IPv4 forms that browsers and resolvers normalise to 127.0.0.1.
    "http://0177.0.0.1/admin",
    "http://0177.0000.0000.0001/admin",
    "http://0x7f.0.0.1/admin",
    "http://0x7f000001/admin",
    "http://127.1/admin",
    "http://127.0.1/admin",
    "http://2130706433/admin",
    "http://017700000001/admin",
    # Percent-escaped hostnames.
    "http://%31%32%37.0.0.1/admin",
    "http://%6c%6f%63%61%6c%68%6f%73%74/admin",
    "https://exa%6dple.com/",
    "http://[fe80::1%25eth0]/",
    # Userinfo.
    "http://user@example.com/",
    "http://user:pass@example.com/",
    "http://example.com:80@127.0.0.1/",
    "https://PUBLIC...test@127.0.0.1/admin",
    # Unicode confusables and compatibility characters (valid IDN hosts are converted, see below).
    "http://exаmple.com/",  # Cyrillic a
    "http://ｅxample.com/",  # fullwidth e
    "http://ex\u200bample.com/",  # zero-width space
    "http://①②⑦.0.0.1/",  # enclosed digits that NFKC-fold to 127
    # Whitespace and control characters anywhere in the URL.
    "http://example.com /x",
    "http://example.com/\tx",
    "http://example.com/a\nb",
    "http://example.com/a\rb",
    "http://exam\x00ple.com/",
    "http://example.com/\x7f",
    "http://example.com/a\u2028b",
]


@pytest.mark.parametrize("url", AMBIGUOUS_URLS)
def test_ambiguous_urls_are_rejected_before_dns(url, no_dns):
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls([url], config={})
    assert no_dns == []


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:127.0.0.1]/admin",
        "http://[::ffff:7f00:1]/admin",
        "http://[::ffff:10.0.0.1]/admin",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[64:ff9b::7f00:1]/admin",
        "http://[::1]/admin",
        "http://[::]/admin",
        "http://[fe80::1]/admin",
        "http://[fd00::1]/admin",
    ],
)
def test_ipv6_literals_with_embedded_private_ipv4_are_rejected(url):
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls([url], config={})


@pytest.mark.parametrize(
    "answer",
    [
        "::ffff:10.0.0.5",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.0.9",
        "169.254.10.10",
        "100.64.0.1",
        "224.0.0.251",
        "::1",
        "fe80::1",
        "fd12::1",
        "ff02::1",
    ],
)
def test_dns_answers_in_non_global_ranges_are_rejected(monkeypatch, answer):
    monkeypatch.setattr(extract.socket, "getaddrinfo", _resolver(answer))
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls(["https://rebind.example.test/page"], config={})


def test_one_private_answer_among_public_answers_rejects(monkeypatch):
    monkeypatch.setattr(extract.socket, "getaddrinfo", _resolver("93.184.216.34", "10.0.0.7"))
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls(["https://mixed.example.test/"], config={})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path?q=1#frag",
        "https://sub.example.com:8443/a/b",
        "http://93.184.216.34/",
        "https://xn--bcher-kva.example/",
        "https://example.com./",
        "https://my_host.example.com/",
        "https://[2606:4700:4700::1111]/",
        "https://example.com/?next=http://127.0.0.1/",
        "https://example.com/%E2%9C%93?q=%20",
    ],
)
def test_normal_public_urls_still_pass(monkeypatch, url):
    monkeypatch.setattr(extract.socket, "getaddrinfo", _resolver("93.184.216.34"))
    assert extract._validate_extract_urls([url], config={}) == [url]


def test_rejected_url_never_reaches_a_provider(monkeypatch, no_dns):
    with mock.patch("search.extract_firecrawl") as provider:
        result = search.extract_plus(
            ["http://127.0.0.1\\@example.com/collect"],
            provider="firecrawl",
            config={"auto_routing": {"disabled_providers": []}},
        )
    provider.assert_not_called()
    assert result["results"] == []
    assert "blocked" in result["error"].lower()


def test_one_bad_url_in_a_batch_blocks_the_whole_batch(monkeypatch):
    monkeypatch.setattr(extract.socket, "getaddrinfo", _resolver("93.184.216.34"))
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls(
            ["https://example.com/ok", "http://0177.0.0.1/"], config={}
        )


def test_syntax_checks_still_apply_when_private_urls_are_allowed():
    config = {"extract": {"allow_private_urls": True}}
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls(["http://127.0.0.1\\@example.com/"], config=config)
    assert extract._validate_extract_urls(["http://10.0.0.5/ok"], config=config) == ["http://10.0.0.5/ok"]


# --- IDN hosts: converted to punycode, then validated; the fetcher gets the ASCII form ---

@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://m\u00fcller.de/a?q=1", "https://xn--mller-kva.de/a?q=1"),
        ("https://B\u00dcCHER.example:8443/x", "https://xn--bcher-kva.example:8443/x"),
        ("http://stra\u00dfe.example/", "http://xn--strae-oqa.example/"),
        ("https://\u4f8b\u3048.jp/", "https://xn--r8jz45g.jp/"),
        ("https://sub.m\u00fcller.de./", "https://sub.xn--mller-kva.de./"),
    ],
)
def test_idn_hosts_are_converted_and_the_converted_url_is_returned(monkeypatch, url, expected):
    seen = []

    def fake(host, port, *args, **kwargs):
        seen.append(host)
        return _resolver("93.184.216.34")(host, port)

    monkeypatch.setattr(extract.socket, "getaddrinfo", fake)
    assert extract._validate_extract_urls([url], config={}) == [expected]
    assert all(h.isascii() for h in seen) and seen


@pytest.mark.parametrize(
    "url",
    [
        "https://exam\uff45ple.com/",          # fullwidth letter
        "https://127\u30021\u30020\u30021/",    # ideographic full stops
        "https://exam\uff0eple.com/",          # fullwidth full stop
        "https://a\u2024b.com/",               # one dot leader
        "https://ex\u200bample.com/",          # zero width space
        "https://exam\u2215ple.com/",          # division slash
        "https://\u00fc" + "a" * 70 + ".de/",    # over-long label
        "https://m\u00fcller..de/",             # empty label
        "https://\u00fc@example.com/",         # userinfo still rejected
    ],
)
def test_idn_lookalikes_and_structure_characters_are_still_rejected(no_dns, url):
    with pytest.raises((extract.ExtractUrlSecurityError, ValueError)):
        extract._validate_extract_urls([url], config={})


def test_idn_host_that_maps_to_a_private_ip_literal_is_rejected(no_dns):
    with pytest.raises((extract.ExtractUrlSecurityError, ValueError)):
        extract._validate_extract_urls(["https://\u00bc.example/"], config={})


def test_idn_url_reaches_provider_only_as_punycode(monkeypatch):
    monkeypatch.setattr(extract.socket, "getaddrinfo", _resolver("93.184.216.34"))
    with mock.patch("search.extract_firecrawl") as provider:
        provider.return_value = {"provider": "firecrawl", "results": []}
        search.extract_plus(["https://m\u00fcller.de/"], provider="firecrawl", config={"extract": {}})
    urls = provider.call_args[0][0]
    assert urls == ["https://xn--mller-kva.de/"]
