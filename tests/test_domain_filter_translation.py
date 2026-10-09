"""include_domains / exclude_domains reach every provider in its own dialect."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wsp_core import provider_dispatch as pd
from wsp_core import providers, search


def _args(**kw):
    base = dict(
        query="tokio select macro", max_results=5, include_domains=None, exclude_domains=None,
        time_range=None, freshness=None, country=None, language=None, search_type="search",
        you_safesearch=None, no_news=False, livecrawl=None, images=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("provider", ["serper", "serpbase", "brave", "you"])
def test_providers_without_native_domain_field_get_site_operators(provider, monkeypatch):
    fn = MagicMock(return_value={"results": []})
    module = SimpleNamespace(**{f"search_{provider}": fn})
    monkeypatch.setattr(pd, "_locale", lambda prov, args, config: ("at", "de"))
    args = _args(include_domains=["docs.rs", "tokio.rs"], exclude_domains=["reddit.com"])

    pd.SEARCH_DISPATCH[provider](module, provider, args, "k", {}, {})

    assert fn.call_args.kwargs["query"] == (
        "tokio select macro site:docs.rs OR site:tokio.rs -site:reddit.com"
    )


def test_no_domains_leaves_query_untouched():
    assert pd._with_site_operators("tokio select", _args()) == "tokio select"


def test_existing_site_operator_is_not_doubled():
    args = _args(query="site:docs.rs tokio", include_domains=["docs.rs"])
    assert pd._with_site_operators(args.query, args) == "site:docs.rs tokio"


@pytest.mark.parametrize("provider", ["tavily", "exa", "linkup", "parallel", "firecrawl", "querit"])
def test_native_domain_providers_keep_the_field(provider):
    import inspect

    from wsp_core import providers

    params = inspect.signature(getattr(providers, f"search_{provider}")).parameters
    assert "include_domains" in params and "exclude_domains" in params


@pytest.mark.parametrize("bad", ["--help", "x OR y", "site:reddit.com", "-q x", "https://", "1.2.3.4", "localhost", "a_b.com"])
def test_an_include_list_without_a_usable_domain_fails_closed(bad):
    # Searching unrestricted would answer a different question than the one asked.
    with pytest.raises(ValueError, match="Invalid include_domains value"):
        pd._with_site_operators("q", _args(query="q", include_domains=[bad]))


def test_a_suffix_filter_restricts_the_search():
    # "Government sites only" is a filter, not an unusable entry.
    args = _args(query="q", include_domains=[".gov", "*.ac.uk"], exclude_domains=[".example"])
    assert pd._with_site_operators("q", args) == "q site:gov OR site:ac.uk -site:example"


@pytest.mark.parametrize("blank", ["", "  ", None, [], [""], [None]])
def test_a_blank_include_list_is_no_filter(blank):
    assert pd._with_site_operators("q", _args(query="q", include_domains=blank)) == "q"


def test_unusable_exclude_entries_are_skipped_and_usable_ones_kept():
    args = _args(query="q", include_domains=["docs.rs", "x OR y"], exclude_domains=["--help", "reddit.com"])
    assert pd._with_site_operators("q", args) == "q site:docs.rs -site:reddit.com"


@pytest.mark.parametrize("value, expected", [
    ("docs.rs", "q site:docs.rs"),
    ("docs.rs, tokio.rs", "q site:docs.rs OR site:tokio.rs"),
    ("docs.rs tokio.rs", "q site:docs.rs OR site:tokio.rs"),
    (["docs.rs, tokio.rs"], "q site:docs.rs OR site:tokio.rs"),
    (["docs.rs", "tokio.rs,"], "q site:docs.rs OR site:tokio.rs"),
    (("docs.rs", "tokio.rs"), "q site:docs.rs OR site:tokio.rs"),
])
def test_a_string_or_a_separated_list_is_a_list_of_domains(value, expected):
    assert pd._with_site_operators("q", _args(query="q", include_domains=value)) == expected


@pytest.mark.parametrize("entry, host", [
    ("https://docs.rs:443/x", "docs.rs"),
    ("docs.rs:443", "docs.rs"),
    ("http://user:pw@Docs.RS/x?y=1#z", "docs.rs"),
    ("docs.rs.", "docs.rs"),
    ("https://www.docs.rs./a", "docs.rs"),
    ("münchen.de", "xn--mnchen-3ya.de"),
    ("https://München.DE/x", "xn--mnchen-3ya.de"),
    ("пример.рф", "xn--e1afmkfd.xn--p1ai"),
])
def test_entries_are_reduced_to_an_ascii_host(entry, host):
    assert pd._with_site_operators("q", _args(query="q", include_domains=[entry])) == f"q site:{host}"
    assert pd._with_site_operators("q", _args(query="q", exclude_domains=[entry])) == f"q -site:{host}"


def test_a_minus_site_operator_in_the_query_does_not_hide_the_include_filter():
    query = "what does -site:foo.com mean"
    assert pd._with_site_operators(query, _args(query=query, include_domains=["docs.rs"])) == (
        "what does -site:foo.com mean site:docs.rs"
    )


@pytest.mark.parametrize("query", ["site:docs.rs tokio", "tokio SITE:docs.rs", "(site:docs.rs OR site:tokio.rs) select"])
def test_a_real_site_operator_in_the_query_keeps_its_own_include_filter(query):
    assert pd._with_site_operators(query, _args(query=query, include_domains=["example.com"])) == query


def test_a_host_in_both_lists_is_excluded():
    args = _args(include_domains=["a.com", "b.com"], exclude_domains=["a.com", "c.com"])
    assert pd._with_site_operators("q", args) == "q site:b.com -site:a.com -site:c.com"


def test_when_every_include_is_also_excluded_nothing_is_left_to_search():
    args = _args(include_domains=["a.com"], exclude_domains=["https://www.a.com/x"])
    with pytest.raises(ValueError, match="exclude_domains"):
        pd._with_site_operators("q", args)


def test_each_list_is_capped_at_ten_operators():
    include = [f"in{i}.com" for i in range(15)]
    exclude = [f"out{i}.com" for i in range(15)]

    result = pd._with_site_operators("q", _args(include_domains=include, exclude_domains=exclude))

    assert result.count("-site:") == 10 and result.count("site:") == 20
    assert "in9.com" in result and "in10.com" not in result
    assert "out9.com" in result and "out10.com" not in result


def test_an_exclusion_already_in_the_query_is_not_repeated():
    args = _args(query="q -site:reddit.com", exclude_domains=["reddit.com", "x.com"])
    assert pd._with_site_operators(args.query, args) == "q -site:reddit.com -site:x.com"


def test_urls_and_www_are_reduced_to_the_host():
    args = _args(include_domains=["https://www.Docs.rs/tokio/latest", "docs.rs"])
    assert pd._with_site_operators("q", args) == "q site:docs.rs"


def _firecrawl_query(monkeypatch, **domains):
    seen = {}

    def fake_request(url, headers, body, timeout=30):
        seen["body"] = body
        return {"success": True, "data": {"web": []}}

    monkeypatch.setattr(providers, "make_request", fake_request)
    providers.search_firecrawl(query="q", api_key="k", **domains)
    return seen["body"]["query"]


def test_firecrawl_joins_several_include_domains_with_or(monkeypatch):
    # "site:docs.rs site:tokio.rs" asks for pages on both hosts at once: no results.
    query = _firecrawl_query(monkeypatch, include_domains=["docs.rs", "tokio.rs"])
    assert query == "q site:docs.rs OR site:tokio.rs"


def test_firecrawl_domains_cannot_inject_operators(monkeypatch):
    query = _firecrawl_query(
        monkeypatch,
        include_domains=["a.com", "x.com OR site:evil.com"],
        exclude_domains=["b.com -site:ok.com", "--help"],
    )
    assert query == "q site:a.com OR site:x.com -site:b.com"


def test_firecrawl_normalises_and_lets_exclude_win(monkeypatch):
    query = _firecrawl_query(
        monkeypatch,
        include_domains="https://www.a.com:8443/x, münchen.de, c.com",
        exclude_domains=["c.com."],
    )
    assert query == "q site:a.com OR site:xn--mnchen-3ya.de -site:c.com"


def test_firecrawl_without_a_usable_include_does_not_search_unrestricted(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the request must not be sent")

    monkeypatch.setattr(providers, "make_request", forbidden)
    with pytest.raises(ValueError, match="Invalid include_domains value"):
        providers.search_firecrawl(query="q", api_key="k", include_domains=["OR", "site:evil.com"])


def _isolated_search(monkeypatch, provider, env_key, fake):
    monkeypatch.setattr(search, "provider_in_cooldown", lambda p: (False, 0))
    monkeypatch.setattr(search, "cache_get", lambda **kw: None)
    monkeypatch.setattr(search, "cache_put", lambda **kw: None)
    monkeypatch.setattr(search, "reset_provider_health", lambda p: None)
    monkeypatch.setenv(env_key, "test-key-0123456789")
    monkeypatch.setattr(providers, f"search_{provider}", fake)


def _canned(provider):
    return {
        "provider": provider, "query": "q", "images": [], "answer": "", "metadata": {},
        "results": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
    }


def test_a_bare_domain_string_restricts_the_search(monkeypatch):
    seen = {}
    _isolated_search(monkeypatch, "serper", "SERPER_API_KEY", lambda **kw: seen.update(kw) or _canned("serper"))

    result = search.run_search_request(query="tokio select", provider="serper", include_domains="docs.rs, tokio.rs")

    assert "error" not in result
    assert seen["query"] == "tokio select site:docs.rs OR site:tokio.rs"


def test_native_domain_providers_get_a_clean_list_for_a_string(monkeypatch):
    seen = {}
    _isolated_search(monkeypatch, "tavily", "TAVILY_API_KEY", lambda **kw: seen.update(kw) or _canned("tavily"))

    search.run_search_request(query="tokio select", provider="tavily", include_domains="docs.rs, tokio.rs")

    assert seen["include_domains"] == ["docs.rs", "tokio.rs"]


@pytest.mark.parametrize("provider, env_key", [("serper", "SERPER_API_KEY"), ("brave", "BRAVE_API_KEY")])
def test_an_unusable_include_list_is_an_error_and_no_provider_is_called(monkeypatch, provider, env_key):
    def forbidden(**kwargs):
        raise AssertionError("the provider must not be called")

    _isolated_search(monkeypatch, provider, env_key, forbidden)

    result = search.run_search_request(
        query="tokio select", provider=provider, include_domains=["site:docs.rs", "x OR y"]
    )

    assert result["error"].startswith("Invalid include_domains value: ")
    assert result["provider"] == provider
    assert result["results"] == []


def test_the_command_line_rejects_an_unusable_include_list(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["search.py", "--query", "tokio", "--include-domains", "x OR y"])

    with pytest.raises(SystemExit) as exit_info:
        search.main()

    assert exit_info.value.code == 2
    assert "Invalid include_domains value: 'x', 'OR', 'y'" in capsys.readouterr().err
