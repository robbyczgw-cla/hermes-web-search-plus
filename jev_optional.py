"""Optional Jev decisions. Default off. TypeSafe types never leak.

Code runs first. Jev is consulted only for gated optional seams,
and only when confidence meets the seam threshold. Missing key/SDK
falls back to unchanged WSP behaviour.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PINNED_MODEL = "jev-1.13.0"
DEFAULT_MIN_CONFIDENCE = 0.85
SEARCH_TYPE_MIN_CONFIDENCE = 0.95
_CHOOSER_OVERRIDE: Callable[..., Any] | None = None

_BLOCKED = re.compile(
    r"just a moment|attention required|cf-browser-verification|checking your browser|"
    r"enable javascript( and cookies)?|please (enable|turn on) javascript|"
    r"verify you are human|hcaptcha|recaptcha|g-recaptcha|access denied|"
    r"403 forbidden|error 1020|ray id\b|why have i been blocked",
    re.I,
)
_COOKIE = re.compile(
    r"we use cookies|accept (all )?cookies|cookie (policy|consent|banner)|"
    r"this site uses cookies|manage (cookie|consent) preferences",
    re.I,
)
_NAV = re.compile(
    r"skip to (main )?content|sign in\b|log in\b|main menu|nav(igation)?\b|"
    r"home\s+about\s+contact|subscribe to our newsletter",
    re.I,
)
_NEWS = re.compile(
    r"\b(news|breaking|keynote|wahl|election|bundesliga|ergebnisse|press briefing|"
    r"nächstes spiel|yesterday|gestern|tonight|live)\b",
    re.I,
)

EXTRACT_CRITERIA = {
    "usable_content": "Real page body that could inform the query.",
    "blocked_or_challenge": "Bot wall, CAPTCHA, Cloudflare interstitial, or access denied.",
    "navigation_or_boilerplate": "Mostly chrome, cookie banner, or nav.",
    "empty_or_broken": "Empty, JS placeholder, or unreadable garbage.",
    "uncertain": "Not enough evidence.",
}
LANGUAGE_CRITERIA = {
    "de": "Unambiguous German.",
    "en": "Unambiguous English.",
    "es": "Unambiguous Spanish.",
    "fr": "Unambiguous French.",
    "it": "Unambiguous Italian.",
    "pt": "Unambiguous Portuguese.",
    "nl": "Unambiguous Dutch.",
    "none": "Too short, jargon, mixed, or unsupported script. Prefer none.",
}
SEARCH_TYPE_CRITERIA = {
    "search": "Ordinary web lookup. Default when unsure.",
    "news": "The user wants current reporting, headlines, or a live event recap.",
}


@dataclass(frozen=True)
class JevSettings:
    enabled: bool = False
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    search_type_min_confidence: float = SEARCH_TYPE_MIN_CONFIDENCE
    timeout_s: float = 8.0
    search_type: bool = False
    extract_quality: bool = False
    language_fill: bool = False

    def decision_on(self, name: str) -> bool:
        return self.enabled and bool(getattr(self, name, False))


def settings_from_config(config: Optional[Dict[str, Any]]) -> JevSettings:
    raw = (config or {}).get("jev") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        return JevSettings()

    def _float(name: str, default: float) -> float:
        try:
            return float(raw.get(name, default))
        except (TypeError, ValueError):
            return default

    conf = min(1.0, max(0.0, _float("min_confidence", DEFAULT_MIN_CONFIDENCE)))
    st_conf = min(1.0, max(0.0, _float("search_type_min_confidence", SEARCH_TYPE_MIN_CONFIDENCE)))
    timeout = max(1.0, _float("timeout_s", 8.0))
    return JevSettings(
        enabled=bool(raw.get("enabled", False)),
        min_confidence=conf,
        search_type_min_confidence=st_conf,
        timeout_s=timeout,
        search_type=bool(raw.get("search_type", False)),
        extract_quality=bool(raw.get("extract_quality", False)),
        language_fill=bool(raw.get("language_fill", False)),
    )


def _key() -> str:
    env = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if env:
        return env
    path = os.environ.get("TYPESAFE_API_KEY_FILE", "").strip()
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


@dataclass
class _Decision:
    label: str
    confidence: float | None
    error: str | None = None


def keyword_vertical(query: str) -> str:
    """Cheap news detector. Shopping/local stay 'search' on the WSP wire."""
    return "news" if _NEWS.search(query or "") else "search"


def _heuristic_extract(content: str) -> str:
    text = (content or "").strip()
    if len(text) < 40:
        return "empty_or_broken"
    if _BLOCKED.search(text) and len(text) < 1800:
        return "blocked_or_challenge"
    if _COOKIE.search(text) and len(text) < 500:
        return "navigation_or_boilerplate"
    if _NAV.search(text) and len(text) < 400:
        return "navigation_or_boilerplate"
    return "usable_content"


def _choose(
    state: dict,
    instructions: str,
    criteria: dict[str, str],
    settings: JevSettings,
    chooser: Any | None,
) -> _Decision:
    fn = chooser or _CHOOSER_OVERRIDE
    if fn is None:
        key = _key()
        if not key:
            return _Decision("uncertain", None, "missing_api_key")
        try:
            from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
        except ImportError:
            return _Decision("uncertain", None, "missing_sdk")
        client = TypeSafeClient(
            api_key=key,
            model=PINNED_MODEL,
            retry=RetryPolicy(max_retries=0, timeout=settings.timeout_s),
            timeout=settings.timeout_s,
        )
        try:
            result = client.system_one(
                state=state,
                questions={"d": Choice(instructions=instructions, criteria=criteria)},
                model=PINNED_MODEL,
                timeout=settings.timeout_s,
            )
        except Exception as exc:
            return _Decision("uncertain", None, type(exc).__name__)
        ans = (getattr(result, "choices", None) or {}).get("d")
        if ans is None:
            return _Decision("uncertain", None, "missing_answer")
        label = str(ans.choice)
        conf = float(ans.confidence) if getattr(ans, "confidence", None) is not None else None
        if label not in criteria:
            return _Decision("uncertain", conf, "bad_label")
        return _Decision(label, conf)
    out = fn(state=state, instructions=instructions, criteria=criteria)
    if isinstance(out, _Decision):
        return out
    label, conf = out
    return _Decision(str(label), None if conf is None else float(conf))


def maybe_search_type(
    query: str,
    requested: Optional[str] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    chooser: Any | None = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Keyword proposes news; Jev confirms only at high confidence. Else search.

    Explicit caller search_type other than search/empty is never overridden.
    """
    requested_norm = str(requested or "search").strip().lower() or "search"
    if requested_norm not in {"search", "news"}:
        return requested, None
    if requested_norm == "news":
        return requested if requested is not None else "news", None
    settings = settings_from_config(config)
    if not settings.decision_on("search_type"):
        return requested, None
    proposed = keyword_vertical(query)
    if proposed != "news":
        return requested if requested is not None else "search", None
    gate = settings.search_type_min_confidence
    decision = _choose(
        {"query": query},
        "Choose the search vertical. Prefer search unless the user clearly wants news.",
        SEARCH_TYPE_CRITERIA,
        settings,
        chooser,
    )
    meta = {
        "backend": "jev",
        "keyword": proposed,
        "label": decision.label,
        "confidence": decision.confidence,
        "error": decision.error,
        "applied": "search",
    }
    if (
        not decision.error
        and decision.label == "news"
        and decision.confidence is not None
        and decision.confidence >= gate
    ):
        meta["applied"] = "news"
        return "news", meta
    return "search", meta


def extract_item_action(
    content: str,
    *,
    query: str = "",
    url: str = "",
    config: Optional[Dict[str, Any]] = None,
    chooser: Any | None = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return ('keep'|'reject', meta). Default is keep with no meta."""
    settings = settings_from_config(config)
    if not settings.decision_on("extract_quality"):
        return "keep", None
    heuristic = _heuristic_extract(content)
    text = (content or "").strip()
    if heuristic == "usable_content":
        return "keep", None
    if heuristic == "empty_or_broken":
        return "reject", {"backend": "regex", "label": heuristic}
    if len(text) < 400:
        return "reject", {"backend": "regex", "label": heuristic}
    decision = _choose(
        {"query": query, "url": url, "content": text[:2500]},
        "Classify extracted web text. Keywords inside a real article are not a block.",
        EXTRACT_CRITERIA,
        settings,
        chooser,
    )
    if decision.error or decision.confidence is None or decision.confidence < settings.min_confidence:
        return "reject", {
            "backend": "jev_fallback_regex",
            "heuristic": heuristic,
            "jev": decision.label,
            "confidence": decision.confidence,
            "error": decision.error,
        }
    if decision.label == "usable_content":
        return "keep", {
            "backend": "jev",
            "label": decision.label,
            "confidence": decision.confidence,
            "overrode": heuristic,
        }
    return "reject", {
        "backend": "jev",
        "label": decision.label,
        "confidence": decision.confidence,
    }


def filter_extract_results(
    results: List[Dict[str, Any]],
    *,
    query: str = "",
    config: Optional[Dict[str, Any]] = None,
    chooser: Any | None = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    settings = settings_from_config(config)
    if not settings.decision_on("extract_quality") or not results:
        return results, None
    kept: List[Dict[str, Any]] = []
    rejected = []
    for item in results:
        if item.get("error"):
            kept.append(item)
            continue
        content = str(item.get("content") or item.get("raw_content") or item.get("text") or "")
        action, meta = extract_item_action(
            content, query=query, url=str(item.get("url") or ""), config=config, chooser=chooser
        )
        if action == "keep":
            if meta:
                item = dict(item)
                item.setdefault("metadata", {})["jev_extract_quality"] = meta
            kept.append(item)
        else:
            rejected.append({"url": item.get("url"), **(meta or {})})
    if not rejected:
        return kept, None
    return kept, {"rejected": rejected, "kept": len(kept)}


def maybe_fill_language(
    query: str,
    inferred: Optional[str],
    *,
    config: Optional[Dict[str, Any]] = None,
    chooser: Any | None = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Only runs when WSP inference returned None."""
    settings = settings_from_config(config)
    if inferred or not settings.decision_on("language_fill"):
        return inferred, None
    if len((query or "").split()) < 2:
        return None, None
    decision = _choose(
        {"query": query},
        "Infer query language. Prefer none for jargon or mixed languages.",
        LANGUAGE_CRITERIA,
        settings,
        chooser,
    )
    if (
        decision.error
        or decision.label in {"none", "uncertain"}
        or decision.confidence is None
        or decision.confidence < settings.min_confidence
    ):
        return None, {
            "backend": "jev",
            "label": decision.label,
            "confidence": decision.confidence,
            "error": decision.error,
            "applied": False,
        }
    return decision.label, {
        "backend": "jev",
        "label": decision.label,
        "confidence": decision.confidence,
        "applied": True,
    }
