"""Query length limits: generic cap in the engine entry, per-provider limits in the provider layer."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wsp_core import provider_dispatch as pd
from wsp_core import search
from wsp_core.errors_v3 import classify_provider_error
from wsp_core.contract_v3 import ErrorClass
from wsp_core.http_client import ProviderRequestError
from wsp_core.quality import build_quality_report
from wsp_core.query_limits import MAX_QUERY_CHARS, PROVIDER_QUERY_LIMITS, fit_query


def _args(**kw):
    base = dict(
        query="q", max_results=5, include_domains=None, exclude_domains=None,
        time_range=None, freshness=None, country=None, language=None, search_type="search",
        you_safesearch=None, no_news=False, livecrawl=None, images=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _brave_query(args):
    fn = MagicMock(return_value={"provider": "brave", "query": "", "results": [], "metadata": {}})
    result = pd.SEARCH_DISPATCH["brave"](SimpleNamespace(search_brave=fn), "brave", args, "k", {}, {})
    return fn.call_args.kwargs["query"], result


def _long_query(chars=581):
    words = []
    while len(" ".join(words)) < chars:
        words.append(f"word{len(words)}")
    return " ".join(words)[:chars]


def test_brave_581_char_query_is_shortened_within_both_limits():
    query, result = _brave_query(_args(query=_long_query(581)))
    assert len(query) <= 400 and len(query.split()) <= 50
    info = result["metadata"]["query_truncated"]
    assert info["provider"] == "brave" and info["original_chars"] == 581
    assert info["sent_chars"] == len(query)


def test_brave_word_limit_applies_to_short_words():
    query, result = _brave_query(_args(query=" ".join(["ab"] * 120)))
    assert len(query.split()) <= 50 and len(query) <= 400
    assert result["metadata"]["query_truncated"]["original_words"] == 120


def test_free_text_is_cut_at_a_word_boundary():
    text = _long_query(581)
    query, _ = _brave_query(_args(query=text))
    assert text.startswith(query) and text[len(query)] == " "


def test_site_operators_survive_and_count_towards_the_limit():
    args = _args(
        query=_long_query(581),
        include_domains=["docs.rs", "tokio.rs"],
        exclude_domains=["reddit.com", "x.com"],
    )
    query, result = _brave_query(args)
    suffix = "site:docs.rs OR site:tokio.rs -site:reddit.com -site:x.com"
    assert query.endswith(" " + suffix)
    assert len(query) <= 400 and len(query.split()) <= 50
    assert "operators_dropped" not in result["metadata"]["query_truncated"]


def test_twenty_operators_still_fit_with_free_text():
    domains = [f"d{i}.example.org" for i in range(10)]
    args = _args(query=_long_query(581), include_domains=domains, exclude_domains=[f"x{i}.example.net" for i in range(10)])
    query, _ = _brave_query(args)
    assert len(query) <= 400 and len(query.split()) <= 50
    assert all(f"site:{d}" in query for d in domains)


def test_exclusions_are_dropped_only_when_nothing_else_fits():
    query, info = fit_query("brave", "hello world", ["site:a.com", *[f"-site:{'z' * 40}{i}.com" for i in range(10)]])
    assert len(query) <= 400 and "site:a.com" in query and "hello" in query
    assert info["operators_dropped"] >= 1


def test_include_filter_that_cannot_fit_fails_closed():
    with pytest.raises(ValueError, match="Too many domain filters for brave"):
        fit_query("brave", "hello", ["site:" + "a" * 500 + ".com"])


def test_single_overlong_token_is_hard_cut():
    query, info = fit_query("brave", "x" * 700)
    assert len(query) == 400 and info["original_chars"] == 700


def test_normal_queries_are_unchanged():
    args = _args(query="tokio select macro", include_domains=["docs.rs"])
    query, result = _brave_query(args)
    assert query == "tokio select macro site:docs.rs"
    assert "query_truncated" not in result["metadata"]


def test_provider_without_documented_limit_is_untouched():
    text = _long_query(581)
    assert fit_query("serper", text, ["site:a.com"]) == (text + " site:a.com", None)
    assert set(PROVIDER_QUERY_LIMITS) == {"brave"}


def test_other_site_operator_providers_keep_their_query():
    fn = MagicMock(return_value={"provider": "serper", "query": "", "results": []})
    text = _long_query(581)
    pd.SEARCH_DISPATCH["serper"](SimpleNamespace(search_serper=fn), "serper", _args(query=text), "k", {}, {})
    assert fn.call_args.kwargs["query"] == text


def test_quality_report_shows_the_truncation():
    info = {"provider": "brave", "original_chars": 581, "sent_chars": 390}
    result = {"provider": "brave", "results": [], "metadata": {"query_truncated": info}}
    report = build_quality_report("q", result, {}, [], [], [], [])
    assert report["query_truncated"] == info
    plain = build_quality_report("q", {"provider": "brave", "results": []}, {}, [], [], [], [])
    assert "query_truncated" not in plain


# -- engine entry cap (shared by the tool and the native backend) ----------------


class _Stop(Exception):
    pass


def _capture_engine_query(monkeypatch):
    seen = {}

    def fake_execute(request, *_a, **_k):
        seen["query"] = request.input["query"]
        raise _Stop

    monkeypatch.setattr(search, "execute_v3_request", fake_execute)
    return seen


def test_engine_entry_caps_generic_length(monkeypatch):
    seen = _capture_engine_query(monkeypatch)
    with pytest.raises(_Stop):
        search.run_search_request(query="x" * 50_000, config={})
    assert len(seen["query"]) == MAX_QUERY_CHARS


def test_engine_entry_leaves_normal_queries(monkeypatch):
    seen = _capture_engine_query(monkeypatch)
    with pytest.raises(_Stop):
        search.run_search_request(query="normal query", config={})
    assert seen["query"] == "normal query"


def test_native_backend_path_is_capped(monkeypatch):
    abc = ModuleType("agent.web_search_provider")
    abc.WebSearchProvider = object
    web = ModuleType("tools.web_tools")
    web._load_web_config = lambda: {"backend": "wsp"}
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", abc)
    monkeypatch.setitem(sys.modules, "tools.web_tools", web)
    spec = importlib.util.spec_from_file_location(
        "_wsp_native_cap", Path(__file__).resolve().parents[1] / "native_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = _capture_engine_query(monkeypatch)
    plugin = SimpleNamespace(
        _load_search_module=lambda: object(),
        _run_search=lambda **kw: search.run_search_request(config={}, **kw),
    )
    out = module.WSPNativeBackend(plugin).search("y" * 9000, limit=3)
    assert out["success"] is False  # the capture stops the engine after recording the query
    assert len(seen["query"]) == MAX_QUERY_CHARS


# -- error text ------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 413, 414, 422])
def test_validation_errors_are_understandable(status):  # a 400 counts for brave only
    err = classify_provider_error(ProviderRequestError("API error", status_code=status), provider="brave")
    assert err.error_class is ErrorClass.INVALID_REQUEST
    assert "rejected by brave" in err.message and "too long" in err.message and "400 characters" in err.message
    assert err.retryable is False and err.http_status == status


def test_validation_error_for_provider_without_known_limit():
    err = classify_provider_error(ProviderRequestError("API error", status_code=422), provider="tavily")
    assert err.error_class is ErrorClass.INVALID_REQUEST and "422" in err.message


def test_bare_400_of_other_providers_keeps_its_class():
    err = classify_provider_error(ProviderRequestError("API error", status_code=400), provider="tavily")
    assert err.error_class is ErrorClass.INTERNAL


def test_out_of_credit_400_stays_quota():
    err = classify_provider_error(
        ProviderRequestError("x", status_code=400, out_of_credit=True), provider="serper"
    )
    assert err.error_class is ErrorClass.QUOTA

