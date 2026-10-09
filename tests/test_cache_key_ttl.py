"""Cache identity ignores query case and spacing; short-lived query classes get a 300 s TTL cap."""

from __future__ import annotations

import hashlib
import json
from unittest import mock

import pytest

from wsp_core import cache, extract, orchestrator_v3, search
from wsp_core.cache_identity_v3 import ExtractionCacheIdentityV3
from wsp_core.cache_v3 import derive_cache_key
from wsp_core.compat_v3 import legacy_request_to_v3
from wsp_core.contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from wsp_core.orchestrator_v3 import CapabilityAdapter, ProviderPlan, execute_v3_request
from wsp_core.intents import INTENTS

SPELLINGS = [
    "best nas 2026",
    "Best  NAS 2026",
    "best nas 2026 ",
    "\tBEST NAS　2026\n",
]
UNICODE_FORMS = ["Café test", "Café TEST", "CAFÉ  test"]
SHORT_LIVED = {"news", "security"}
ALL_CLASSES = set(INTENTS)


def _search_request(query: str, **payload) -> RequestV3:
    base = legacy_request_to_v3(
        Capability.SEARCH,
        {"query": "placeholder", "provider": "serper", "count": 3, **payload},
        request_id="req",
    )
    # Bypass the compat layer's own NFC/strip so the raw spelling reaches the key.
    return RequestV3.from_dict({**base.to_dict(), "input": {"query": query}})


def _plan(routing_class: str | None) -> ProviderPlan:
    metadata = (
        {"analysis_summary": {"routing_class": routing_class}} if routing_class else {}
    )
    return ProviderPlan(("serper",), "serper", routing_metadata=metadata)


# --- normaliser -------------------------------------------------------------


def test_normalize_query_for_cache():
    normalize = cache.normalize_query_for_cache

    assert normalize("  Best \t NAS\n2026 ") == "best nas 2026"
    assert normalize("Café") == normalize("Café") == "café"
    assert normalize("Straße") == "strasse"
    assert normalize(normalize("  ǅ  X ")) == normalize("  ǅ  X ")


# --- legacy search cache key --------------------------------------------------


@pytest.mark.parametrize("variants", [SPELLINGS, UNICODE_FORMS])
def test_legacy_key_ignores_case_whitespace_and_unicode_form(variants):
    params = {"locale": "AT:de", "freshness": None}
    keys = {cache._get_cache_key(v, "serper", 3, params) for v in variants}

    assert len(keys) == 1


def test_legacy_key_still_separates_different_searches():
    params = {"locale": "AT:de"}
    base = cache._get_cache_key("best nas 2026", "serper", 3, params)

    assert cache._get_cache_key("best nas 2025", "serper", 3, params) != base
    assert cache._get_cache_key("nas best 2026", "serper", 3, params) != base
    assert cache._get_cache_key("best nas 2026", "brave", 3, params) != base
    assert cache._get_cache_key("best nas 2026", "serper", 5, params) != base
    assert cache._get_cache_key("best nas 2026", "serper", 3, {"locale": "DE:de"}) != base


def test_legacy_cache_hit_across_spellings_keeps_the_raw_query_in_the_entry():
    cache.cache_put("Best  NAS 2026", "serper", 3, {"results": ["ok"]})

    hit = cache.cache_get("best nas 2026 ", "serper", 3)

    assert hit is not None
    assert hit["results"] == ["ok"]
    assert hit["_cache_query"] == "Best  NAS 2026"


def test_legacy_search_hit_echoes_the_callers_query():
    config = {"auto_routing": {"enabled": True, "provider_priority": ["serper"]}}
    writer = search._search_args_from_v3(_search_request("Best  NAS 2026"), config)
    writer.provider = "serper"
    params = search._legacy_search_cache_context(writer, "serper", config)
    cache.cache_put(
        "Best  NAS 2026",
        "serper",
        3,
        {"provider": "serper", "query": "Best  NAS 2026", "results": []},
        params=params,
    )
    reader = search._search_args_from_v3(_search_request("best nas 2026"), config)
    reader.provider = "serper"

    with mock.patch.object(
        search, "execute_provider_with_retry", side_effect=AssertionError("provider called")
    ):
        payload, exit_code = search._execute_search_request_core(reader, config)

    assert exit_code == 0
    assert payload["cached"] is True
    assert payload["query"] == "best nas 2026"


# --- v3 cache key ---------------------------------------------------------------


@pytest.mark.parametrize("variants", [SPELLINGS, UNICODE_FORMS])
def test_v3_search_key_ignores_case_whitespace_and_unicode_form(variants):
    keys = {derive_cache_key(_search_request(v)) for v in variants}

    assert len(keys) == 1
    assert keys.pop().startswith("search_")


def test_v3_search_key_still_separates_different_searches():
    base = derive_cache_key(_search_request("best nas 2026"))

    assert derive_cache_key(_search_request("best nas 2025")) != base
    assert derive_cache_key(_search_request("best nas 2026", count=5)) != base
    assert derive_cache_key(_search_request("best nas 2026", provider="brave")) != base
    assert derive_cache_key(_search_request("best nas 2026", freshness="week")) != base


def test_v3_key_normalisation_leaves_the_request_untouched():
    request = _search_request("Best  NAS 2026")

    derive_cache_key(request)

    assert request.input == {"query": "Best  NAS 2026"}


def test_orchestrator_serves_a_respelled_query_from_the_v3_cache(tmp_path):
    calls = []
    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: _plan(None),
        execute=lambda *_: calls.append("provider") or {"provider": "serper", "results": []},
        normalize=_response,
    )
    config = {"v3": {"cache_dir": str(tmp_path)}}

    execute_v3_request(_search_request("Cache  Me NOW"), adapter, config)
    respelled = execute_v3_request(_search_request("cache me now "), adapter, config)
    other = execute_v3_request(_search_request("cache me later"), adapter, config)

    assert calls == ["provider", "provider"]
    assert respelled.response.cache_status["disposition"] == "fresh_hit"
    assert other.response.cache_status["disposition"] == "miss"


# --- extraction keys stay exact ---------------------------------------------------


def test_extraction_key_without_typed_identity_hashes_the_request_verbatim():
    request = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://Example.test/Path?Q=1"], "spans_query": "Mixed Case"},
    )
    material = {
        "contract_version": request.contract_version,
        "capability": "extract",
        "input": request.input,
        "options": request.options,
        "routing": request.routing,
        "budget": request.budget,
    }
    digest = hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()[:32]

    assert derive_cache_key(request) == f"extract_{digest}"
    lowered = legacy_request_to_v3(
        Capability.EXTRACT,
        {"urls": ["https://example.test/path?q=1"], "spans_query": "mixed case"},
    )
    assert derive_cache_key(lowered) != derive_cache_key(request)


def test_typed_extraction_key_keeps_url_case(tmp_path):
    config = {
        "serper": {"api_key": "serper-test-key-123456", "scrape_url": "https://one.test"},
        "auto_routing": {"disabled_providers": []},
        "extract": {"allow_private_urls": True},
        "v3": {"cache_dir": str(tmp_path), "operator_receipt_journal": False},
    }
    keys = []
    for url in ("https://example.test/Report", "https://example.test/report"):
        request = legacy_request_to_v3(
            Capability.EXTRACT, {"urls": [url], "provider": "serper"}
        )
        vary = extract._extract_cache_vary(request, _plan(None), config)
        identity = ExtractionCacheIdentityV3.from_canonical_form(
            vary["extraction_cache_identity"]
        )
        key = derive_cache_key(request, vary=vary)
        assert key == f"extract_{identity.key}"
        keys.append(key)

    assert keys[0] != keys[1]


# --- TTL by query class -----------------------------------------------------------


def test_class_cache_ttl_table_names_real_intents_with_a_five_minute_cap():
    assert set(cache.CLASS_CACHE_TTL) == SHORT_LIVED
    assert SHORT_LIVED <= ALL_CLASSES
    assert set(cache.CLASS_CACHE_TTL.values()) == {300}


@pytest.mark.parametrize("routing_class", sorted(ALL_CLASSES))
def test_ttl_by_routing_class(routing_class):
    expected = 300 if routing_class in SHORT_LIVED else cache.DEFAULT_CACHE_TTL

    assert (
        cache.effective_search_cache_ttl("bookshelf speakers", routing_class=routing_class)
        == expected
    )


def test_class_cap_only_lowers_the_ttl():
    ttl = cache.effective_search_cache_ttl

    # A shorter request, recency or freshness cap still wins over the class cap.
    assert ttl("bookshelf speakers", requested_ttl=120, routing_class="news") == 120
    assert ttl("breaking news tonight", routing_class="news") == 60
    assert ttl("bookshelf speakers", freshness="hour", routing_class="security") == 60
    # A longer request is never raised, class or not.
    assert ttl("bookshelf speakers", requested_ttl=86400, routing_class="general") == 3600
    assert ttl("bookshelf speakers", requested_ttl=86400, routing_class="news") == 300


def test_ttl_without_a_plan_uses_the_class_of_the_query():
    ttl = cache.effective_search_cache_ttl

    assert ttl("CVE-2026-1234 mitigation") == 300
    assert ttl("NVIDIA earnings guidance") == 300
    # Evergreen comparisons and queries in other languages are not capped.
    assert ttl("what are the differences between NAS and DAS") == cache.DEFAULT_CACHE_TTL
    assert ttl("Москва метро схема") == cache.DEFAULT_CACHE_TTL
    assert ttl("bookshelf speakers") == cache.DEFAULT_CACHE_TTL


def test_v3_ttl_uses_the_class_of_the_plan():
    request = _search_request("bookshelf speakers")

    assert orchestrator_v3._request_cache_ttl(request, _plan("security")) == 300
    assert orchestrator_v3._request_cache_ttl(request, _plan("general")) == 3600
    assert orchestrator_v3._request_cache_ttl(request, _plan(None)) == 3600
    assert orchestrator_v3._request_cache_ttl(request) == 3600


def test_planner_class_reaches_the_v3_ttl():
    config = {
        "serper": {"api_key": "serper-test-key-123456"},
        "auto_routing": {
            "enabled": True,
            "provider_priority": ["serper"],
            "disabled_providers": [],
        },
    }
    for query, expected_class, expected_ttl in (
        ("NVIDIA earnings guidance", "news", 300),
        ("bookshelf speakers", "general", 3600),
    ):
        request = _search_request(query, provider="auto")
        plan = search._plan_search_v3(request, config)

        assert plan.routing_metadata["analysis_summary"]["routing_class"] == expected_class
        assert orchestrator_v3._request_cache_ttl(request, plan) == expected_ttl


def test_search_core_ttl_uses_the_planned_class_of_a_v3_attempt():
    args = search._search_args_from_v3(_search_request("bookshelf speakers"), {})
    args._v3_planned_routing = {"analysis_summary": {"routing_class": "security"}}

    with mock.patch.object(search, "cache_get", side_effect=RuntimeError("probe")) as get:
        with pytest.raises(RuntimeError, match="probe"):
            search._execute_search_request_core(args, {})

    assert get.call_args.kwargs["ttl"] == 300


def _response(request: RequestV3, plan: ProviderPlan, _payload: dict) -> ResponseV3:
    return ResponseV3(
        request_id=request.request_id or plan.execution_id,
        capability=request.capability,
        status=ResponseStatus.OK,
        results=[],
        provider_attempts=[],
        routing_receipt={
            "policy_id": "classic",
            "policy_revision": "v2.9.1",
            "mode": "classic",
            "candidate_order": list(plan.candidate_order),
            "selected_provider": "serper",
            "fallback_reason": "none",
        },
        cache_status={"disposition": "miss"},
    )


def _age_stored_entries(root, seconds: int) -> None:
    for path in (root / "v3" / "response" / "search").glob("*.json"):
        envelope = json.loads(path.read_text())
        envelope["created_at"] -= seconds
        path.write_text(json.dumps(envelope))


@pytest.mark.parametrize(
    "routing_class,refetched_after_ten_minutes",
    [("news", True), ("general", False)],
)
def test_orchestrator_expires_short_lived_classes_after_five_minutes(
    tmp_path, routing_class, refetched_after_ten_minutes
):
    calls = []
    adapter = CapabilityAdapter(
        capability=Capability.SEARCH,
        plan=lambda *_: _plan(routing_class),
        execute=lambda *_: calls.append("provider") or {"provider": "serper", "results": []},
        normalize=_response,
    )
    config = {"v3": {"cache_dir": str(tmp_path)}}
    request = _search_request("bookshelf speakers")

    execute_v3_request(request, adapter, config)
    _age_stored_entries(tmp_path, 100)
    young = execute_v3_request(request, adapter, config)
    _age_stored_entries(tmp_path, 500)
    old = execute_v3_request(request, adapter, config)

    assert young.response.cache_status["disposition"] == "fresh_hit"
    assert young.response.cache_status["ttl_seconds"] == (
        300 if refetched_after_ten_minutes else 3600
    )
    assert (len(calls) == 2) is refetched_after_ten_minutes
    assert old.response.cache_status["disposition"] == (
        "miss" if refetched_after_ten_minutes else "fresh_hit"
    )
