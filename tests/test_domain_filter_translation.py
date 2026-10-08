"""include_domains / exclude_domains reach every provider in its own dialect."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wsp_core import provider_dispatch as pd


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


@pytest.mark.parametrize("bad", ["--help", "x OR y", "site:reddit.com", "a b.com", "", None, "-q x"])
def test_only_bare_hostnames_become_site_operators(bad):
    assert pd._with_site_operators("q", _args(query="q", include_domains=[bad])) == "q"


def test_urls_and_www_are_reduced_to_the_host():
    args = _args(include_domains=["https://www.Docs.rs/tokio/latest", "docs.rs"])
    assert pd._with_site_operators("q", args) == "q site:docs.rs"
