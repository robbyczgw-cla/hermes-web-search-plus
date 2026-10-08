"""Deterministic, dependency-free result-set diversity diagnostics.

The score deliberately describes a result set rather than judging any one
source.  It is safe to use for diagnostics without changing ordering; the
separate ``rerank_duplicate_candidates`` helper is used only by the explicit
research-mode opt-in.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# This is intentionally an approximation, not a bundled public-suffix list.
# It covers common multi-label registrations and the country suffixes used by
# our operators while retaining the conservative last-two-label fallback.
MULTI_LABEL_SUFFIXES = frozenset(
    {
        "ac.at",
        "ac.jp",
        "ac.nz",
        "ac.uk",
        "asn.au",
        "co.at",
        "co.in",
        "co.jp",
        "co.nz",
        "co.uk",
        "com.au",
        "com.br",
        "com.cn",
        "com.hk",
        "com.mx",
        "com.my",
        "com.sg",
        "com.tr",
        "edu.au",
        "edu.cn",
        "edu.hk",
        "edu.in",
        "edu.my",
        "edu.sg",
        "ed.jp",
        "firm.in",
        "gen.in",
        "go.jp",
        "gov.au",
        "gov.cn",
        "gov.hk",
        "gov.in",
        "gov.uk",
        "govt.nz",
        "gv.at",
        "id.au",
        "ind.in",
        "ltd.uk",
        "me.uk",
        "ne.jp",
        "net.au",
        "net.cn",
        "net.in",
        "net.nz",
        "or.at",
        "or.jp",
        "org.au",
        "org.cn",
        "org.hk",
        "org.in",
        "org.nz",
        "org.uk",
        "plc.uk",
        "priv.at",
        "sch.uk",
    }
)

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
        "vero_id",
        "yclid",
        "_ga",
    }
)

# The weights intentionally favour source diversity and exact URL uniqueness.
# Content and provider coverage still matter, but should not hide a result set
# monopolised by one registrable domain.
DOMAIN_DIVERSITY_WEIGHT = 0.40
URL_DUPLICATION_WEIGHT = 0.30
CONTENT_DIVERSITY_WEIGHT = 0.20
PROVIDER_MIX_WEIGHT = 0.10

DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.60


class DiversityComponents(TypedDict):
    domain_diversity: float
    url_duplication: float
    content_diversity: float
    provider_mix: float


class DuplicateCandidate(TypedDict):
    kind: str
    kept: int
    dropped_candidate: int


class DominantDomain(TypedDict):
    domain: str
    share: float


class DiversityReport(TypedDict):
    score: float
    components: DiversityComponents
    duplicates: List[DuplicateCandidate]
    dominant_domain: Optional[DominantDomain]


def _url_parts(value: object):
    """Parse a URL or host-like value without allowing parser errors to leak."""
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


def _normalized_host(parsed: Any) -> str:
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


def registrable_domain(url: str) -> str:
    """Return a conservative eTLD+1 approximation for a URL's host.

    IP addresses and single-label hosts are returned unchanged.  Unicode IDNs
    are normalised to their stdlib IDNA form so equivalent URL spellings group
    together deterministically.
    """
    parsed = _url_parts(url)
    if parsed is None:
        return ""
    host = _normalized_host(parsed)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def _is_tracking_parameter(name: str) -> bool:
    normalized = name.casefold()
    return normalized.startswith("utm_") or normalized in TRACKING_PARAMETER_NAMES


def canonical_url(url: str) -> str:
    """Canonicalise a URL for exact-duplicate comparison.

    Canonicalisation lowers and IDNA-normalises hosts, removes default ports,
    fragments and common tracking parameters, sorts retained query pairs, and
    treats a root trailing slash as equivalent to no path.  It deliberately
    avoids network access and does not attempt redirect or content canonicality.
    """
    parsed = _url_parts(url)
    if parsed is None:
        return ""
    host = _normalized_host(parsed)
    if not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.casefold()
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host_display = f"[{host}]" if ":" in host else host
        netloc = f"{host_display}:{port}"
    else:
        netloc = f"[{host}]" if ":" in host else host
    path = parsed.path.rstrip("/")
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return ""
    kept_pairs = sorted(
        (name, value) for name, value in query_pairs if not _is_tracking_parameter(name)
    )
    query = urlencode(kept_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _word_trigrams(text: object) -> set[Tuple[str, str, str]]:
    if not isinstance(text, str):
        return set()
    words = _WORD_RE.findall(text.casefold())
    if len(words) < 3:
        return set()
    return {tuple(words[index : index + 3]) for index in range(len(words) - 2)}


def snippet_similarity(a: str, b: str) -> float:
    """Return Jaccard similarity over casefolded, punctuation-free word trigrams.

    Text shorter than three words has no trigram evidence and therefore scores
    ``0.0`` rather than becoming an accidental near-duplicate.
    """
    shingles_a = _word_trigrams(a)
    shingles_b = _word_trigrams(b)
    if not shingles_a or not shingles_b:
        return 0.0
    return len(shingles_a & shingles_b) / len(shingles_a | shingles_b)


def _snippet_for(item: Mapping[str, Any]) -> str:
    value = item.get("snippet")
    if not isinstance(value, str) or not value.strip():
        value = item.get("description", "")
    return value if isinstance(value, str) else ""


def _validated_threshold(value: float) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return DEFAULT_NEAR_DUPLICATE_THRESHOLD
    if math.isnan(threshold) or threshold < 0.0 or threshold > 1.0:
        return DEFAULT_NEAR_DUPLICATE_THRESHOLD
    return threshold


def _duplicate_analysis(
    results: Sequence[Mapping[str, Any]], near_duplicate_threshold: float
) -> Tuple[List[DuplicateCandidate], int, int]:
    """Return candidate explanations plus URL and content duplication counts."""
    threshold = _validated_threshold(near_duplicate_threshold)
    canonical_seen: Dict[str, int] = {}
    url_kept_for_index: Dict[int, int] = {}
    url_duplicate_count = 0
    for index, item in enumerate(results):
        canonical = canonical_url(str(item.get("url") or ""))
        if not canonical:
            continue
        if canonical in canonical_seen:
            url_kept_for_index[index] = canonical_seen[canonical]
            url_duplicate_count += 1
        else:
            canonical_seen[canonical] = index

    snippets = [_snippet_for(item) for item in results]
    content_kept_for_index: Dict[int, int] = {}
    near_duplicate_pair_count = 0
    for dropped_candidate in range(len(results)):
        for kept in range(dropped_candidate):
            if snippet_similarity(snippets[kept], snippets[dropped_candidate]) >= threshold:
                near_duplicate_pair_count += 1
                content_kept_for_index.setdefault(dropped_candidate, kept)

    duplicates: List[DuplicateCandidate] = []
    for dropped_candidate in range(len(results)):
        if dropped_candidate in url_kept_for_index:
            duplicates.append(
                {
                    "kind": "url",
                    "kept": url_kept_for_index[dropped_candidate],
                    "dropped_candidate": dropped_candidate,
                }
            )
        if dropped_candidate in content_kept_for_index:
            duplicates.append(
                {
                    "kind": "content",
                    "kept": content_kept_for_index[dropped_candidate],
                    "dropped_candidate": dropped_candidate,
                }
            )
    return duplicates, url_duplicate_count, near_duplicate_pair_count


def score_diversity(
    results: Sequence[Mapping[str, Any]],
    *,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> DiversityReport:
    """Score result-set diversity with deterministic 0..1 components.

    The total is ``0.40 * domain + 0.30 * URL + 0.20 * content + 0.10 *
    provider``.  Provider mix uses normalised provider entropy only when more
    than one provider is represented; a single-provider result set receives
    ``1.0`` because provider variety is not meaningful there.
    """
    normalized_results = [item for item in results if isinstance(item, Mapping)]
    count = len(normalized_results)
    if not count:
        empty_components: DiversityComponents = {
            "domain_diversity": 0.0,
            "url_duplication": 0.0,
            "content_diversity": 0.0,
            "provider_mix": 0.0,
        }
        return {
            "score": 0.0,
            "components": empty_components,
            "duplicates": [],
            "dominant_domain": None,
        }

    domains = [registrable_domain(str(item.get("url") or "")) for item in normalized_results]
    domain_counts = Counter(domain for domain in domains if domain)
    domain_diversity = len(domain_counts) / count
    dominant_domain: Optional[DominantDomain] = None
    if domain_counts:
        domain, domain_count = sorted(
            domain_counts.items(), key=lambda row: (-row[1], row[0])
        )[0]
        dominant_domain = {"domain": domain, "share": round(domain_count / count, 4)}

    duplicates, url_duplicate_count, near_duplicate_pair_count = _duplicate_analysis(
        normalized_results, near_duplicate_threshold
    )
    url_duplication = 1.0 - (url_duplicate_count / count)
    pair_count = count * (count - 1) // 2
    content_diversity = (
        1.0 if pair_count == 0 else 1.0 - (near_duplicate_pair_count / pair_count)
    )

    providers = [
        str(item.get("provider")).strip()
        for item in normalized_results
        if str(item.get("provider") or "").strip()
    ]
    provider_counts = Counter(providers)
    if len(provider_counts) <= 1:
        provider_mix = 1.0
    else:
        entropy = -sum(
            (provider_count / len(providers))
            * math.log(provider_count / len(providers))
            for provider_count in provider_counts.values()
        )
        provider_mix = entropy / math.log(len(provider_counts))

    components: DiversityComponents = {
        "domain_diversity": round(domain_diversity, 4),
        "url_duplication": round(max(0.0, url_duplication), 4),
        "content_diversity": round(max(0.0, content_diversity), 4),
        "provider_mix": round(max(0.0, provider_mix), 4),
    }
    score = (
        DOMAIN_DIVERSITY_WEIGHT * components["domain_diversity"]
        + URL_DUPLICATION_WEIGHT * components["url_duplication"]
        + CONTENT_DIVERSITY_WEIGHT * components["content_diversity"]
        + PROVIDER_MIX_WEIGHT * components["provider_mix"]
    )
    return {
        "score": round(min(1.0, max(0.0, score)), 4),
        "components": components,
        "duplicates": duplicates,
        "dominant_domain": dominant_domain,
    }


def rerank_duplicate_candidates(
    results: Sequence[Mapping[str, Any]],
    *,
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> Tuple[List[Dict[str, Any]], List[DuplicateCandidate]]:
    """Stably move duplicate candidates behind the diverse result head.

    Results are never removed by this helper.  The earliest candidate remains
    in the head, and later URL/content duplicates retain their relative order
    in the tail.  The explanations use original input indices.
    """
    normalized_results = [dict(item) for item in results if isinstance(item, Mapping)]
    duplicates, _url_duplicate_count, _content_pair_count = _duplicate_analysis(
        normalized_results, near_duplicate_threshold
    )
    duplicate_indices = {entry["dropped_candidate"] for entry in duplicates}
    diverse_head = [
        item for index, item in enumerate(normalized_results) if index not in duplicate_indices
    ]
    duplicate_tail = [
        item for index, item in enumerate(normalized_results) if index in duplicate_indices
    ]
    return diverse_head + duplicate_tail, duplicates
