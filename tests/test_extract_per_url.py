"""Per-URL extraction outcomes: one bad URL must not sink the batch (A3)."""

import os
import socket
import threading
from unittest import mock
from urllib.parse import unquote

import pytest

from wsp_core import extract, providers
from wsp_core.http_client import ProviderBatchStopped, ProviderRequestError


GOOD = "https://example.com/good"
OTHER = "https://example.org/other"
INTERNAL = "https://internal.example.test/page"


@pytest.fixture
def dns(monkeypatch):
    """Resolve example.* publicly and internal.example.test to a private IP."""
    calls = []
    resolve = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        calls.append(host)
        if host == "internal.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))]
        if host in {"example.com", "example.org"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


def _item(provider, url, content="page text", **extra):
    return {"url": url, "title": "T", "content": content, "raw_content": content, "provider": provider, **extra}


def _env(*names):
    keys = {
        "firecrawl": ("FIRECRAWL_API_KEY", "fc-test"),
        "linkup": ("LINKUP_API_KEY", "linkup-test"),
        "tavily": ("TAVILY_API_KEY", "tvly-test"),
        "exa": ("EXA_API_KEY", "exa-test"),
    }
    return mock.patch.dict(os.environ, dict(keys[name] for name in names), clear=True)


# a) validation per URL ------------------------------------------------------


def test_private_url_becomes_error_item_and_good_url_is_still_extracted(dns):
    def fake_firecrawl(urls, *args, **kwargs):
        return {"provider": "firecrawl", "results": [_item("firecrawl", url) for url in urls]}

    with _env("firecrawl"), mock.patch.object(providers, "extract_firecrawl", side_effect=fake_firecrawl) as fc:
        result = extract.extract_plus([GOOD, INTERNAL, OTHER], provider="firecrawl")

    assert fc.call_args.args[0] == [GOOD, OTHER]
    assert [item["url"] for item in result["results"]] == [GOOD, INTERNAL, OTHER]
    assert result["results"][0]["content"] == "page text"
    assert result["results"][1]["error"]
    assert "private/internal" in result["results"][1]["error"]
    assert result["results"][2]["content"] == "page text"
    # The resolved internal address is never shown to the model.
    assert "10.1.2.3" not in repr(result)
    # DNS runs once per URL per request, not again inside the provider attempt.
    assert dns.count("internal.example.test") == 1
    assert dns.count("example.com") == 1


def test_all_urls_rejected_keeps_a_clear_error_without_provider_calls(dns):
    with _env("firecrawl"), mock.patch.object(providers, "extract_firecrawl") as fc:
        result = extract.extract_plus([INTERNAL], provider="firecrawl")

    fc.assert_not_called()
    assert result["results"] == []
    assert "private/internal" in result["error"]
    assert "10.1.2.3" not in result["error"]


def test_resolved_private_ip_is_not_in_validation_error(dns):
    with pytest.raises(extract.ExtractUrlSecurityError) as excinfo:
        extract._validate_extract_urls([INTERNAL], config={})
    assert "10.1.2.3" not in str(excinfo.value)


# b) Firecrawl / Linkup per-URL HTTP errors -----------------------------------


def _fc_response(url):
    return {"success": True, "data": {"markdown": f"content of {url}", "metadata": {"sourceURL": url}}}


def test_firecrawl_keeps_fetched_urls_when_one_url_fails():
    def fake_request(_api, _headers, body, timeout=0):
        if body["url"] == OTHER:
            raise ProviderRequestError("Not found (HTTP 404)", status_code=404)
        return _fc_response(body["url"])

    with mock.patch.object(providers, "make_request", side_effect=fake_request):
        result = providers.extract_firecrawl([GOOD, OTHER], "fc-test")

    assert result["results"][0]["content"] == f"content of {GOOD}"
    assert result["results"][1]["url"] == OTHER
    assert result["results"][1]["error"] == "Firecrawl scrape failed (HTTP 404)"


@pytest.mark.parametrize(
    "error",
    [
        ProviderRequestError("Unauthorized (HTTP 401)", status_code=401),
        ProviderRequestError("Rate limited (HTTP 429)", status_code=429, transient=True),
        ProviderRequestError("Out of credits", status_code=402, out_of_credit=True),
    ],
)
def test_firecrawl_account_wide_error_on_first_url_still_raises(error):
    calls = []

    def fake_request(_api, _headers, body, timeout=0):
        calls.append(body["url"])
        if body["url"] == OTHER:
            raise error
        return _fc_response(body["url"])

    with mock.patch.object(providers, "make_request", side_effect=fake_request):
        with pytest.raises(ProviderRequestError) as excinfo:
            providers.extract_firecrawl([OTHER, GOOD], "fc-test")
    assert excinfo.value is error
    # Nothing was fetched, so nothing is kept, and no further request is sent.
    assert calls == [OTHER]


def test_firecrawl_raises_when_every_url_failed():
    error = ProviderRequestError("Server error (HTTP 500)", status_code=500)
    with mock.patch.object(providers, "make_request", side_effect=error):
        with pytest.raises(ProviderRequestError):
            providers.extract_firecrawl([GOOD, OTHER], "fc-test")


def test_linkup_batch_keeps_fetched_urls_when_one_url_fails():
    def fake_request(_api, _headers, body, timeout=0):
        if body["url"] == OTHER:
            raise ProviderRequestError("Server error (HTTP 500)", status_code=500)
        return {"markdown": f"content of {body['url']}"}

    with mock.patch.object(providers, "make_request", side_effect=fake_request):
        result = providers.extract_linkup([GOOD, OTHER], "linkup-test")

    assert result["results"][0]["content"] == f"content of {GOOD}"
    assert result["results"][1]["url"] == OTHER
    assert result["results"][1]["error"] == "Linkup fetch failed (HTTP 500)"


def test_linkup_batch_auth_error_stops_the_batch_and_keeps_fetched_pages():
    error = ProviderRequestError("Forbidden (HTTP 403)", status_code=403)

    def fake_request(_api, _headers, body, timeout=0):
        if body["url"] == OTHER:
            raise error
        return {"markdown": "ok"}

    with mock.patch.object(providers, "make_request", side_effect=fake_request):
        with pytest.raises(ProviderBatchStopped) as excinfo:
            providers.extract_linkup([GOOD, OTHER], "linkup-test")
    assert excinfo.value.cause is error
    assert excinfo.value.payload["results"][0]["content"] == "ok"
    assert excinfo.value.payload["results"][1]["error"] == (
        "Linkup fetch failed (HTTP 403): provider authentication failed"
    )


def test_linkup_batch_auth_error_on_every_url_still_raises():
    error = ProviderRequestError("Forbidden (HTTP 403)", status_code=403)
    with mock.patch.object(providers, "make_request", side_effect=error):
        with pytest.raises(ProviderRequestError) as excinfo:
            providers.extract_linkup([GOOD, OTHER], "linkup-test")
    assert excinfo.value is error


# b2) provider-wide error midway through a batch (5.1.2) ---------------------


BATCH = [f"https://example.com/page{n}" for n in range(1, 6)]

PROVIDER_WIDE_ERRORS = [
    pytest.param(
        lambda: ProviderRequestError("Rate limited (HTTP 429)", status_code=429, transient=True, retry_after=30),
        "rate_limit", " (HTTP 429): provider rate limit reached", "provider rate limit reached", id="rate-limit",
    ),
    pytest.param(
        lambda: ProviderRequestError("Out of credits", status_code=402, out_of_credit=True),
        "quota", " (HTTP 402): provider out of credits", "provider out of credits", id="quota",
    ),
    pytest.param(
        lambda: ProviderRequestError("Unauthorized (HTTP 401)", status_code=401),
        "auth", " (HTTP 401): provider authentication failed", "provider authentication failed", id="auth",
    ),
]


def _firecrawl_failing_on_third(error, calls):
    def fake_request(api, _headers, body, timeout=0):
        calls.append(body["url"])
        if body["url"] == BATCH[2]:
            raise error
        return _fc_response(body["url"])

    return fake_request


def _linkup_serving(calls):
    def fake_linkup(urls, *args, **kwargs):
        calls.append(list(urls))
        return {"provider": "linkup", "results": [_item("linkup", url, f"linkup {url}") for url in urls]}

    return fake_linkup


@pytest.mark.parametrize("make_error, _error_class, failed_suffix, cause", PROVIDER_WIDE_ERRORS)
def test_firecrawl_keeps_fetched_pages_when_provider_wide_error_hits_midway(
    make_error, _error_class, failed_suffix, cause
):
    error = make_error()
    calls = []
    with mock.patch.object(providers, "make_request", side_effect=_firecrawl_failing_on_third(error, calls)):
        with pytest.raises(ProviderBatchStopped) as excinfo:
            providers.extract_firecrawl(BATCH, "fc-test")

    # No request after the account-wide error.
    assert calls == BATCH[:3]
    assert excinfo.value.cause is error
    results = excinfo.value.payload["results"]
    assert [item["requested_url"] for item in results] == BATCH
    assert [item["content"] for item in results[:2]] == [f"content of {url}" for url in BATCH[:2]]
    assert not any(item.get("error") for item in results[:2])
    assert results[2]["error"] == "Firecrawl scrape failed" + failed_suffix
    assert [item["error"] for item in results[3:]] == [f"not tried: {cause}"] * 2


@pytest.mark.parametrize("make_error, error_class, _failed_suffix, cause", PROVIDER_WIDE_ERRORS)
def test_provider_wide_error_midway_keeps_pages_falls_back_for_rest_and_opens_circuit(
    make_error, error_class, _failed_suffix, cause
):
    from wsp_core.compat_v3 import legacy_request_to_v3
    from wsp_core.contract_v3 import AttemptOutcome, Capability, CircuitState

    error = make_error()
    fc_calls, lu_calls = [], []
    request = legacy_request_to_v3(Capability.EXTRACT, {"urls": BATCH, "provider": "firecrawl"})
    with _env("firecrawl", "linkup"), \
         mock.patch.object(providers, "make_request", side_effect=_firecrawl_failing_on_third(error, fc_calls)), \
         mock.patch.object(providers, "extract_linkup", side_effect=_linkup_serving(lu_calls)):
        response = extract.run_extract_request_v3(request)
        # The circuit now holds the failure: the next request skips Firecrawl.
        second = extract.extract_plus([GOOD], provider="auto")

    # One try only: a retry would pay again for pages already fetched.
    assert fc_calls == BATCH[:3]
    assert lu_calls[0] == BATCH[2:]
    assert response.status.value == "ok"
    assert [r["text"]["text"] for r in response.results] == (
        [f"content of {url}" for url in BATCH[:2]] + [f"linkup {url}" for url in BATCH[2:]]
    )
    attempts = {a.provider: a for a in response.provider_attempts}
    firecrawl = attempts["firecrawl"]
    assert firecrawl.outcome is AttemptOutcome.PARTIAL
    assert firecrawl.error.error_class.value == error_class
    assert firecrawl.circuit_state_after is not CircuitState.CLOSED
    assert firecrawl.retry_count == 0
    assert attempts["linkup"].outcome is AttemptOutcome.SUCCESS
    by_url = {o["url"]["observed"]: o["provider"] for o in response.observations}
    assert by_url == {**{url: "firecrawl" for url in BATCH[:2]}, **{url: "linkup" for url in BATCH[2:]}}

    assert fc_calls == BATCH[:3]  # the second request never reached Firecrawl
    assert lu_calls[1] == [GOOD]
    assert second["results"][0]["content"] == f"linkup {GOOD}"


@pytest.mark.parametrize("make_error, _error_class, failed_suffix, cause", PROVIDER_WIDE_ERRORS)
def test_provider_wide_error_midway_without_fallback_keeps_pages_and_is_not_cached(
    make_error, _error_class, failed_suffix, cause
):
    error = make_error()
    calls = []
    with _env("firecrawl"), \
         mock.patch.object(providers, "make_request", side_effect=_firecrawl_failing_on_third(error, calls)):
        result = extract.extract_plus(BATCH, provider="firecrawl")
        # Circuit open: the partial result must not come back from the cache.
        second = extract.extract_plus(BATCH, provider="firecrawl")

    assert [item["url"] for item in result["results"]] == BATCH
    assert [item["content"] for item in result["results"][:2]] == [f"content of {url}" for url in BATCH[:2]]
    assert result["results"][2]["error"] == "Firecrawl scrape failed" + failed_suffix
    assert [item["error"] for item in result["results"][3:]] == [f"not tried: {cause}"] * 2
    assert result["routing"]["provider"] == "firecrawl"
    assert not second.get("cached")
    assert second["error"] == "All extraction providers failed"
    assert calls == BATCH[:3]


def test_provider_wide_error_on_first_url_fails_the_attempt_and_falls_back():
    from wsp_core.compat_v3 import legacy_request_to_v3
    from wsp_core.contract_v3 import AttemptOutcome, Capability

    error = ProviderRequestError("Rate limited (HTTP 429)", status_code=429, transient=True, retry_after=0)

    def fake_request(_api, _headers, body, timeout=0):
        fc_calls.append(body["url"])
        raise error

    fc_calls, lu_calls = [], []
    request = legacy_request_to_v3(Capability.EXTRACT, {"urls": BATCH, "provider": "firecrawl"})
    with _env("firecrawl", "linkup"), \
         mock.patch.object(providers, "make_request", side_effect=fake_request), \
         mock.patch.object(providers, "extract_linkup", side_effect=_linkup_serving(lu_calls)):
        response = extract.run_extract_request_v3(request)

    attempts = {a.provider: a for a in response.provider_attempts}
    assert attempts["firecrawl"].outcome is AttemptOutcome.FAILED
    assert attempts["firecrawl"].error.error_class.value == "rate_limit"
    # Nothing was fetched, so the engine may retry as before (one URL per try).
    assert fc_calls == [BATCH[0]] * attempts["firecrawl"].retry_count + [BATCH[0]]
    assert lu_calls == [BATCH]
    assert [r["text"]["text"] for r in response.results] == [f"linkup {url}" for url in BATCH]


def test_linkup_batch_stops_sending_after_rate_limit_and_keeps_pages():
    error = ProviderRequestError("Rate limited (HTTP 429)", status_code=429)
    calls = []
    rate_limited = threading.Event()

    def fake_request(_api, _headers, body, timeout=0):
        calls.append(body["url"])
        if body["url"] == BATCH[1]:
            raise error
        rate_limited.wait(5)  # in flight while the rate limit arrives
        return {"markdown": f"content of {body['url']}"}

    real_task = providers.DaemonTask

    def ordered_task(fn, url):
        # The five fetches run in parallel; hold the queued ones until the
        # rate-limited fetch has returned, as a slower worker would.
        if url == BATCH[1]:
            def run(u):
                try:
                    return fn(u)
                finally:
                    rate_limited.set()
        elif url in BATCH[2:]:
            def run(u):
                rate_limited.wait(5)
                return fn(u)
        else:
            run = fn
        return real_task(run, url)

    with mock.patch.object(providers, "make_request", side_effect=fake_request), \
         mock.patch.object(providers, "DaemonTask", side_effect=ordered_task):
        with pytest.raises(ProviderBatchStopped) as excinfo:
            providers.extract_linkup(BATCH, "linkup-test")

    assert sorted(calls) == sorted(BATCH[:2])
    results = excinfo.value.payload["results"]
    assert results[0]["content"] == f"content of {BATCH[0]}"
    assert results[1]["error"] == "Linkup fetch failed (HTTP 429): provider rate limit reached"
    assert [item["error"] for item in results[2:]] == ["not tried: provider rate limit reached"] * 3


@pytest.mark.parametrize("provider, call", [
    ("serper", lambda urls: providers.extract_serper(urls, "serper-test")),
    ("keenable", lambda urls: providers.extract_keenable(urls, "keen-test")),
])
def test_serper_and_keenable_stop_at_provider_wide_error(provider, call):
    error = ProviderRequestError("Out of credits", status_code=400, out_of_credit=True)
    calls = []

    def fake(url_or_api, _headers, *args, **kwargs):
        # Serper POSTs {"url": ...}; Keenable GETs .../fetch?url=<quoted>.
        url = args[0]["url"] if args else unquote(url_or_api.split("?url=", 1)[1])
        calls.append(url)
        if url == BATCH[1]:
            raise error
        return {"markdown": f"content of {url}", "content": f"content of {url}"}

    with mock.patch.object(providers, "make_request", side_effect=fake), \
         mock.patch.object(providers, "make_get_request", side_effect=fake):
        with pytest.raises(ProviderBatchStopped) as excinfo:
            call(BATCH)
        assert calls == BATCH[:2]
        results = excinfo.value.payload["results"]
        assert results[0]["content"] == f"content of {BATCH[0]}"
        assert results[1]["error"].endswith("(HTTP 400): provider out of credits")
        assert [item["error"] for item in results[2:]] == ["not tried: provider out of credits"] * 3

        # On the first URL the error itself is raised, so the engine classifies it.
        calls.clear()
        with pytest.raises(ProviderRequestError) as first:
            call([BATCH[1], BATCH[0]])
        assert first.value is error
        assert calls == [BATCH[1]]


# c) per-URL fallback -------------------------------------------------------


def test_failed_url_is_retried_on_next_provider_and_good_one_is_kept():
    firecrawl = {"provider": "firecrawl", "results": [
        _item("firecrawl", GOOD, "from firecrawl"),
        {"url": OTHER, "title": "", "content": "", "provider": "firecrawl", "error": "Firecrawl scrape failed"},
    ]}
    linkup = {"provider": "linkup", "results": [_item("linkup", OTHER, "from linkup")]}
    with _env("firecrawl", "linkup"), \
         mock.patch.object(providers, "extract_firecrawl", return_value=firecrawl) as fc, \
         mock.patch.object(providers, "extract_linkup", return_value=linkup) as lu:
        result = extract.extract_plus([GOOD, OTHER], provider="firecrawl")

    assert fc.call_args.args[0] == [GOOD, OTHER]
    assert lu.call_args.args[0] == [OTHER]
    assert [item["url"] for item in result["results"]] == [GOOD, OTHER]
    assert [item["content"] for item in result["results"]] == ["from firecrawl", "from linkup"]
    assert [item["provider"] for item in result["results"]] == ["firecrawl", "linkup"]
    assert not any(item.get("error") for item in result["results"])
    # The receipt names the provider the fallback reached as selected.
    assert result["routing"]["provider"] == "linkup"
    assert result["routing"]["fallback_used"] is True


def test_per_url_fallback_v3_response_attributes_each_observation_to_its_provider():
    from wsp_core.compat_v3 import legacy_request_to_v3
    from wsp_core.contract_v3 import Capability

    firecrawl = {"provider": "firecrawl", "results": [_item("firecrawl", GOOD), _item("firecrawl", OTHER, "")]}
    linkup = {"provider": "linkup", "results": [_item("linkup", OTHER, "from linkup")]}
    request = legacy_request_to_v3(Capability.EXTRACT, {"urls": [GOOD, OTHER], "provider": "firecrawl"})
    with _env("firecrawl", "linkup"), \
         mock.patch.object(providers, "extract_firecrawl", return_value=firecrawl), \
         mock.patch.object(providers, "extract_linkup", return_value=linkup):
        response = extract.run_extract_request_v3(request)

    assert response.status.value == "ok"
    assert [r["text"]["text"] for r in response.results] == ["page text", "from linkup"]
    attempts = {a.attempt_id: a.provider for a in response.provider_attempts}
    providers_by_url = {
        o["url"]["observed"]: (o["provider"], attempts[o["provider_attempt_id"]])
        for o in response.observations
    }
    assert providers_by_url == {GOOD: ("firecrawl", "firecrawl"), OTHER: ("linkup", "linkup")}


def test_url_failing_everywhere_stays_an_error_item_in_requested_order():
    firecrawl = {"provider": "firecrawl", "results": [
        {"url": OTHER, "title": "", "content": "", "provider": "firecrawl", "error": "Firecrawl scrape failed"},
        _item("firecrawl", GOOD),
    ]}
    with _env("firecrawl", "linkup"), \
         mock.patch.object(providers, "extract_firecrawl", return_value=firecrawl), \
         mock.patch.object(providers, "extract_linkup", return_value={"provider": "linkup", "results": []}) as lu:
        result = extract.extract_plus([GOOD, OTHER], provider="firecrawl")

    lu.assert_called_once()
    assert [item["url"] for item in result["results"]] == [GOOD, OTHER]
    assert result["results"][0]["content"] == "page text"
    assert result["results"][1]["error"] == "Firecrawl scrape failed"


# d) empty content is a failure and never cached ----------------------------


def test_blank_content_falls_back_and_is_not_cached():
    tavily = {"provider": "tavily", "results": [_item("tavily", GOOD, "   \n")]}
    with _env("tavily"), mock.patch.object(providers, "extract_tavily", return_value=tavily) as tv:
        first = extract.extract_plus([GOOD], provider="tavily")
        second = extract.extract_plus([GOOD], provider="tavily")

    assert first["results"] == []
    assert first["error"] == "All extraction providers failed"
    assert not second.get("cached")
    assert tv.call_count == 2


def test_empty_result_list_triggers_fallback():
    with _env("tavily", "exa"), \
         mock.patch.object(providers, "extract_tavily", return_value={"provider": "tavily", "results": []}), \
         mock.patch.object(providers, "extract_exa", return_value={"provider": "exa", "results": [_item("exa", GOOD)]}) as ex:
        result = extract.extract_plus([GOOD], provider="tavily")

    ex.assert_called_once()
    assert result["results"][0]["content"] == "page text"
    assert result["routing"]["provider"] == "exa"


def test_partial_result_is_not_cached():
    linkup = {"provider": "linkup", "results": [_item("linkup", GOOD)]}
    with _env("linkup"), mock.patch.object(providers, "extract_linkup", return_value=linkup) as lu:
        first = extract.extract_plus([GOOD, OTHER], provider="linkup")
        second = extract.extract_plus([GOOD, OTHER], provider="linkup")

    assert first["results"][1]["url"] == OTHER and first["results"][1]["error"]
    assert not second.get("cached")
    assert lu.call_count == 2


def test_complete_result_is_still_cached():
    linkup = {"provider": "linkup", "results": [_item("linkup", GOOD)]}
    with _env("linkup"), mock.patch.object(providers, "extract_linkup", return_value=linkup) as lu:
        extract.extract_plus([GOOD], provider="linkup")
        second = extract.extract_plus([GOOD], provider="linkup")

    assert second.get("cached") is True
    assert lu.call_count == 1


def test_cache_write_refuses_empty_and_incomplete_results():
    from wsp_core.compat_v3 import legacy_request_to_v3
    from wsp_core.contract_v3 import Capability

    request = legacy_request_to_v3(Capability.EXTRACT, {"urls": [GOOD, OTHER]})

    def eligible(results):
        return extract._extract_cache_write_eligible(
            request, None, None, {"provider": "linkup", "results": results}, {}
        )

    assert eligible([]) is False
    assert eligible([_item("linkup", GOOD)]) is False
    assert eligible([_item("linkup", GOOD), _item("linkup", OTHER, " ")]) is False
    assert eligible([_item("linkup", GOOD), _item("linkup", OTHER)]) is True


# e) Exa statuses ------------------------------------------------------------


def test_exa_failed_statuses_become_error_items():
    data = {
        "results": [{"url": GOOD, "id": GOOD, "title": "Good", "text": "exa text"}],
        "statuses": [
            {"id": GOOD, "status": "success"},
            {"id": OTHER, "status": "error", "error": {"tag": "CRAWL_NOT_FOUND", "httpStatusCode": 404}},
        ],
    }
    with mock.patch.object(providers, "make_request", return_value=data):
        result = providers.extract_exa([GOOD, OTHER], "exa-test")

    assert [item["url"] for item in result["results"]] == [GOOD, OTHER]
    assert not result["results"][0].get("error")
    assert result["results"][1]["error"] == "Exa could not fetch this URL"


# URLs past the fan-out cap -----------------------------------------------------


def test_urls_past_the_fan_out_cap_get_their_own_not_tried_row(dns):
    # Live on hermi: 12 URLs requested, 10 extracted, the last two vanished from
    # the tool output with only an internal warning.
    urls = [f"https://example.com/page-{index}" for index in range(12)]

    def fake_firecrawl(batch, *args, **kwargs):
        return {"provider": "firecrawl", "results": [_item("firecrawl", url) for url in batch]}

    with _env("firecrawl"), mock.patch.object(providers, "extract_firecrawl", side_effect=fake_firecrawl) as fc:
        result = extract.extract_plus(urls, provider="firecrawl")

    assert fc.call_args.args[0] == urls[:10]
    assert [item["url"] for item in result["results"]] == urls
    assert all(item["content"] == "page text" for item in result["results"][:10])
    for item in result["results"][10:]:
        assert item["error"] == "not tried: limit of 10 URLs per call reached"
        assert not item.get("content")


def test_requests_within_the_cap_get_no_extra_rows(dns):
    urls = [f"https://example.com/page-{index}" for index in range(3)]

    def fake_firecrawl(batch, *args, **kwargs):
        return {"provider": "firecrawl", "results": [_item("firecrawl", url) for url in batch]}

    with _env("firecrawl"), mock.patch.object(providers, "extract_firecrawl", side_effect=fake_firecrawl):
        result = extract.extract_plus(urls, provider="firecrawl")

    assert [item["url"] for item in result["results"]] == urls
    assert not any(item.get("error") for item in result["results"])
