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
- ``host_and_path`` - for path-prefix rules such as ``github.com/org``.

No network access: redirects and content-level canonical links are out of
scope.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Optional
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
