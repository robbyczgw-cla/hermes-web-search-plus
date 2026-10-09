"""The one URL normalisation used for every comparison WSP makes.

Result dedup, diversity scoring, research fusion and the v3 observation
``url.canonical`` value all compare URLs. Before 5.0 each had its own rules
(scheme, ``www.``, trailing slash, tracking parameters, ports were handled
differently in six places). They now share these functions:

- ``canonical_url`` - a normalised URL with scheme, for display and the v3
  ``canonical`` value;
- ``url_key`` - the same without scheme, the identity used for dedup and
  fusion (``http`` and ``https`` copies of a page are one page);
- ``strip_tracking_params`` - remove tracking parameters but keep the URL as
  the user should see and open it;
- ``host_and_path`` - for path-prefix rules such as ``github.com/org``;
- ``domain_filters`` - the ``include_domains`` / ``exclude_domains`` of a
  search as bare hostnames that are safe to put after ``site:``.

No network access: redirects and content-level canonical links are out of
scope.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, List, Optional, Tuple
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMETER_NAMES = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "oly_anon_id",
        "oly_enc_id",
        "ref",
        "srsltid",
        "vero_id",
        "yclid",
        "_ga",
    }
)

# Host prefixes that serve the same page as the bare host. Only stripped when
# at least two labels remain, so "m.com" stays "m.com".
_ALIAS_HOST_PREFIXES = ("www.", "m.", "amp.")
# AMP renderings of a page: a trailing "/amp" path segment, a leading "/amp/"
# segment, or an amp query marker.
_AMP_PATH_SUFFIX = re.compile(r"/amp/?$", re.IGNORECASE)
_AMP_PATH_PREFIX = re.compile(r"^/amp(?=/)", re.IGNORECASE)


def url_parts(value: object) -> Optional[SplitResult]:
    """Parse a URL or host-like value without letting parser errors escape."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if any(character.isspace() for character in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
    except (TypeError, ValueError):
        return None
    if not parsed.netloc and not parsed.scheme:
        try:
            parsed = urlsplit("//" + candidate)
        except (TypeError, ValueError):
            return None
    return parsed


def normalized_host(parsed: Any) -> str:
    """Lower-cased, IDNA-encoded host (IP addresses unchanged)."""
    try:
        host = parsed.hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".").casefold()
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host


def is_tracking_parameter(name: str) -> bool:
    normalized = name.casefold()
    return normalized.startswith("utm_") or normalized in TRACKING_PARAMETER_NAMES


def _is_amp_marker(name: str, value: str) -> bool:
    lowered = name.casefold()
    return lowered == "amp" or (lowered == "outputtype" and value.casefold() == "amp")


def _identity_host(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    stripped = True
    while stripped:  # "www.m.example.com" -> "example.com"
        stripped = False
        for prefix in _ALIAS_HOST_PREFIXES:
            if host.startswith(prefix) and host.count(".") >= 2:
                host, stripped = host[len(prefix):], True
    return host


def canonical_url(url: object) -> str:
    """Normalised URL for comparison: one spelling per page, scheme kept.

    Lower-cases scheme and host, IDNA-encodes the host, drops ``www.``, ``m.``
    and ``amp.`` host aliases, default ports, the fragment, a trailing slash,
    AMP path segments and query markers, and tracking parameters (``utm_*``,
    ``gclid``, ``srsltid``, ...). Remaining query pairs, which often identify
    the page (``watch?v=``), are kept and sorted. Returns "" for invalid input.
    """
    parsed = url_parts(url)
    if parsed is None:
        return ""
    host = normalized_host(parsed)
    if not host:
        return ""
    host = _identity_host(host)
    try:
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    display = f"[{host}]" if ":" in host else host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{display}:{port}"
    else:
        netloc = display
    path = _AMP_PATH_PREFIX.sub("", _AMP_PATH_SUFFIX.sub("", parsed.path)).rstrip("/")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return ""
    kept = sorted(
        (name, value) for name, value in pairs
        if not is_tracking_parameter(name) and not _is_amp_marker(name, value)
    )
    return urlunsplit((scheme, netloc, path, urlencode(kept, doseq=True), ""))


def url_key(url: object) -> str:
    """Identity of a page across scheme and presentation variants ("" if invalid)."""
    canonical = canonical_url(url)
    return canonical.split("://", 1)[1] if "://" in canonical else canonical.lstrip("/")


def strip_tracking_params(url: str) -> str:
    """Remove tracking parameters only; the URL otherwise stays as given."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return url
    kept = [(name, value) for name, value in pairs if not is_tracking_parameter(name)]
    return urlunsplit(parsed._replace(query=urlencode(kept, doseq=True)))


def host_and_path(url: str) -> str:
    """Identity host plus path without trailing slash, for path-prefix rules."""
    parsed = url_parts(url)
    if parsed is None:
        return ""
    host = normalized_host(parsed)
    if not host:
        return ""
    return f"{_identity_host(host)}{parsed.path.rstrip('/')}"


# Domain filters. Brave, Serper, SerpBase, You.com and Firecrawl take them as
# ``site:`` / ``-site:`` operators inside the query text, so an entry must be a
# bare hostname and nothing else: anything that could carry a second operator
# ("site:evil.com", "--help") is rejected here, not escaped later.
SITE_OPERATOR_LIMIT = 10  # operators per list
_FILTER_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)
# A suffix filter such as ``.gov`` or ``*.ac.uk``: the labels after the dot.
_FILTER_SUFFIX = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)
_FILTER_SEPARATORS = re.compile(r"[\s,]+")


def domain_filter_tokens(value: Any) -> List[str]:
    """The entries of a domain filter: a string or a list, split on commas and spaces.

    An LLM passes ``"docs.rs"``, ``"docs.rs, tokio.rs"`` or ``["docs.rs"]`` for
    the same thing. Blank entries are dropped; nothing else is judged here.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    tokens: List[str] = []
    for item in items:
        if item is not None:
            tokens.extend(token for token in _FILTER_SEPARATORS.split(str(item)) if token)
    return tokens


def domain_filter_host(entry: str) -> Optional[str]:
    """One domain-filter entry as a bare ASCII hostname, or None if it is not a domain.

    A URL is reduced to its host (scheme, userinfo, port and path dropped),
    ``www.`` and a trailing dot are removed and an internationalised name is
    converted to punycode. Only ``label.label.tld`` made of ``[a-z0-9-]`` is
    accepted, so the result is always safe to put after ``site:``. An entry
    written as a suffix (``.gov``, ``*.ac.uk``) stays a suffix: ``gov``,
    ``ac.uk``, which ``site:`` also takes.
    """
    candidate = entry.strip()
    if candidate.startswith((".", "*.")):
        return _filter_suffix(candidate.removeprefix("*").removeprefix("."))
    try:
        host = urlsplit(candidate if "://" in candidate else "//" + candidate).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.removesuffix(".")
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host if _FILTER_HOST.fullmatch(host) else None


def _filter_suffix(suffix: str) -> Optional[str]:
    try:
        suffix = suffix.lower().encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return suffix if _FILTER_SUFFIX.fullmatch(suffix) else None


def _hosts(tokens: List[str]) -> List[str]:
    hosts: List[str] = []
    for token in tokens:
        host = domain_filter_host(token)
        if host is not None and host not in hosts:
            hosts.append(host)
    return hosts


def domain_filters(include: Any, exclude: Any) -> Tuple[List[str], List[str]]:
    """Validated ``(include, exclude)`` hostnames for one search.

    Each argument may be a string (comma or space separated) or a list. An
    unusable exclude entry is skipped. A host named in both lists is excluded.
    Raises ``ValueError`` (the message is meant for the caller) when include
    entries were given but none is left: searching unrestricted would answer a
    different question than the one asked.
    """
    include_tokens = domain_filter_tokens(include)
    include_hosts = _hosts(include_tokens)
    exclude_hosts = _hosts(domain_filter_tokens(exclude))
    if include_tokens and not include_hosts:
        shown = ", ".join(repr(token[:60]) for token in include_tokens[:3])
        raise ValueError(
            "Invalid include_domains value: {}{}. Entries must be hostnames such as docs.rs "
            "(a list, or one string separated by commas or spaces).".format(
                shown, ", ..." if len(include_tokens) > 3 else ""
            )
        )
    allowed = [host for host in include_hosts if host not in exclude_hosts]
    if include_hosts and not allowed:
        raise ValueError(
            "Invalid include_domains value: every domain is also in exclude_domains, "
            "which takes precedence, so no domain is left to search."
        )
    return allowed, exclude_hosts
