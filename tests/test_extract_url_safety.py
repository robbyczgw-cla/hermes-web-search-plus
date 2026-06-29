from unittest import mock

import pytest

import extract


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://LOCALHOST.:8080/admin",
        "http://localhost:8080/admin",
        "http://0.0.0.0/admin",
        "http://10.0.0.5/private",
        "http://172.16.0.5/private",
        "http://192.168.1.5/private",
        "http://100.64.0.1/private",
        "http://100.100.100.100/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://224.0.0.1/private",
        "http://[::1]/admin",
        "http://[::ffff:127.0.0.1]/admin",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[fe80::1]/admin",
        "http://[fc00::1]/admin",
        "http://[fd12:3456:789a::1]/admin",
        "http://[ff02::1]/multicast",
        "http://[::]/unspecified",
        "http://[2001:db8::1]/doc",
        "https://PUBLIC.example.test@127.0.0.1/admin",
        "http://2130706433/admin",
    ],
)
def test_extract_urls_reject_private_internal_targets(url):
    with pytest.raises(extract.ExtractUrlSecurityError):
        extract._validate_extract_urls([url], config={})


def test_extract_urls_reject_hostname_that_resolves_to_private_ip():
    with mock.patch(
        "extract.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("192.168.1.20", 443))],
    ):
        with pytest.raises(extract.ExtractUrlSecurityError):
            extract._validate_extract_urls(["https://internal.example.test/page"], config={})


def test_extract_urls_reject_hostname_if_any_dns_answer_is_private():
    with mock.patch(
        "extract.socket.getaddrinfo",
        return_value=[
            (None, None, None, None, ("93.184.216.34", 443)),
            (None, None, None, None, ("127.0.0.1", 443)),
        ],
    ):
        with pytest.raises(extract.ExtractUrlSecurityError):
            extract._validate_extract_urls(["https://rebinding.example.test/page"], config={})


def test_extract_urls_allow_public_targets():
    with mock.patch(
        "extract.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("93.184.216.34", 443))],
    ):
        assert extract._validate_extract_urls(["https://example.com/page"], config={}) == ["https://example.com/page"]


def test_extract_private_url_escape_hatch_is_explicit():
    config = {"extract": {"allow_private_urls": True}}
    assert extract._validate_extract_urls(["http://127.0.0.1:8080/admin"], config=config) == ["http://127.0.0.1:8080/admin"]


def test_extract_plus_rejects_private_url_before_provider_dispatch():
    with mock.patch("extract.extract_firecrawl") as mock_extract:
        result = extract.extract_plus(
            ["http://169.254.169.254/latest/meta-data/"],
            provider="firecrawl",
            config={"firecrawl": {"api_key": "fc-test-key"}},
        )

    mock_extract.assert_not_called()
    assert result["results"] == []
    assert "private/internal" in result["error"]


def test_local_provider_endpoint_remains_allowed_for_public_target():
    with mock.patch("extract._validate_extract_urls", return_value=["https://example.com/page"]), \
         mock.patch("extract.get_api_key", return_value="fc-test-key"), \
         mock.patch("extract.provider_in_cooldown", return_value=(False, 0)), \
         mock.patch("extract.reset_provider_health"), \
         mock.patch("extract.extract_firecrawl", return_value={"provider": "firecrawl", "results": []}) as mock_extract:
        result = extract.extract_plus(
            ["https://example.com/page"],
            provider="firecrawl",
            config={"firecrawl": {"scrape_url": "http://127.0.0.1:8080/v2/scrape"}},
        )

    assert result["provider"] == "firecrawl"
    assert mock_extract.call_args.kwargs["api_url"] == "http://127.0.0.1:8080/v2/scrape"
