"""Config-first search locale resolution with query-aware language inference.

Providers with country/language request parameters used to receive hardcoded
us/en defaults from DEFAULT_CONFIG. Resolution is now centralized here:

- Country is config-first. Precedence: CLI flag > explicit provider config in
  config.json > explicit location hint in the query (curated city/country
  table) > ``defaults.locale.country`` > "us".
- Language is query-aware. Precedence: CLI flag > explicit provider config >
  ``defaults.locale.language`` > "en". The value "auto" (as a flag or as
  ``defaults.locale.language``) enables conservative query language detection
  (see routing.detect_query_language); when the detector is not confident no
  language is sent and the provider default applies.

Query language never implies a country: a German query may come from Austria
or Switzerland just as well as Germany, so only explicit location hints or
configuration move the region.
"""

import re
from typing import Any, Dict, Optional, Tuple

from .routing import detect_query_language

FALLBACK_COUNTRY = "us"
FALLBACK_LANGUAGE = "en"

# Language value (flag or defaults.locale.language) that enables query language
# detection. Not a language code: it is never sent to a provider.
AUTO_LANGUAGE = "auto"

# Merged-config keys that carry an explicit per-provider locale override.
# DEFAULT_CONFIG no longer ships these keys, so their presence in the merged
# config means the user set them in config.json — that explicit choice wins
# over query hints and global defaults. Providers without locale parameters
# (tavily, exa, linkup, parallel, keenable, ...) are absent.
PROVIDER_LOCALE_CONFIG_KEYS: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "serper": ("country", "language"),
    "serpbase": ("country", "language"),
    "brave": ("country", "search_lang"),
    "querit": ("country", "language"),
    "firecrawl": ("country", None),
    "you": ("country", "language"),
    "searxng": (None, "language"),
}

# Small curated table of unambiguous location hints. Only well-known city and
# country names are listed; generic example queries such as
# "mejores restaurantes Madrid" or "boulangerie Paris horaires" resolve to the
# matching country. Deliberately small: unknown places simply do not hint.
LOCATION_COUNTRY_HINTS: Dict[str, str] = {
    # Austria
    "wien": "at", "vienna": "at", "graz": "at", "salzburg": "at",
    "innsbruck": "at", "österreich": "at", "austria": "at",
    # Germany
    "berlin": "de", "münchen": "de", "munich": "de", "hamburg": "de",
    "frankfurt": "de", "deutschland": "de", "germany": "de",
    # Switzerland
    "zürich": "ch", "zurich": "ch", "schweiz": "ch", "switzerland": "ch",
    # France
    "paris": "fr", "lyon": "fr", "marseille": "fr", "france": "fr",
    # Spain
    "madrid": "es", "barcelona": "es", "españa": "es", "spain": "es",
    # Italy
    "rome": "it", "roma": "it", "milano": "it", "milan": "it", "italia": "it", "italy": "it",
    # Portugal
    "lisbon": "pt", "lisboa": "pt", "portugal": "pt",
    # Netherlands
    "amsterdam": "nl", "rotterdam": "nl", "netherlands": "nl",
    # United Kingdom
    "london": "gb", "manchester": "gb", "united kingdom": "gb",
    # United States
    "new york": "us", "chicago": "us", "san francisco": "us", "usa": "us",
}

_LOCATION_HINT_PATTERNS: Tuple[Tuple[Any, str], ...] = tuple(
    (re.compile(r"\b" + re.escape(place) + r"\b"), country)
    for place, country in LOCATION_COUNTRY_HINTS.items()
)


def provider_supports_locale(provider: str) -> bool:
    """Whether a provider's request carries country and/or language parameters."""
    return provider in PROVIDER_LOCALE_CONFIG_KEYS


def detect_location_country(query: Optional[str]) -> Optional[str]:
    """Return the ISO 3166-1 alpha-2 country for an explicit location hint.

    Only returns a country when every hint in the query agrees on a single
    country; conflicting hints (e.g. a "Paris vs Madrid" comparison) resolve
    to None so configuration keeps deciding.
    """
    if not query:
        return None
    lowered = query.lower()
    countries = {country for pattern, country in _LOCATION_HINT_PATTERNS if pattern.search(lowered)}
    if len(countries) == 1:
        return next(iter(countries))
    return None


def _normalize(value: Any) -> str:
    return str(value).strip().lower()


def is_auto_language(value: Any) -> bool:
    return isinstance(value, str) and _normalize(value) == AUTO_LANGUAGE


def apply_auto_language(
    language: Optional[str], config: Dict[str, Any]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Carry a per-call language "auto" as ``defaults.locale.language``.

    The v3 request contract has no value for "auto" (``locale.language`` is a
    two-letter code), so entry points hand it to the resolver through a copy of
    the config. Any other language passes through unchanged.
    """
    if not is_auto_language(language):
        return language, config
    defaults = config.get("defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    locale = defaults.get("locale")
    locale = locale if isinstance(locale, dict) else {}
    return None, {**config, "defaults": {**defaults, "locale": {**locale, "language": AUTO_LANGUAGE}}}


def resolve_locale(
    provider: str,
    config: Optional[Dict[str, Any]],
    query: Optional[str],
    cli_country: Optional[str] = None,
    cli_language: Optional[str] = None,
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """Resolve ``(country, language, metadata)`` for a provider request.

    Precedence:
      country:  CLI flag > explicit provider config > location hint in query >
                ``defaults.locale.country`` > "us"
      language: CLI flag > explicit provider config >
                ``defaults.locale.language`` > "en"
                With "auto" (flag or ``defaults.locale.language``) the
                detected query language replaces the configured default; an
                explicit provider config still wins. Without a confident
                detection the language is None and providers omit the
                parameter.

    The metadata dict follows the freshness/search_type reporting pattern:
    ``{"country": ..., "language": ..., "source": {"country": "config|hint|cli|fallback",
    "language": "config|inferred|jev|cli|provider_default|fallback"}}``. Country
    codes are normalized to lowercase; providers that need uppercase (brave,
    firecrawl, querit, you) upper-case them in their own request builders.
    """
    config = config if isinstance(config, dict) else {}
    country_key, language_key = PROVIDER_LOCALE_CONFIG_KEYS.get(provider, (None, None))
    section = config.get(provider)
    if not isinstance(section, dict):
        section = {}
    defaults = config.get("defaults")
    locale_defaults = defaults.get("locale") if isinstance(defaults, dict) else None
    if not isinstance(locale_defaults, dict):
        locale_defaults = {}

    if cli_country:
        country, country_source = _normalize(cli_country), "cli"
    elif country_key and section.get(country_key):
        country, country_source = _normalize(section[country_key]), "config"
    else:
        hinted = detect_location_country(query)
        default_country = locale_defaults.get("country")
        if hinted:
            country, country_source = hinted, "hint"
        elif default_country:
            country, country_source = _normalize(default_country), "config"
        else:
            country, country_source = FALLBACK_COUNTRY, "fallback"

    default_language = locale_defaults.get("language")
    cli_auto = is_auto_language(cli_language)
    auto_language = cli_auto or is_auto_language(default_language)
    language: Optional[str]
    jev_lang_meta = None
    if cli_language and not cli_auto:
        language, language_source = _normalize(cli_language), "cli"
    elif language_key and section.get(language_key):
        language, language_source = _normalize(section[language_key]), "config"
    elif default_language and not auto_language:
        language, language_source = _normalize(default_language), "config"
    elif not auto_language:
        language, language_source = FALLBACK_LANGUAGE, "fallback"
    else:
        language, language_source = detect_query_language(query or "").inferred, "inferred"
        if not language:
            from .jev_optional import maybe_fill_language

            language, jev_lang_meta = maybe_fill_language(query or "", language, config=config)
            language_source = "jev" if language else "provider_default"

    metadata = {
        "country": country,
        "language": language,
        "source": {"country": country_source, "language": language_source},
    }
    if jev_lang_meta:
        metadata["jev_language"] = jev_lang_meta
    return country, language, metadata
