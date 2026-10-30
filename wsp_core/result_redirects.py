"""Remove Google redirect wrappers before sources reach ranking or caches."""

from __future__ import annotations

import math
import queue
import re
import time
from urllib.parse import parse_qs, urlsplit

from . import http_client
from .daemon_tasks import DaemonTask
from .urls import url_key


_GOOGLE_HOST = re.compile(r"(?:www\.)?google\.(?:[a-z]{2,63}|(?:co|com)\.[a-z]{2})\Z")
_LINK_TIMEOUT = 2.0
_MAX_PARALLEL = 5
_MIN_REMAINING = 0.05
COUNT_KEYS = ("redirects_wrapped", "redirects_resolved", "redirects_dropped")
# Also keeps results cached before this policy from leaking wrapper URLs.
CACHE_POLICY = "google-redirects-url-sanity-v2"


def _google_redirect_path(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if _GOOGLE_HOST.fullmatch((parts.hostname or "").lower().rstrip(".")):
            return parts.path if parts.path in {"/goto", "/url"} else None
    except ValueError:
        pass
    return None


def wrapped_url(url: str) -> tuple[str, str] | None:
    """(path, parameter) for opaque goto and locally decodable classic links."""
    path = _google_redirect_path(url)
    if path:
        params = parse_qs(urlsplit(url).query, keep_blank_values=True)
        for name in (("url",) if path == "/goto" else ("q", "url")):
            if name in params:
                return path, params[name][0]
    return None


def _safe_destination(url: str) -> str | None:
    if not url or url != url.strip() or _google_redirect_path(url):
        return None
    # Import lazily: extract imports the search engine for shared routing.
    from .extract import _validate_extract_urls

    return _validate_extract_urls([url])[0]


def _resolve(url: str, wrapper: tuple[str, str]) -> str | None:
    remaining = http_client.remaining_seconds()
    if remaining is not None and remaining < _MIN_REMAINING:
        return None
    try:
        if wrapper[0] == "/url":
            destination = wrapper[1]  # parse_qs already percent-decoded it once
        else:
            # Apply the extraction guard to the outbound URL too. Even a
            # Google-looking hostname must not resolve to a private address.
            from .extract import _validate_extract_urls

            checked = _validate_extract_urls([url])[0]
            destination = http_client.redirect_location(checked, timeout=_LINK_TIMEOUT)
        return _safe_destination(destination) if destination else None
    except Exception:
        # A broken source must not fail an otherwise successful search. Never
        # expose tokens, Location values or resolver errors in the metadata.
        return None


def clean_result_redirects(payload: dict, config: dict) -> None:
    """Resolve/drop wrappers in place, with bounded workers and a wall deadline.

    Workers publish values only; after a timeout they cannot mutate a returned
    result. DaemonTask copies the request deadline and timeout-cap context.
    DNS preflight can block beyond a socket timeout, so the caller also bounds
    its wait and each worker checks its deadline before issuing another GET.
    """
    results = payload.get("results") or []
    wrappers = {
        index: wrapper for index, item in enumerate(results)
        if isinstance(item, dict) and isinstance(item.get("url"), str)
        and (wrapper := wrapped_url(item["url"])) is not None
    }
    if not wrappers:
        return
    resolved: dict[int, str] = {}
    # Classic /url wrappers carry the destination in their query: decode them locally,
    # always, without any request.
    for index, wrapper in wrappers.items():
        if wrapper[0] == "/url":
            destination = _resolve(results[index]["url"], wrapper)
            if destination:
                resolved[index] = destination
    network = [index for index, wrapper in wrappers.items() if wrapper[0] != "/url"]
    # Opt-in: live tests (2026-10-30) resolved only the first /goto link per search; Google
    # serves a JavaScript shell for the rest, so resolution mostly costs latency and a
    # request from the user's machine to Google for about one extra source.
    if network and (config.get("serper") or {}).get("resolve_redirects", False) is True:
        completed = queue.Queue()
        pending = queue.Queue()
        for index in network:
            pending.put(index)
        deadline = time.monotonic() + math.ceil(len(network) / _MAX_PARALLEL) * _LINK_TIMEOUT
        remaining = http_client.remaining_seconds()
        if remaining is not None:
            deadline = min(deadline, time.monotonic() + max(0.0, remaining))

        def worker():
            with http_client.request_deadline_scope(deadline):
                while not pending.empty():
                    if (http_client.remaining_seconds() or 0) < _MIN_REMAINING:
                        break
                    try:
                        index = pending.get_nowait()
                    except queue.Empty:
                        break
                    with http_client.request_deadline_scope(time.monotonic() + _LINK_TIMEOUT):
                        destination = _resolve(results[index]["url"], wrappers[index])
                        if destination and not http_client.deadline_passed():
                            completed.put((index, destination))

        if deadline - time.monotonic() >= _MIN_REMAINING:
            tasks = [DaemonTask(worker) for _ in range(min(_MAX_PARALLEL, len(network)))]
            for task in tasks:
                try:
                    task.result(timeout=max(0.0, deadline - time.monotonic()))
                except TimeoutError:
                    pass
            while not completed.empty():
                index, destination = completed.get_nowait()
                resolved[index] = destination

    kept = []
    seen = set()
    duplicates = 0
    for index, item in enumerate(results):
        if index in wrappers:
            if index not in resolved:
                continue
            item = {**item, "url": resolved[index]}
        identity = url_key(item.get("url", "")) if isinstance(item, dict) else ""
        if identity and identity in seen:
            duplicates += 1
            continue
        if identity:
            seen.add(identity)
        kept.append(item)
    payload["results"] = kept
    metadata = payload.setdefault("metadata", {})
    metadata.update(zip(COUNT_KEYS, (len(wrappers), len(resolved), len(wrappers) - len(resolved))))
    if duplicates:
        payload["deduplicated"] = True
        metadata["dedup_count"] = metadata.get("dedup_count", 0) + duplicates


def combined_redirect_counts(provider_results: list) -> dict:
    """Keep redirect and URL-sanity counts across fallback and Research merges."""
    totals = {
        key: sum((payload.get("metadata") or {}).get(key, 0) for _, payload in provider_results)
        for key in COUNT_KEYS
    }
    counts = totals if totals["redirects_wrapped"] else {}
    invalid = sum((payload.get("metadata") or {}).get("invalid_urls_dropped", 0) for _, payload in provider_results)
    if invalid:
        counts["invalid_urls_dropped"] = invalid
    return counts
