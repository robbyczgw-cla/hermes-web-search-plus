"""Explicit-only Search1API source provider for Web Search Plus.

The adapter calls Search1API's /search, /news, and /crawl endpoints and
projects ranked source results and extracted page text only.  News requests
(``search_type="news"``) are served by the /news endpoint; general search uses
/search.  Extraction returns Markdown text; the html/raw-html/render-js tool
flags are accepted for compatibility but have no upstream effect.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wsp_sdk import (
    ProviderConfigError,
    ProviderRequestError,
    ProviderSpec,
    extract_result,
    register_provider,
    search_result,
    source_result,
)

_API_BASE = "https://api.search1api.com"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_QUERY_CHARS = 2_000
_MAX_URL_CHARS = 8_192
_MAX_TITLE_CHARS = 1_000
_MAX_SNIPPET_CHARS = 8_000
_MAX_COUNT = 50
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_FRESHNESS_VALUES = {"day", "week", "month", "year"}
_SEARCH_SERVICES = {
    "google",
    "bing",
    "bingcn",
    "duckduckgo",
    "yahoo",
    "youtube",
    "x",
    "reddit",
    "github",
    "arxiv",
    "wechat",
    "bilibili",
    "imdb",
    "wikipedia",
    "baidu",
    "360",
    "quark",
}
_NEWS_SERVICES = {
    "google",
    "bing",
    "duckduckgo",
    "yahoo",
    "hackernews",
    "reuters",
}
_DEFAULT_SEARCH_SERVICE = "google"
_DEFAULT_NEWS_SERVICE = "bing"


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the API key on the fixed Search1API origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _open_request(request: Request, timeout: int):
    return _OPENER.open(request, timeout=timeout)


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get("search1api", {})
    return section if isinstance(section, Mapping) else {}


def _timeout(config: Mapping[str, Any]) -> int:
    raw = _section(config).get("timeout", 30)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ProviderConfigError("search1api_timeout_invalid") from None
    if not 1 <= value <= 120:
        raise ProviderConfigError("search1api_timeout_invalid")
    return value


def _service(config: Mapping[str, Any], field: str, allowed: set[str], default: str) -> str:
    raw = _section(config).get(field, default)
    if not isinstance(raw, str):
        raise ProviderConfigError(f"search1api_{field}_invalid")
    value = raw.strip().lower()
    if value not in allowed:
        raise ProviderConfigError(f"search1api_{field}_invalid")
    return value


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
    return parsed if parsed >= 0 else None


def _read_payload(response: Any) -> dict[str, Any]:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProviderRequestError("search1api_response_too_large", transient=True)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderRequestError("search1api_invalid_response", transient=True) from None
    if not isinstance(payload, dict):
        raise ProviderRequestError("search1api_invalid_response", transient=True)
    return payload


def _request(path: str, body: Mapping[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    request = Request(
        f"{_API_BASE}{path}",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _open_request(request, timeout) as response:
            return _read_payload(response)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise ProviderConfigError("search1api_key_rejected") from None
        raise ProviderRequestError(
            f"search1api_http_{exc.code}",
            status_code=exc.code,
            transient=exc.code in _TRANSIENT_STATUS,
            retry_after=_retry_after(exc),
        ) from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise ProviderRequestError("search1api_unavailable", transient=True) from None


def _bounded_string(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_domains(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _safe_url(value: Any) -> str:
    url = _bounded_string(value, _MAX_URL_CHARS)
    if not url.startswith(("https://", "http://")) or any(
        character.isspace() for character in url
    ):
        return ""
    return url


def execute_search(search_module, prov, args, key, config, routing_info):
    if not isinstance(key, str) or not key.strip():
        raise ProviderConfigError("search1api_key_required")

    try:
        count = max(1, min(int(getattr(args, "max_results", 5)), _MAX_COUNT))
    except (TypeError, ValueError):
        raise ProviderConfigError("search1api_max_results_invalid") from None

    query = _bounded_string(getattr(args, "query", None), _MAX_QUERY_CHARS)
    if not query:
        raise ProviderConfigError("search1api_query_invalid")

    news = getattr(args, "search_type", "search") == "news"
    body: dict[str, Any] = {
        "query": query,
        "max_results": count,
        "search_service": _service(
            config,
            "news_service" if news else "search_service",
            _NEWS_SERVICES if news else _SEARCH_SERVICES,
            _DEFAULT_NEWS_SERVICE if news else _DEFAULT_SEARCH_SERVICE,
        ),
        # Source-only contract: snippets stay attached to their links; inline
        # page crawling stays off so /search never returns expanded content.
        "crawl_results": 0,
    }
    include_domains = _clean_domains(getattr(args, "include_domains", None))
    exclude_domains = _clean_domains(getattr(args, "exclude_domains", None))
    if include_domains:
        body["include_sites"] = include_domains
    if exclude_domains:
        body["exclude_sites"] = exclude_domains

    freshness = getattr(args, "freshness", None)
    if freshness not in _FRESHNESS_VALUES:
        freshness = getattr(args, "time_range", None)
    if freshness in _FRESHNESS_VALUES:
        body["time_range"] = freshness

    payload = _request("/news" if news else "/search", body, key.strip(), _timeout(config))
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ProviderRequestError("search1api_invalid_response", transient=True)

    projected = []
    for item in raw_results[:count]:
        if not isinstance(item, Mapping):
            continue
        url = _safe_url(item.get("link") or item.get("url"))
        if not url:
            continue
        fields: dict[str, Any] = {
            "title": _bounded_string(item.get("title"), _MAX_TITLE_CHARS),
            "snippet": _bounded_string(item.get("snippet"), _MAX_SNIPPET_CHARS),
        }
        for field in ("date", "source"):
            value = _bounded_string(item.get(field), 1_000)
            if value:
                fields[field] = value
        projected.append(source_result(url, **fields))

    return search_result(
        prov,
        query,
        projected,
        metadata={
            "endpoint": "news" if news else "search",
            "service": body["search_service"],
        },
    )


def execute_extract(
    extract_module,
    prov,
    urls,
    key,
    output_format,
    include_images,
    include_raw_html,
    render_js,
    config,
    keyless_allowed,
):
    if not isinstance(key, str) or not key.strip():
        raise ProviderConfigError("search1api_key_required")

    timeout = _timeout(config)
    projected = []
    for requested_url in urls:
        url = _safe_url(requested_url)
        if not url:
            projected.append({"url": str(requested_url)[:_MAX_URL_CHARS], "error": "search1api_url_invalid", "status": 0})
            continue
        try:
            payload = _request("/crawl", {"url": url}, key.strip(), timeout)
        except ProviderConfigError:
            raise
        except ProviderRequestError as exc:
            projected.append({"url": url, "error": str(exc), "status": exc.status_code or 0})
            # A transient upstream failure is recorded per URL, then the batch
            # stops: retrying the remaining URLs against a sick endpoint burns
            # quota.  Deterministic per-URL failures keep going.
            if exc.transient:
                break
            continue

        results = payload.get("results")
        if not isinstance(results, Mapping):
            projected.append({"url": url, "error": "search1api_invalid_response", "status": 0})
            continue
        content = _bounded_string(results.get("content"), _MAX_SNIPPET_CHARS * 32)
        if not content:
            projected.append({"url": url, "error": "search1api_empty_content", "status": 0})
            continue
        result = source_result(
            _safe_url(results.get("link")) or url,
            title=_bounded_string(results.get("title"), _MAX_TITLE_CHARS),
            content=content,
            images=[],
            status=200,
            fetcher="search1api",
            source_type="web",
        )
        if include_raw_html:
            # Search1API crawl returns Markdown only; do not label it HTML.
            result["raw_error"] = "search1api_raw_html_unsupported"
        projected.append(result)
    return extract_result(prov, projected)


PROVIDER = register_provider(
    ProviderSpec(
        id="search1api",
        kind="both",
        env_var="SEARCH1API_KEY",
        display_name="Search1API",
        description=(
            "Web search, news vertical, and page extraction through one "
            "Search1API key. search_type=news uses the /news endpoint; "
            "extraction returns Markdown text (html/raw-html/render-js flags "
            "have no upstream effect). Review "
            "https://blog.s1.dev/pages/terms and "
            "https://s1.dev/privacy before use. "
            "Explicit-only by default."
        ),
        config_section="search1api",
        capability_labels=("search", "news", "extract", "freshness"),
        upstream_capabilities=("search", "news", "extract", "freshness", "domain-filtering"),
        auto_allowed_by_default=False,
        recommended=False,
        supports_freshness=True,
        free_tier="Free plan: 100 credits; 1 credit per search/news/crawl call",
        signup_url="https://s1.dev",
        execute_search=execute_search,
        execute_extract=execute_extract,
    )
)
