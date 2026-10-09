"""Automatic provider routing for Web Search Plus.

The first provider for a query comes from its intent (``intents.classify_intent``)
and a small measured table; the fallback chain after it follows
``auto_routing.provider_priority``.
"""

import re
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .config import (
    DEFAULT_CONFIG,
    apply_profile_effects,
    get_api_key,
    keyless_public_allowed,
    self_hosted_profile_error,
)
from .intents import classify_intent
from .provider_registry import DEFAULT_AUTO_ALLOW, DEFAULT_PROVIDER_PRIORITY, PROVIDER_SPECS


def provider_configured(provider: str, config: Dict[str, Any] | None = None) -> bool:
    """Whether a provider can run: keyed via this module's (sync-patchable)
    ``get_api_key`` binding, or keyless with its public endpoint opted in."""
    if get_api_key(provider, config):
        return True
    return keyless_public_allowed(provider, config)


ROUTING_POLICY = "routing-v3"


def iter_all_selectable_provider_modes() -> Tuple[str, ...]:
    """Enumerate every search mode that routing/fallback is allowed to select."""
    return tuple(
        provider
        for provider, spec in PROVIDER_SPECS.items()
        if spec.supports_search
        and not spec.rejected_reason
        and spec.search_output_semantics == "source_results"
    )


# ---------------------------------------------------------------------------
# Query-language detection (one detector for routing and locale resolution)
# ---------------------------------------------------------------------------
# detect_query_language serves two callers from the same evidence. The hint is
# reported in routing metadata ("en" when nothing matches). The inference used
# by search_locale.resolve_locale counts distinct stopword/character signals per
# language and only reports a language when the evidence is unambiguous. Short
# keyword or technical queries (for example "PostgreSQL 17 release notes")
# produce no inference on purpose.

# Minimum number of distinct signals before a language inference is trusted.
LANGUAGE_INFERENCE_MIN_MATCHES = 2

# Common function/search words per supported language. Words shared between
# languages (e.g. "que" in es/fr/pt) may appear in several sets; the strict
# single-winner rule in detect_query_language keeps those from mis-firing.
LANGUAGE_INFERENCE_STOPWORDS: Dict[str, frozenset] = {
    "en": frozenset({
        "the", "and", "what", "how", "where", "when", "which", "who",
        "best", "near", "hours", "open", "with", "from", "for", "are",
        "is", "was", "does", "latest", "today", "new",
    }),
    "de": frozenset({
        "der", "die", "das", "und", "oder", "nicht", "ist", "sind",
        "ein", "eine", "einen", "mit", "für", "von", "wie", "wo", "was",
        "warum", "welche", "beste", "besten", "gibt", "öffnungszeiten",
        "heute", "morgen", "preis", "kaufen", "günstig", "nähe",
    }),
    "es": frozenset({
        "el", "los", "las", "una", "unos", "que", "qué", "cómo", "dónde",
        "cuál", "por", "para", "con", "mejores", "mejor", "cerca", "hoy",
        "horario", "horarios", "abierto", "abiertos", "tiendas",
        "restaurantes", "precio", "precios", "donde", "como",
    }),
    "fr": frozenset({
        "le", "les", "des", "une", "du", "où", "quel", "quelle", "quels",
        "quelles", "meilleur", "meilleure", "meilleurs", "meilleures",
        "horaires", "ouvert", "ouverts", "ouverture", "aujourd", "hui",
        "près", "proche", "avec", "pour", "prix", "cher", "que",
    }),
    "it": frozenset({
        "il", "lo", "gli", "che", "come", "dove", "quale", "quali",
        "migliori", "migliore", "orari", "orario", "aperto", "aperti",
        "vicino", "con", "oggi", "prezzo", "prezzi", "negozi",
        "ristoranti", "della", "delle",
    }),
    "pt": frozenset({
        "os", "do", "dos", "das", "um", "uma", "que", "como", "onde",
        "qual", "quais", "melhores", "melhor", "horários", "aberto",
        "perto", "hoje", "preço", "lojas", "com", "você", "para",
        "restaurantes",
    }),
    "nl": frozenset({
        "het", "een", "waar", "hoe", "welke", "beste", "goedkoop",
        "goedkoopste", "vandaag", "morgen", "openingstijden", "winkel",
        "winkels", "dichtbij", "buurt", "naar", "zijn", "niet", "voor",
    }),
}

# Distinctive characters that count as one additional signal per language.
LANGUAGE_INFERENCE_CHAR_HINTS: Dict[str, str] = {
    "de": "äöüß",
    "es": "ñ¿¡",
    "pt": "ãõ",
    "fr": "œ",
}


# Scripts behind the routing hint, checked in this order before any Latin words.
_ARABIC_SCRIPT = re.compile(r'[\u0600-\u06ff]')
_CYRILLIC_SCRIPT = re.compile(r'[\u0400-\u04ff]')
_KANA_SCRIPT = re.compile(r'[\u3040-\u30ff]')
_JAPANESE_WORDS = re.compile(r'(東京|ニュース|今日|企業|発表)')
_HAN_SCRIPT = re.compile(r'[\u4e00-\u9fff]')
# Letters outside the base Russian / Arabic alphabets (Ukrainian, Persian,
# Urdu, ...): the script alone then no longer identifies "ru" or "ar".
_NON_RUSSIAN_CYRILLIC = re.compile(r'[\u0400\u0402-\u040f\u0450\u0452-\u04ff]')
_NON_BASE_ARABIC = re.compile(r'[\u0671-\u06d3\u06f0-\u06f9]')

# Single trigger words for the routing hint, first match wins (not used for
# inference: "france" or "die" appear in English queries too).
_ROUTING_HINT_TRIGGERS: Tuple[Tuple[str, Any], ...] = (
    ("es", re.compile(r'\b(noticias|españa|hoy|regulación|inteligencia artificial)\b')),
    ("fr", re.compile(r'\b(actualités|france|aujourd|ouverts?|dimanche|récents?|avis)\b')),
    ("de", re.compile(r'\b(der|die|das|und|oder|nicht|ist|sind|aktuelle?n?|preis|kaufen|öffnungszeiten|österreich)\b')),
)


class QueryLanguage(NamedTuple):
    """Result of detect_query_language.

    ``hint`` always carries a code ("en" when nothing matches) and feeds
    routing. ``inferred`` is None unless the evidence is unambiguous and feeds
    locale resolution.
    """

    hint: str
    inferred: Optional[str]


# Words that alone identify a language: none is an English word, and none is
# shared by two of the listed languages. They only count when the query has
# no English stopword and no other language matched, so "Bundestag
# Abstimmung heute Ergebnis" is German while "Avis car rental" stays unknown.
LANGUAGE_STRONG_WORDS: Dict[str, frozenset] = {
    "de": frozenset({
        "heute", "öffnungszeiten", "warum", "welche", "günstig", "nähe",
        "erfahrungen", "aktuelle", "aktuell", "neueste", "unterschied", "wie",
        "wetter", "rezept", "vergleich", "empfehlung", "meinungen", "warnung",
        "schwachstelle", "sicherheitslücke",
    }),
    "fr": frozenset({
        "aujourd", "horaires", "près", "dernières", "météo", "demain",
        "vulnérabilité", "meilleur", "étude",
    }),
    "es": frozenset({"dónde", "cómo", "qué", "horarios", "últimas", "noticias", "opiniones", "revisión"}),
    "it": frozenset({"oggi", "orari", "notizie", "migliore", "opinioni"}),
    "pt": frozenset({"notícias", "hoje", "você"}),
    "nl": frozenset({"vandaag", "openingstijden", "werkt"}),
}


def _strong_word_language(words: set) -> Optional[str]:
    if words & LANGUAGE_INFERENCE_STOPWORDS["en"]:
        return None
    matches = {language for language, strong in LANGUAGE_STRONG_WORDS.items() if words & strong}
    return matches.pop() if len(matches) == 1 else None


def _stopword_language(lowered: str) -> Optional[str]:
    """Latin-script inference: stopword and character signals, single winner."""
    words = set(re.findall(r"\w+", lowered))
    counts: Dict[str, int] = {}
    for language, stopwords in LANGUAGE_INFERENCE_STOPWORDS.items():
        count = len(words & stopwords)
        count += sum(1 for char in LANGUAGE_INFERENCE_CHAR_HINTS.get(language, "") if char in lowered)
        if count:
            counts[language] = count
    if not counts:
        return _strong_word_language(words)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    best_language, best_count = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == best_count:
        return None
    if best_count < LANGUAGE_INFERENCE_MIN_MATCHES:
        strong = _strong_word_language(words)
        return strong if strong == best_language else None
    return best_language


def _script_language(text: str) -> Tuple[Optional[str], bool]:
    """(routing hint, confident) for non-Latin scripts; (None, False) otherwise.

    Kana only occurs in Japanese. Han-only text (zh or ja) and the Japanese
    word list stay hint-only, as do Cyrillic/Arabic text with letters outside
    the Russian/Arabic alphabets.
    """
    if _ARABIC_SCRIPT.search(text):
        return "ar", not _NON_BASE_ARABIC.search(text)
    if _CYRILLIC_SCRIPT.search(text):
        return "ru", not _NON_RUSSIAN_CYRILLIC.search(text)
    if _KANA_SCRIPT.search(text):
        return "ja", True
    if _JAPANESE_WORDS.search(text):
        return "ja", False
    if _HAN_SCRIPT.search(text):
        return "zh", False
    return None, False


def detect_query_language(query: Optional[str]) -> QueryLanguage:
    """Detect the query language for routing (``hint``) and locale (``inferred``).

    ``inferred`` is an ISO 639-1 code only when the evidence is unambiguous:
    either a script that identifies the language, or at least
    LANGUAGE_INFERENCE_MIN_MATCHES distinct stopword/character signals for one
    language that strictly beats every other candidate. Conflicting evidence
    (for example kana inside an English sentence) infers nothing. For example
    "Wiener Kaffeehaus Öffnungszeiten" infers "de", while a terse technical
    query such as "DAC R2R NOS" infers nothing and callers fall back to their
    configured default.
    """
    text = query or ""
    lowered = text.lower()
    script_hint, script_confident = _script_language(text)
    candidates = {_stopword_language(lowered)}
    if script_confident:
        candidates.add(script_hint)
    candidates.discard(None)
    inferred = candidates.pop() if len(candidates) == 1 else None
    if script_hint:
        return QueryLanguage(script_hint, inferred)
    hint = next((lang for lang, pattern in _ROUTING_HINT_TRIGGERS if pattern.search(lowered)), "en")
    return QueryLanguage(hint, inferred)


def infer_query_language(query: Optional[str]) -> Optional[str]:
    """The unambiguous language of ``detect_query_language``, else None."""
    return detect_query_language(query).inferred


# ---------------------------------------------------------------------------
# Routing: query intent -> first provider
# ---------------------------------------------------------------------------
# Measured on the recorded evaluation set: Brave gave
# the best results overall (nDCG@5 0.696; Serper 0.642, Exa 0.638, Tavily
# 0.552), Exa led on academic and documentation queries and Serper on shopping,
# in every leave-one-out fold. Security goes to Serper after a live A/B of the
# 26 security queries with authority domains: authority hit@5 22/26 on v4.3.5,
# 16/26 with Brave first, 23/26 with Serper first, at fewer output tokens.
# Community stays on Brave: on all 36 community queries Brave, Serper and
# Firecrawl each hit the authority domain 15/15, with Brave at p50 640 ms /
# p90 733 ms against Serper's 1067 / 1686 ms.
INTENT_FIRST_PROVIDER: Dict[str, str] = {
    "academic": "exa",
    "docs": "exa",
    "security": "serper",
    "shopping": "serper",
}
MEASURED_PROVIDER_ORDER: Tuple[str, ...] = ("brave", "serper", "exa", "tavily")

_RECENCY_PATTERNS: Tuple[Tuple[Any, float], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), weight)
    for pattern, weight in (
        (r"\b(latest|newest|recent|current)\b", 2.5),
        (r"\b(today|yesterday|this week|this month)\b", 3.0),
        (r"\b(breaking|live|just|now)\b", 3.0),
        (r"\blast (hour|day|week|month)\b", 2.5),
        (r"\b(hoy|aujourd|heute|aktuell)\b", 2.5),
        (r"[今日最新]", 2.5),
        (r"(сегодня|новости)", 2.5),
        (r"(اليوم|أخبار)", 2.5),
        (r"(最新|今天)", 2.5),
    )
)
_URL_IN_QUERY = re.compile(r"https?://\S+|\b[\w-]+\.(?:com|org|net|io|dev|ai|de|at)/\S*", re.IGNORECASE)


def detect_recency(query: str, *, year: Optional[int] = None) -> Tuple[bool, float]:
    """(wants recent information, score). The current year counts as a signal."""
    text = query or ""
    total = sum(weight for pattern, weight in _RECENCY_PATTERNS if pattern.search(text))
    current = year if year is not None else time.gmtime().tm_year
    if re.search(rf"\b({current}|{current + 1})\b", text):
        total += 2.0
    return total > 2.0, total


def _auto_eligible(provider: str, config: Dict[str, Any], auto_config: Dict[str, Any], disabled: set) -> bool:
    return (
        provider not in disabled
        and _provider_auto_allowed(provider, auto_config)
        and provider_configured(provider, config)
    )


def route_query(query: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """First provider for ``query`` among the configured, auto-allowed ones."""
    auto_config = config.get("auto_routing", DEFAULT_CONFIG["auto_routing"])
    disabled = set(auto_config.get("disabled_providers", []))
    intent = classify_intent(query)
    priority = list(auto_config.get("provider_priority", list(DEFAULT_PROVIDER_PRIORITY)))
    custom_order = auto_config.get("order") == "custom"
    if custom_order:
        # The user's own order: provider_priority decides, no per-intent rule.
        preferred = list(priority)
    else:
        preferred = [INTENT_FIRST_PROVIDER.get(intent.intent), *MEASURED_PROVIDER_ORDER, *priority]
    first = next(
        (p for p in preferred if p and _auto_eligible(p, config, auto_config, disabled)), None
    )
    # The order an automatic search tries providers in: the first provider, then
    # the fallback chain in provider_priority order (search._plan_search_v3).
    order: List[str] = [first] if first else []
    for provider in priority:
        if provider not in order and _auto_eligible(provider, config, auto_config, disabled):
            order.append(provider)
    auto_excluded = [
        provider for provider in PROVIDER_SPECS
        if provider not in disabled
        and not _provider_auto_allowed(provider, auto_config)
        and provider_configured(provider, config)
    ]
    recency_focused, _ = detect_recency(query)
    analysis_summary = {
        "query_length": len(query.split()),
        "has_url": bool(_URL_IN_QUERY.search(query)),
        "recency_focused": recency_focused,
        "language_hint": detect_query_language(query).hint,
        "routing_class": intent.intent,
        "intent_signals": list(intent.signals),
    }
    confidence = round(intent.confidence, 3)
    routing = {
        "confidence": confidence,
        "confidence_level": "high" if confidence >= 0.7 else "medium" if confidence >= 0.4 else "low",
        "reason": "custom_order" if custom_order else (f"intent_{intent.intent}" if intent.signals else "no_signals_matched"),
        "routing_policy": ROUTING_POLICY,
        "exa_depth": "normal",
        "scores": {},
        "top_signals": [{"matched": signal, "weight": 1.0} for signal in intent.signals[:5]],
        "candidate_order": order,
        "auto_allow_excluded": auto_excluded,
        "analysis_summary": analysis_summary,
    }
    if not order:
        return {
            **routing,
            "provider": auto_config.get("fallback_provider", "serper"),
            "confidence": 0.0,
            "confidence_level": "low",
            "reason": "no_available_providers",
        }
    return {**routing, "provider": order[0]}


def auto_route_provider(query: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Route a query to its first provider; returns the routing metadata."""
    config = apply_profile_effects(config)
    profile_error = self_hosted_profile_error(config)
    if profile_error is not None:
        return {
            "provider": None,
            "confidence": 0.0,
            "confidence_level": "low",
            "reason": profile_error.error_type,
            "error": str(profile_error),
            "error_type": profile_error.error_type,
            "scores": {},
            "top_signals": [],
            "auto_routed": True,
        }
    auto_config = config.get("auto_routing", DEFAULT_CONFIG["auto_routing"])
    if auto_config.get("enabled", True) is False:
        default_provider = config.get("default_provider")
        if default_provider:
            return {
                "provider": default_provider,
                "confidence": 1.0,
                "confidence_level": "high",
                "reason": "auto_routing_disabled_default_provider",
                "scores": {default_provider: 1.0},
                "top_signals": [],
                "auto_routed": False,
            }
        return {
            "provider": None,
            "confidence": 0.0,
            "confidence_level": "low",
            "reason": "auto_routing_disabled_no_default_provider",
            "scores": {},
            "top_signals": [],
            "auto_routed": False,
        }
    return route_query(query, config)


def explain_routing(query: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Routing decision with the intent evidence behind it, for --explain-routing."""
    config = apply_profile_effects(config)
    routing = auto_route_provider(query, config)
    intent = classify_intent(query)
    auto_config = config.get("auto_routing", {})
    return {
        "query": query,
        "routing_decision": {
            "provider": routing.get("provider"),
            "confidence": routing.get("confidence"),
            "confidence_level": routing.get("confidence_level"),
            "reason": routing.get("reason"),
            "routing_policy": routing.get("routing_policy", ROUTING_POLICY),
            "candidate_order": routing.get("candidate_order", []),
            "auto_allow_excluded": routing.get("auto_allow_excluded", []),
        },
        "intent": {
            "intent": intent.intent,
            "confidence": round(intent.confidence, 3),
            "signals": list(intent.signals),
        },
        "first_provider_rules": dict(INTENT_FIRST_PROVIDER),
        "measured_order": list(MEASURED_PROVIDER_ORDER),
        "query_analysis": routing.get("analysis_summary", {}),
        "available_providers": [
            provider
            for provider, spec in PROVIDER_SPECS.items()
            if spec.supports_search
            and provider_configured(provider, config)
            and provider not in auto_config.get("disabled_providers", [])
            and _provider_auto_allowed(provider, auto_config)
        ],
    }


def _provider_auto_allowed(provider: str, auto_config: Dict[str, Any]) -> bool:
    """Return whether a configured provider may be selected by auto-routing/fallback.

    Explicit provider calls still work; this gate only prevents low-trust or
    experimental providers from receiving user queries automatically.
    """
    auto_allow = auto_config.get("auto_allow", {}) if isinstance(auto_config, dict) else {}
    default_allowed = bool(DEFAULT_AUTO_ALLOW.get(provider, True))
    if not isinstance(auto_allow, dict):
        return default_allowed
    return bool(auto_allow.get(provider, default_allowed))
