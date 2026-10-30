"""Opt-in native Hermes WebSearchProvider bridge for Web Search Plus.

Registers as backend name ``wsp``. Selection is strictly explicit via Hermes
config keys:

* ``web.search_backend: wsp``
* ``web.extract_backend: wsp``
* ``web.backend: wsp`` (shared fallback)

Never auto-selected. Never falls back to a subprocess/sidecar path — if the
in-process WSP engine cannot load, search/extract return structured errors.
Existing ``web_search_plus`` / ``web_extract_plus`` tools are unaffected.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

try:  # package import (Hermes); the standalone unit-test load has no parent
    from .wsp_core.extract_match import match_extract_items as _match_extract_items
except ImportError:  # pragma: no cover - exercised only outside the package
    _match_extract_items = None


def _load_web_config() -> Dict[str, Any]:
    """Read optional web settings through Hermes' public config API."""
    try:
        from hermes_cli.config import load_config

        web = load_config().get("web") or {}
        return web if isinstance(web, dict) else {}
    except Exception:  # noqa: BLE001 — unavailable/malformed config is optional
        return {}


def _match_rows(urls: List[str], rows: List[Dict[str, Any]]) -> List[Optional[Dict[str, Any]]]:
    """Pair engine rows with requested URLs (exact, canonical, reported source URL)."""
    if _match_extract_items is not None:
        return _match_extract_items(urls, rows)[0]
    by_url: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        by_url.setdefault(row["url"], row)
    return [by_url.get(url) for url in urls]

# WSP engine hard-cap (matches web_search_plus schema maximum).
_WSP_MAX_RESULTS = 20

_NATIVE_BACKEND_NAME = "wsp"
_NATIVE_DISPLAY_NAME = "Web Search Plus (native)"

# Prefix of every error the engine's URL guard raises for a target it refused on
# purpose (private/internal address, blocked name, unresolvable host).
_URL_REFUSED_PREFIX = "Extraction URL blocked"
_URL_REFUSED_MESSAGE = (
    "Blocked by Web Search Plus URL policy: private/internal or unresolvable address"
)


def _refused_entry(url: str) -> Dict[str, Any]:
    """Error entry carrying Hermes' ``blocked_by_policy`` marker.

    Hermes' one-shot keyless rescue (``web.keyless_rescue``) re-sends failed
    extract items to anonymous third-party backends, except entries with a
    truthy ``blocked_by_policy`` (tools/web_tools_rescue.py). A URL WSP refused
    on purpose must not be forwarded there. Only the requested host is named,
    never a resolved address.
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return {
        "url": url,
        "error": _URL_REFUSED_MESSAGE,
        "blocked_by_policy": {"host": host, "rule": "wsp-url-guard", "source": "web-search-plus"},
    }



# Same limit as the plugin's own web_search_plus display: some providers return
# whole page sections as a snippet (Exa: 5,113 characters for one result).
_DESCRIPTION_CHARS = 1200


def _capped_description(text: str) -> str:
    """Cut a long snippet at a word boundary, with a visible truncation marker."""
    flat = " ".join(text.split())
    if len(flat) <= _DESCRIPTION_CHARS:
        return text
    cut = flat[: _DESCRIPTION_CHARS + 1].rpartition(" ")[0]
    if len(cut) < _DESCRIPTION_CHARS // 2:
        cut = flat[:_DESCRIPTION_CHARS]
    return f"{cut} … [TRUNCATED: showing first {len(cut)} of {len(text)} characters]"


class WSPNativeBackend(WebSearchProvider):
    """In-process bridge from Hermes ``web_search``/``web_extract`` to WSP."""

    def __init__(
        self,
        plugin: Any = None,
        *,
        search_provider: str = "auto",
        extract_provider: str = "auto",
    ) -> None:
        if plugin is None:
            raise RuntimeError(
                "WSPNativeBackend requires the web-search-plus plugin module"
            )
        self.plugin = plugin
        self.search_provider = search_provider or "auto"
        self.extract_provider = extract_provider or "auto"

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return _NATIVE_BACKEND_NAME

    @property
    def display_name(self) -> str:
        return _NATIVE_DISPLAY_NAME

    # -- selection / capability gates ---------------------------------------

    def _selected(self, capability: str) -> bool:
        """True when Hermes config pins this backend for *capability*.

        Mirrors Hermes resolution order for a single name check:
        ``web.{capability}_backend`` then shared ``web.backend``.
        """
        try:
            cfg = _load_web_config() or {}
        except Exception:  # noqa: BLE001 — config optional at probe time
            cfg = {}
        if not isinstance(cfg, dict):
            return False
        selected = cfg.get(f"{capability}_backend") or cfg.get("backend") or ""
        return isinstance(selected, str) and selected.strip().lower() == self.name

    def is_available(self) -> bool:
        # Strictly opt-in. Must stay False unless explicitly selected so the
        # registry single-provider shortcut and _get_backend plugin walk never
        # auto-route onto wsp.
        if not (self._selected("search") or self._selected("extract")) or not self._inprocess_ready():
            return False
        try:
            engine = self.plugin._load_search_module()
            config = engine.load_config()
            auto = config.get("auto_routing", {})
            disabled = set(auto.get("disabled_providers", []) or [])
            for capability, pin in (("search", self.search_provider), ("extract", self.extract_provider)):
                if not self._selected(capability):
                    continue
                ids = getattr(engine, capability.upper() + "_PROVIDER_IDS")
                if capability == "search" and pin == "auto" and auto.get("enabled") is False:
                    pin = config.get("default_provider") or "auto"
                candidates = ids if pin == "auto" else (pin,)
                for provider in candidates:
                    if provider not in ids or provider in disabled:
                        continue
                    if pin == "auto" and not engine._provider_auto_allowed(provider, auto):
                        continue
                    if engine.provider_configured(provider, config):
                        return True
        except Exception as exc:
            # A refused credential read (Hermes multiplexing without a profile
            # scope) is Hermes' to report, not "unavailable".
            if self._is_secret_scope_error(exc):
                raise
            # Offline config inspection only; never probe vendor I/O here.
            return False
        return False

    def _is_secret_scope_error(self, exc: BaseException) -> bool:
        check = getattr(self.plugin, "is_secret_scope_error", None)
        return bool(callable(check) and check(exc))

    def supports_search(self) -> bool:
        return self._selected("search")

    def supports_extract(self) -> bool:
        return self._selected("extract")

    def is_keyless_available(self) -> bool:
        # Never participate in the keyless free-tier walk.
        return False

    # -- in-process readiness (no sidecar) ----------------------------------

    def _inprocess_ready(self) -> bool:
        load = getattr(self.plugin, "_load_search_module", None)
        if not callable(load):
            return False
        try:
            return load() is not None
        except Exception:  # noqa: BLE001
            logger.warning("WSP native backend: engine load failed")
            return False

    @staticmethod
    def _clamp_count(limit: Any) -> int:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 5
        return min(_WSP_MAX_RESULTS, max(1, n))

    # -- search -------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        if not self._inprocess_ready():
            return {
                "success": False,
                "error": "WSP in-process engine unavailable; no sidecar fallback",
                "data": {"web": []},
            }
        count = self._clamp_count(limit)
        run = getattr(self.plugin, "_run_search")
        try:
            data = run(
                query=query,
                provider=self.search_provider,
                count=count,
            )
        except Exception:  # noqa: BLE001 — do not expose vendor exception details
            logger.warning("WSP native search failed")
            return {
                "success": False,
                "error": "WSP native search failed",
                "data": {"web": []},
            }

        if not isinstance(data, dict):
            return {
                "success": False,
                "error": "WSP native search returned malformed payload",
                "data": {"web": []},
            }

        if data.get("error"):
            return {
                "success": False,
                "error": self._failure_message(data, "WSP native search failed; check provider readiness and configuration"),
                "data": {"web": []},
            }

        items = data.get("results")
        if items is None:
            nested = data.get("data")
            if isinstance(nested, dict):
                items = nested.get("web")
        if not isinstance(items, list):
            return {"success": False, "error": "WSP native search returned malformed results", "data": {"web": []}}

        rows: List[Dict[str, Any]] = []
        for i, item in enumerate(items[:count]):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "url": str(item.get("url") or ""),
                    "title": str(item.get("title") or ""),
                    "description": _capped_description(str(
                        item.get("snippet")
                        or item.get("description")
                        or item.get("content")
                        or ""
                    )),
                    "position": i + 1,
                }
            )
        return {
            "success": True,
            "data": {"web": rows},
            "metadata": {
                "backend": self.name,
                "provider": data.get("provider"),
                "requested_limit": limit,
                "effective_limit": count,
            },
        }

    def _failure_message(self, data: Dict[str, Any], generic: str) -> str:
        """Generic text, except a Hermes credential-scope error, which says what to do."""
        scope_type = getattr(self.plugin, "_SECRET_SCOPE_ERROR_TYPE", None)
        if scope_type and data.get("error_type") == scope_type:
            return str(data.get("error") or generic)
        return generic

    # -- extract ------------------------------------------------------------

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        url_list = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
        if not self._inprocess_ready():
            return [
                {
                    "url": url,
                    "error": "WSP in-process engine unavailable; no sidecar fallback",
                }
                for url in url_list
            ]

        # Keep policy refusals outside provider execution: even a timeout or
        # malformed engine response must not make these URLs rescuable.
        try:
            engine = self.plugin._load_search_module()
            checked = engine._extract._check_extract_urls(url_list, engine.load_config())
        except Exception:  # noqa: BLE001 — never dispatch without URL validation
            return [{"url": url, "error": "WSP native URL validation failed"} for url in url_list]
        accepted = [validated for _url, validated, error in checked if error is None]
        rows = iter(self._extract_accepted(accepted, kwargs) if accepted else [])
        return [
            _refused_entry(url) if error is not None else {**next(rows), "url": url}
            for url, _validated, error in checked
        ]

    def _extract_accepted(self, url_list: List[str], kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
        run = getattr(self.plugin, "_run_extract")
        output_format = kwargs.get("format") or "markdown"
        try:
            data = run(
                url_list,
                provider=self.extract_provider,
                output_format=output_format,
            )
        except Exception:  # noqa: BLE001
            logger.warning("WSP native extract failed")
            return [{"url": url, "error": "WSP native extraction failed"} for url in url_list]

        if not isinstance(data, dict):
            return [
                {"url": url, "error": "WSP native extract returned malformed payload"}
                for url in url_list
            ]

        if data.get("error") and not data.get("results"):
            if _URL_REFUSED_PREFIX in str(data["error"]):
                # Every URL failed the engine's pre-flight check; the joined error
                # cannot be mapped back to single URLs, so none may be rescued.
                return [_refused_entry(url) for url in url_list]
            message = self._failure_message(data, "WSP native extraction failed; check provider readiness and configuration")
            return [{"url": url, "error": message} for url in url_list]

        results = data.get("results")
        if not isinstance(results, list):
            return [{"url": url, "error": "WSP native extract returned no results"} for url in url_list]

        # Bind every returned row to an explicitly requested URL: exact, then
        # normalised (canonical), then the source URL the provider reports.
        # Never relabel unrelated content or silently drop requested failures;
        # exactly one result per requested URL, in request order.
        rows = [row for row in results if isinstance(row, dict) and isinstance(row.get("url"), str)]
        matched = _match_rows(url_list, rows)
        return [
            self._normalize_row(url, row) if row is not None
            else {"url": url, "error": "WSP native extract returned no matching result"}
            for url, row in zip(url_list, matched)
        ]

    def _normalize_row(self, url: str, row: Dict[str, Any]) -> Dict[str, Any]:
        # The result carries the requested URL: Hermes correlates by it, and a
        # redirect target or punycode form would not match what was asked.
        if row.get("error") and str(row["error"]).startswith(_URL_REFUSED_PREFIX):
            # Refused by WSP's URL guard on purpose: Hermes must not rescue it.
            return _refused_entry(url)
        content = row.get("content", row.get("markdown", ""))
        if not isinstance(content, str):
            if not row.get("error"):
                return {"url": url, "error": "WSP native extract returned malformed content"}
            # An error row legitimately has no content (``content: None``);
            # report its error, not a content-shape problem.
            content = ""
        normalized = {"url": url, "content": content}
        for key in ("title", "raw_content"):
            if isinstance(row.get(key), str):
                normalized[key] = row[key]
        metadata = {key: row[key] for key in (
            "fetcher", "status", "quality", "lang", "source_type", "page_type",
            "next_offset", "truncated", "original_content_length",
        ) if key in row}
        full_text = row.get("full_text")
        if isinstance(full_text, dict) and full_text.get("truncated"):
            # Content was cut to the context budget: say so, with the real
            # length when known, instead of passing it off as the whole page.
            metadata["truncated"] = True
            if isinstance(full_text.get("original_chars"), int):
                metadata["original_content_length"] = full_text["original_chars"]
            if full_text.get("stored") and full_text.get("path"):
                metadata["full_text_path"] = full_text["path"]
        if row.get("error"):
            # Reasons classified by the engine (not found, site refused, ...) are
            # WSP's own controlled text; anything else stays generic.
            normalized["error"] = (
                f"WSP extraction failed for this URL: {row['error']}"
                if row.get("error_reason")
                else "WSP extraction failed for this URL"
            )
        if metadata:
            normalized["metadata"] = metadata
        return normalized

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "opt-in · multi-provider",
            "tag": (
                "Routes Hermes web_search/web_extract through Web Search Plus "
                "in-process. Set web.search_backend / web.extract_backend to 'wsp'."
            ),
            "env_vars": [],
        }


def register_native_backend(ctx: Any, plugin: Any) -> Optional[Any]:
    """Register the wsp backend on *ctx* if the host supports it.

    Returns the registration handle (for dispose/unload) or None when the
    host PluginContext cannot register web providers.
    """
    register = getattr(ctx, "register_web_search_provider", None)
    if not callable(register):
        logger.debug(
            "web-search-plus: host lacks register_web_search_provider; "
            "skipping native wsp backend"
        )
        return None
    backend = WSPNativeBackend(plugin=plugin)
    try:
        handle = register(backend)
    except Exception:  # noqa: BLE001 — never break Plus tool registration
        logger.warning(
            "web-search-plus: failed to register native wsp backend"
        )
        return None
    if handle is not None:
        logger.info("web-search-plus: registered native wsp WebSearchProvider")
    return handle
