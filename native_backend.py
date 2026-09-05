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

from agent.web_search_provider import WebSearchProvider
from tools.web_tools import _load_web_config

logger = logging.getLogger(__name__)

# WSP engine hard-cap (matches web_search_plus schema maximum).
_WSP_MAX_RESULTS = 20

_NATIVE_BACKEND_NAME = "wsp"
_NATIVE_DISPLAY_NAME = "Web Search Plus (native)"


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
        except Exception:
            # Offline config inspection only; never probe vendor I/O here.
            return False
        return False

    def supports_search(self) -> bool:
        return self._selected("search")

    def supports_extract(self) -> bool:
        return self._selected("extract")

    def is_keyless_available(self) -> bool:
        # Never participate in the keyless free-tier walk.
        return False

    # -- in-process readiness (no sidecar) ----------------------------------

    def _inprocess_ready(self) -> bool:
        force = getattr(self.plugin, "_force_subprocess", None)
        if callable(force) and force():
            return False
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
                inprocess_only=True,
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
                "error": "WSP native search failed; check provider readiness and configuration",
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
                    "description": str(
                        item.get("snippet")
                        or item.get("description")
                        or item.get("content")
                        or ""
                    ),
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

        run = getattr(self.plugin, "_run_extract")
        output_format = kwargs.get("format") or "markdown"
        try:
            data = run(
                url_list,
                provider=self.extract_provider,
                output_format=output_format,
                inprocess_only=True,
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
            return [{"url": url, "error": "WSP native extraction failed; check provider readiness and configuration"} for url in url_list]

        results = data.get("results")
        if not isinstance(results, list):
            return [{"url": url, "error": "WSP native extract returned no results"} for url in url_list]

        # Bind every returned row to an explicitly requested URL. Never relabel
        # unrelated content or silently drop requested failures.
        by_url: Dict[str, Dict[str, Any]] = {}
        for row in results:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            url = row["url"]
            if url not in url_list or url in by_url:
                continue
            content = row.get("content", row.get("markdown", ""))
            if not isinstance(content, str):
                by_url[url] = {"url": url, "error": "WSP native extract returned malformed content"}
                continue
            normalized = {"url": url, "content": content}
            for key in ("title", "raw_content"):
                if isinstance(row.get(key), str):
                    normalized[key] = row[key]
            if row.get("error"):
                normalized["error"] = "WSP extraction failed for this URL"
            metadata = {key: row[key] for key in (
                "fetcher", "status", "quality", "lang", "source_type", "page_type",
                "next_offset", "truncated", "original_content_length",
            ) if key in row}
            if metadata:
                normalized["metadata"] = metadata
            by_url[url] = normalized
        return [by_url.get(url, {"url": url, "error": "WSP native extract returned no matching result"})
                for url in url_list]

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
