"""Config-first search locale resolution with query-aware language inference.

Providers with country/language request parameters used to receive hardcoded
us/en defaults from DEFAULT_CONFIG. Resolution is now centralized here:

- Country is config-first. Precedence: CLI flag > explicit provider config in
  config.json > explicit location hint in the query (curated city/country
  table) > ``defaults.locale.country`` > "us".
- Language is query-aware. Precedence: CLI flag > explicit provider config >
  ``defaults.locale.language``; the value "auto" enables conservative query
  language inference (see routing.infer_query_language) > "en".

Query language never implies a country: a German query may come from Austria
or Switzerland just as well as Germany, so only explicit location hints or
configuration move the region.
"""

import re
from typing import Any, Dict, Optional, Tuple

from routing import infer_query_language

FALLBACK_COUNTRY = "us"
FALLBACK_LANGUAGE = "en"

# defaults.locale.language value that enables query language inference.
AUTO_LANGUAGE = "auto"

# Merged-config keys that carry an explicit per-provider locale override.
# DEFAULT_CONFIG no longer ships these keys, so their presence in the merged
# config means the user set them in config.json — that explicit choice wins
# over query hints and global defaults. Providers without locale parameters
# (tavily, exa, linkup, parallel, perplexity, keenable, ...) are absent.
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


def resolve_locale(
    provider: str,
    config: Optional[Dict[str, Any]],
    query: Optional[str],
    cli_country: Optional[str] = None,
    cli_language: Optional[str] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Resolve ``(country, language, metadata)`` for a provider request.

    Precedence:
      country:  CLI flag > explicit provider config > location hint in query >
                ``defaults.locale.country`` > "us"
      language: CLI flag > explicit provider config >
                ``defaults.locale.language`` ("auto" enables conservative
                query inference) > "en"

    The metadata dict follows the freshness/search_type reporting pattern:
    ``{"country": ..., "language": ..., "source": {"country": "config|hint|cli|fallback",
    "language": "config|inferred|cli|fallback"}}``. Country codes are
    normalized to lowercase; providers that need uppercase (brave, firecrawl,
    querit, you) upper-case them in their own request builders.
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
    auto_language = isinstance(default_language, str) and _normalize(default_language) == AUTO_LANGUAGE
    if cli_language:
        language, language_source = _normalize(cli_language), "cli"
    elif language_key and section.get(language_key):
        language, language_source = _normalize(section[language_key]), "config"
    elif default_language and not auto_language:
        language, language_source = _normalize(default_language), "config"
    else:
        inferred = infer_query_language(query or "") if auto_language else None
        if inferred:
            language, language_source = inferred, "inferred"
        else:
            language, language_source = FALLBACK_LANGUAGE, "fallback"

    metadata = {
        "country": country,
        "language": language,
        "source": {"country": country_source, "language": language_source},
    }
    return country, language, metadata
