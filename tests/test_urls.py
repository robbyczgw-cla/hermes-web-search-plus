"""One URL normalisation for dedup, diversity, fusion and v3 observations."""

import pytest

from wsp_core import urls


@pytest.mark.parametrize("variant", [
    "https://docs.example.com/guide",
    "https://docs.example.com/guide/",
    "http://docs.example.com/guide",
    "https://www.docs.example.com/guide",
    "https://m.docs.example.com/guide",
    "https://DOCS.Example.COM/guide#section",
    "https://docs.example.com:443/guide",
    "https://docs.example.com/guide?utm_source=x&utm_medium=y",
    "https://docs.example.com/guide?srsltid=AfmBOoq&gclid=1",
    "https://docs.example.com/guide/amp",
    "https://docs.example.com/guide/amp/",
    "https://amp.docs.example.com/guide",
    "https://docs.example.com/guide?amp=1",
    "https://docs.example.com/guide?outputType=amp",
])
def test_presentation_variants_share_one_key(variant):
    assert urls.url_key(variant) == "docs.example.com/guide"


def test_canonical_url_keeps_a_scheme_and_sorted_identifying_query():
    assert urls.canonical_url("HTTPS://www.Example.com/a/?b=2&a=1&utm_x=3#f") == "https://example.com/a?a=1&b=2"
    assert urls.canonical_url("https://example.com/") == "https://example.com"


def test_identifying_query_parameters_stay():
    assert urls.url_key("https://youtube.com/watch?v=a") != urls.url_key("https://youtube.com/watch?v=b")
    assert urls.url_key("https://news.ycombinator.com/item?id=1") == "news.ycombinator.com/item?id=1"


def test_short_hosts_and_paths_are_not_over_stripped():
    assert urls.url_key("https://m.com/x") == "m.com/x"
    assert urls.url_key("https://www.com/") == "www.com"
    assert urls.url_key("https://example.com/ampere") == "example.com/ampere"
    assert urls.url_key("https://example.com/amp-guide") == "example.com/amp-guide"


def test_idn_and_ports():
    assert urls.url_key("https://bücher.example/x") == "xn--bcher-kva.example/x"
    assert urls.url_key("https://example.com:8443/x") == "example.com:8443/x"


@pytest.mark.parametrize("bad", ["", "   ", "not a url", None, "https://"])
def test_invalid_input_gives_an_empty_key(bad):
    assert urls.url_key(bad) == ""
    assert urls.canonical_url(bad) == ""


def test_strip_tracking_params_keeps_everything_else():
    url = "https://example.com/Path/?q=1&utm_source=x&srsltid=y#frag"
    assert urls.strip_tracking_params(url) == "https://example.com/Path/?q=1#frag"


def test_host_and_path_for_rule_matching():
    assert urls.host_and_path("https://www.github.com/anthropics/claude-code/") == "github.com/anthropics/claude-code"


def test_stacked_host_aliases_collapse_to_one_identity():
    assert urls.url_key("https://www.m.example.com/a") == urls.url_key("https://m.example.com/a") == "example.com/a"


@pytest.mark.parametrize("entry, host", [
    ("docs.rs", "docs.rs"),
    ("WWW.Docs.RS", "docs.rs"),
    ("www.com", "www.com"),
    ("a.b.example.co.uk", "a.b.example.co.uk"),
    ("user:pw@docs.rs:8080/x", "docs.rs"),
    ("docs.rs/tokio\n", "docs.rs"),
    ("bücher.example", "xn--bcher-kva.example"),
    ("straße.de", "strasse.de"),
    ("xn--mnchen-3ya.de", "xn--mnchen-3ya.de"),
])
def test_a_domain_filter_entry_becomes_a_bare_ascii_host(entry, host):
    assert urls.domain_filter_host(entry) == host


@pytest.mark.parametrize("entry", [
    "", " ", "com", "localhost", "1.2.3.4", "[::1]", "docs rs", "a..b.com", "-a.com", "a_b.com", "b.com;c.com",
    "site:docs.rs", "--help", "a" * 64 + ".com", ("a" * 60 + ".") * 5 + "com", "https://", "docs.r", "docs.123",
    "ａ＠b.com",
])
def test_anything_that_is_not_a_plain_domain_is_rejected(entry):
    assert urls.domain_filter_host(entry) is None


def test_domain_filters_split_strings_and_lists_alike():
    assert urls.domain_filter_tokens("a.com, b.com;c.com\nd.com") == ["a.com", "b.com;c.com", "d.com"]
    assert urls.domain_filter_tokens(["a.com b.com", None, "", 5]) == ["a.com", "b.com", "5"]
    assert urls.domain_filter_tokens(None) == [] and urls.domain_filter_tokens("  ") == []


def test_domain_filters_keep_order_drop_duplicates_and_let_exclude_win():
    assert urls.domain_filters("b.com a.com B.com", ["a.com", "x y", "c.com"]) == (["b.com"], ["a.com", "c.com"])
    assert urls.domain_filters(None, "a.com") == ([], ["a.com"])
