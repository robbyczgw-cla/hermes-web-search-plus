"""Explicit-only TinyFish source-search provider for Web Search Plus.

The adapter calls TinyFish's fixed Search API origin and projects structured
ranked sources only. It deliberately omits purpose, fetch, Agent, Browser, and
other content-expanding modes. TinyFish remains explicit-only because its
standard Terms permit Customer Data to be used for model training/fine-tuning.
"""

from __future__ import annotations

import json
import math
import re
import socket
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wsp_sdk import (
    ProviderConfigError,
    ProviderRequestError,
    ProviderSpec,
    register_provider,
    search_result,
    source_result,
)

_API_URL = "https://api.search.tinyfish.ai/"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_QUERY_CHARS = 2_000
_MAX_DOMAIN_COUNT = 20
_MAX_DOMAIN_CHARS = 253
_MAX_DOMAIN_LIST_CHARS = 2_048
_MAX_REQUEST_URL_CHARS = 8_192
_MAX_URL_CHARS = 8_192
_MAX_TITLE_CHARS = 1_000
_MAX_SNIPPET_CHARS = 8_000
_TRANSIENT_STATUS = {429, 500, 503}
_FRESHNESS_MINUTES = {
    "day": 24 * 60,
    "week": 7 * 24 * 60,
    "month": 30 * 24 * 60,
    "year": 365 * 24 * 60,
}


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the API key on TinyFish's fixed Search API origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _open_request(request: Request, timeout: int):
    return _OPENER.open(request, timeout=timeout)


def _timeout(config: Mapping[str, Any]) -> int:
    section = config.get("tinyfish", {})
    raw = section.get("timeout", 30) if isinstance(section, Mapping) else 30
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ProviderConfigError("tinyfish_timeout_invalid") from None
    if not 1 <= value <= 120:
        raise ProviderConfigError("tinyfish_timeout_invalid")
    return value


def _canonical_hostname(hostname: str) -> str:
    if (
        not hostname
        or hostname.endswith("..")
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in hostname
        )
    ):
        return ""
    token = hostname[:-1] if hostname.endswith(".") else hostname
    if not token.isascii():
        return ""
    canonical = token.lower()
    labels = canonical.split(".")
    if (
        not canonical
        or len(canonical) > _MAX_DOMAIN_CHARS
        or len(labels) < 2
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    ):
        return ""
    return canonical


def _clean_domains(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    if len(value) > _MAX_DOMAIN_COUNT:
        raise ProviderConfigError("tinyfish_domains_invalid")

    raw_items: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or any(
                character.isspace()
                or unicodedata.category(character).startswith("C")
                for character in item
            )
        ):
            raise ProviderConfigError("tinyfish_domains_invalid")
        has_root_dot = item.endswith(".")
        if len(item) > _MAX_DOMAIN_CHARS + (1 if has_root_dot else 0):
            raise ProviderConfigError("tinyfish_domains_invalid")
        raw_items.append(item)
    if len(",".join(raw_items)) > _MAX_DOMAIN_LIST_CHARS:
        raise ProviderConfigError("tinyfish_domains_invalid")

    domains: list[str] = []
    for item in raw_items:
        raw = item.lower()
        if raw.endswith(".."):
            raise ProviderConfigError("tinyfish_domains_invalid")
        has_root_dot = raw.endswith(".")
        token = raw[:-1] if has_root_dot else raw
        wildcard = token.startswith("*.")
        hostname = token[2:] if wildcard else token
        canonical = _canonical_hostname(hostname)
        if not canonical:
            raise ProviderConfigError("tinyfish_domains_invalid")
        normalized = f"*.{canonical}" if wildcard else canonical
        if len(normalized) > _MAX_DOMAIN_CHARS:
            raise ProviderConfigError("tinyfish_domains_invalid")
        if normalized not in domains:
            domains.append(normalized)

    if len(",".join(domains)) > _MAX_DOMAIN_LIST_CHARS:
        raise ProviderConfigError("tinyfish_domains_invalid")
    return domains


def _retry_after(error: HTTPError) -> float | None:
    if error.code != 429:
        return None
    try:
        value = error.headers.get("Retry-After")
        if value is None:
            return None
        parsed = float(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _read_payload(response: Any) -> dict[str, Any]:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProviderRequestError("tinyfish_response_too_large", transient=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderRequestError("tinyfish_invalid_response", transient=True) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ProviderRequestError("tinyfish_invalid_response", transient=True)
    return payload


def _safe_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_URL_CHARS
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        return ""
    url = value
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""
    canonical_hostname = _canonical_hostname(hostname)
    if (
        parsed.scheme not in {"http", "https"}
        or not canonical_hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return ""
    return url


def _bounded_string(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(
        character
        for character in value
        if character in {" ", "\n", "\t"}
        or (
            not unicodedata.category(character).startswith("C")
            and not character.isspace()
        )
    ).strip()
    return cleaned[:limit]


def _domain_matches(hostname: str, domain: str) -> bool:
    normalized = domain.strip().lower().rstrip(".")
    if normalized.startswith("*."):
        normalized = normalized[2:]
    return bool(normalized) and (
        hostname == normalized or hostname.endswith(f".{normalized}")
    )


def _url_allowed_by_domains(
    url: str,
    include_domains: list[str],
    exclude_domains: list[str],
) -> bool:
    try:
        hostname = _canonical_hostname(urlsplit(url).hostname or "")
    except ValueError:
        return False
    if not hostname or any(_domain_matches(hostname, domain) for domain in exclude_domains):
        return False
    return not include_domains or any(
        _domain_matches(hostname, domain) for domain in include_domains
    )


def _request(params: Mapping[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    request_url = f"{_API_URL}?{urlencode(params)}"
    if len(request_url) > _MAX_REQUEST_URL_CHARS:
        raise ProviderConfigError("tinyfish_request_too_large")
    request = Request(
        request_url,
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with _open_request(request, timeout) as response:
            return _read_payload(response)
    except HTTPError as exc:
        transient = exc.code in _TRANSIENT_STATUS
        raise ProviderRequestError(
            f"tinyfish_http_{exc.code}",
            status_code=exc.code,
            transient=transient,
            retry_after=_retry_after(exc),
        ) from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise ProviderRequestError("tinyfish_unavailable", transient=True) from None


def _query(args: Any) -> str:
    value = getattr(args, "query", None)
    if not isinstance(value, str):
        raise ProviderConfigError("tinyfish_query_invalid")
    value = value.strip()
    if not value or len(value) > _MAX_QUERY_CHARS:
        raise ProviderConfigError("tinyfish_query_invalid")
    return value


def _query_params(
    args: Any,
    query: str,
    include_domains: list[str],
    exclude_domains: list[str],
) -> dict[str, Any]:
    params: dict[str, Any] = {"query": query}

    country = getattr(args, "country", None)
    if isinstance(country, str) and country.strip():
        params["location"] = country.strip().upper()
    language = getattr(args, "language", None)
    if isinstance(language, str) and language.strip():
        params["language"] = language.strip().lower()

    if include_domains:
        params["include_domains"] = ",".join(include_domains)
    if exclude_domains:
        params["exclude_domains"] = ",".join(exclude_domains)

    params["domain_type"] = (
        "news" if getattr(args, "search_type", "search") == "news" else "web"
    )
    freshness = getattr(args, "freshness", None)
    if freshness not in _FRESHNESS_MINUTES:
        freshness = getattr(args, "time_range", None)
    if freshness in _FRESHNESS_MINUTES:
        params["recency_minutes"] = _FRESHNESS_MINUTES[freshness]

    # Intentionally omitted: purpose, fetch, include_thumbnail, and pagination.
    # WSP asks TinyFish only for source results and applies its own result bound.
    return params


def execute_search(search_module, prov, args, key, config, routing_info):
    if not isinstance(key, str) or not key.strip():
        raise ProviderConfigError("tinyfish_api_key_required")

    try:
        count = max(1, min(int(getattr(args, "max_results", 5)), 100))
    except (TypeError, ValueError):
        raise ProviderConfigError("tinyfish_max_results_invalid") from None

    query = _query(args)
    include_domains = _clean_domains(getattr(args, "include_domains", None))
    exclude_domains = _clean_domains(getattr(args, "exclude_domains", None))
    payload = _request(
        _query_params(args, query, include_domains, exclude_domains),
        key.strip(),
        _timeout(config),
    )
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ProviderRequestError("tinyfish_invalid_response", transient=True)

    projected = []
    for item in raw_results:
        if len(projected) >= count:
            break
        if not isinstance(item, Mapping):
            continue
        url = _safe_url(item.get("url"))
        if not url or not _url_allowed_by_domains(url, include_domains, exclude_domains):
            continue

        fields: dict[str, Any] = {
            "title": _bounded_string(item.get("title"), _MAX_TITLE_CHARS),
            "snippet": _bounded_string(item.get("snippet"), _MAX_SNIPPET_CHARS),
        }
        optional_strings = {
            "date": item.get("date"),
            "source": item.get("site_name"),
            "author": item.get("publisher"),
        }
        for field, value in optional_strings.items():
            projected_value = _bounded_string(value, 1_000)
            if projected_value:
                fields[field] = projected_value
        position = item.get("position")
        if isinstance(position, int) and not isinstance(position, bool) and position >= 1:
            fields["position"] = position
        projected.append(source_result(url, **fields))

    metadata: dict[str, int] = {}
    for field in ("total_results", "page"):
        value = payload.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            metadata[field] = value

    return search_result(
        prov,
        query,
        projected,
        metadata=metadata,
    )


PROVIDER = register_provider(
    ProviderSpec(
        id="tinyfish",
        kind="search",
        env_var="TINYFISH_API_KEY",
        display_name="TinyFish Search",
        description=(
            "Direct source-only TinyFish web/news search using your own account/API key. "
            "WSP does not provide, pool, proxy, or share TinyFish credentials. "
            "Domain filters and result hosts are accepted only as ASCII/Punycode hostnames. "
            "Privacy warning: TinyFish's "
            "standard Terms permit Customer Data to be used for model training and "
            "fine-tuning; review https://www.tinyfish.ai/terms and "
            "https://www.tinyfish.ai/privacy-policy before use. Explicit-only by default."
        ),
        config_section="tinyfish",
        capability_labels=("search", "news", "freshness", "privacy-warning"),
        upstream_capabilities=(
            "search",
            "news",
            "research-paper",
            "freshness",
            "domain-filtering",
        ),
        auto_allowed_by_default=False,
        recommended=False,
        supports_freshness=True,
        free_tier="Search does not consume credits; API access required (30 rpm Free/PAYG)",
        signup_url="https://agent.tinyfish.ai/api-keys",
        execute_search=execute_search,
    )
)
