"""Result normalization, deduplication, reranking, and quality-report helpers."""

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .diversity_v3 import DEFAULT_NEAR_DUPLICATE_THRESHOLD, score_diversity
from .routing import ROUTING_POLICY
from .urls import host_and_path, url_key




def _title_from_url(url: str) -> str:
    """Derive a readable title from a URL when none is provided."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        # Use last meaningful path segment as context
        segments = [s for s in parsed.path.strip("/").split("/") if s]
        if segments:
            last = segments[-1].replace("-", " ").replace("_", " ")
            # Strip file extensions
            last = re.sub(r'\.\w{2,4}$', '', last)
            if last:
                return f"{domain} — {last[:80]}"
        return domain
    except Exception:
        return url[:60]


def normalize_result_url(url: str) -> str:
    """Dedup key for a result URL (see urls.url_key)."""
    return url_key(url)


def deduplicate_results_across_providers(results_by_provider: List[Tuple[str, Dict[str, Any]]], max_results: int) -> Tuple[List[Dict[str, Any]], int]:
    deduped = []
    seen = set()
    dedup_count = 0
    for provider_name, data in results_by_provider:
        for item in data.get("results", []):
            norm = normalize_result_url(item.get("url", ""))
            if norm and norm in seen:
                dedup_count += 1
                continue
            if norm:
                seen.add(norm)
            item = item.copy()
            item.setdefault("provider", provider_name)
            deduped.append(item)
            if len(deduped) >= max_results:
                return deduped, dedup_count
    return deduped, dedup_count

def _result_domain(url: str) -> str:
    try:
        netloc = urlparse(url or "").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


# Authority rules per query intent (wsp_core/intents.py): results from these
# domains move up or down in the intent reranker and the quality report.
CANONICAL_DOMAIN_RULES: Dict[str, Dict[str, List[str]]] = {
    "docs": {
        "boost": ["docs.", "developer.", "github.com", "readthedocs.io", "modelcontextprotocol.io"],
        "demote": ["medium.com", "dev.to", "reddit.com", "stackoverflow.com", "youtube.com"],
    },
    "security": {
        "boost": [
            "nvd.nist.gov", "cve.org", "github.com", "github.com/advisories", "security.",
            "cert.europa.eu", "kb.cert.org", "cisa.gov", "bsi.bund.de", "cert.ssi.gouv.fr",
            "ncsc.gov.uk", "msrc.microsoft.com", "owasp.org", "first.org",
        ],
        "demote": ["youtube.com", "medium.com", "reddit.com", "stackexchange.com"],
    },
}


def _domain_matches_rule(domain: str, rule: str) -> bool:
    if rule.endswith("."):
        # Label-prefix rules such as "docs." / "investor." / "ir." match a
        # leading host label (docs.python.org), never a bare domain (notdocs.com).
        return domain.startswith(rule)
    # Exact domain or true subdomain only. A bare startswith would let
    # look-alike registrations such as openai.com.evil.example inherit
    # authority boosts (same reasoning as _blocked_domain_matches below).
    return domain == rule or domain.endswith(f".{rule}")


# Known content mirrors and SEO scraper sites that republish Stack Overflow,
# GitHub, and documentation content. These add no information over the
# canonical source and frequently outrank it; they are removed from results
# rather than merely demoted. Operators can extend via config
# quality.blocked_domains or rescue a domain via quality.allowed_domains.
SPAM_MIRROR_DOMAINS: List[str] = [
    # Stack Overflow / Q&A scrapers
    "newbedev.com",
    "stackoom.com",
    "stackovergo.com",
    "syntaxfix.com",
    "copyprogramming.com",
    "devcodef1.com",
    "exceptionshub.com",
    "code-examples.net",
    "i-harness.com",
    "fixmycodeerror.com",
    "stacklesson.com",
    # Python documentation look-alikes
    "domainunion.de",
    "pythonlang.net",
    "pythonlang.de",
    # GitHub issue/readme mirrors
    "githubmemory.com",
    "gitmemory.com",
    "issueexplorer.com",
    "bleepcoder.com",
    "gitanswer.com",
    # Documentation mirrors
    "w3cub.com",
    # Generic AI/SEO content farms already demoted by the intent reranker
    "aizolo.com",
]


def _blocked_domain_matches(domain: str, rule: str) -> bool:
    """Strict matcher for domain block/allow lists.

    Only the exact domain or true subdomains match (``newbedev.com``,
    ``de.newbedev.com``). Unlike ``_domain_matches_rule`` there is no
    ``startswith`` clause, so look-alike registrations such as
    ``newbedev.com.evil.example`` do NOT match.
    """
    return domain == rule or domain.endswith(f".{rule}")


_SITE_OPERATOR_RE = re.compile(r"\bsite:([a-z0-9][a-z0-9.-]*)", re.IGNORECASE)


def extract_domain_constraints(query: str, include_domains: Optional[List[str]] = None) -> List[str]:
    """Domains the user explicitly constrained the search to.

    Collects ``site:`` operators from the query plus ``include_domains``.
    Explicit constraints express intent: constrained domains are exempt from
    spam filtering, and domain-diversity reranking is skipped entirely.
    """
    domains = [d.lower().rstrip(".") for d in _SITE_OPERATOR_RE.findall(query or "")]
    for entry in include_domains or []:
        if entry and entry.strip():
            domains.append(entry.lower().strip())
    return sorted(set(domains))


def filter_spam_results(
    results: List[Dict[str, Any]],
    extra_blocked: Optional[List[str]] = None,
    allowed: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Drop results from known mirror/SEO-spam domains.

    Returns the kept results and the sorted unique domains that were removed.
    ``allowed`` rescues a domain from both the builtin and extra blocklists.
    """
    blocked_rules = SPAM_MIRROR_DOMAINS + [d.lower().strip() for d in (extra_blocked or []) if d and d.strip()]
    allowed_rules = [d.lower().strip() for d in (allowed or []) if d and d.strip()]
    kept: List[Dict[str, Any]] = []
    removed_domains: List[str] = []
    for item in results:
        domain = _result_domain(item.get("url", ""))
        if (
            domain
            and not any(_blocked_domain_matches(domain, rule) for rule in allowed_rules)
            and any(_blocked_domain_matches(domain, rule) for rule in blocked_rules)
        ):
            removed_domains.append(domain)
            continue
        kept.append(item)
    return kept, sorted(set(removed_domains))


def rerank_domain_diversity(
    results: List[Dict[str, Any]],
    max_per_domain: int = 2,
    query: str = "",
) -> Tuple[List[Dict[str, Any]], int]:
    """Stable rerank that stops one domain from crowding out the result list.

    The first ``max_per_domain`` results per domain keep their original order;
    overflow results are moved behind the diverse head (also in original
    order) instead of being dropped. Returns the reranked list and how many
    results were demoted.
    """
    if max_per_domain < 1 or len(results) < 3:
        return results, 0
    # Official docs of the product the query names are not crowding: five
    # react.dev pages answer "React useEffect cleanup" better than a mix.
    query_terms = _query_terms(query)
    head: List[Dict[str, Any]] = []
    overflow: List[Dict[str, Any]] = []
    per_domain: Dict[str, int] = {}
    for item in results:
        domain = _result_domain(item.get("url", ""))
        count = per_domain.get(domain, 0)
        if domain and count >= max_per_domain and not _is_named_vendor_domain(item.get("url", ""), query_terms):
            overflow.append(item)
            continue
        per_domain[domain] = count + 1
        head.append(item)
    return head + overflow, len(overflow)


def _url_matches_rule(url: str, rule: str) -> bool:
    domain = _result_domain(url)
    if "/" not in rule:
        return _domain_matches_rule(domain, rule)
    normalized = host_and_path(url)
    normalized_rule = rule.lower().strip().rstrip("/")
    return normalized == normalized_rule or normalized.startswith(f"{normalized_rule}/")


# Host labels that never name a vendor on their own.
_GENERIC_HOST_LABELS = frozenset({
    "www", "docs", "doc", "developer", "developers", "dev", "api", "help", "support", "wiki",
    "blog", "news", "learn", "security", "com", "org", "net", "io", "dev", "gov", "edu",
    "co", "uk", "de", "app", "cloud", "github", "medium", "reddit", "youtube", "stackoverflow",
})


# Social profiles answer no docs, security, academic or news question; in those
# classes a spare result from the overfetch replaces them in the top N.
_SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "pinterest.com", "threads.net")
_SOCIAL_SINK_CLASSES = frozenset({"docs", "security", "academic", "news"})
# Classes where the source the query names ("Bundestag" -> bundestag.de) is the
# primary answer; one such spare may take the last top-N slot.
_VENDOR_PROMOTE_CLASSES = frozenset({"docs", "security", "news"})


def _is_social(url: str) -> bool:
    return any(_domain_matches_rule(_result_domain(url), host) for host in _SOCIAL_HOSTS)


def _query_terms(query: str) -> set:
    return {term for term in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(term) >= 3}


# Public suffixes a vendor's own site plausibly uses. Novelty TLDs
# (.website, .wiki, ...) host look-alikes, so they never count as the vendor.
_VENDOR_TLDS = frozenset({
    "com", "org", "net", "io", "dev", "rs", "sh", "ai", "app", "gov", "eu",
    "de", "at", "ch", "fr", "uk", "jp", "us", "info",
})


def _is_named_vendor_domain(url: str, query_terms: set) -> bool:
    """True when the registrable domain of the result is named by the query.

    Only the label left of the public suffix counts (``fastapi`` in
    fastapi.tiangolo.com is a subdomain of tiangolo and does not; neither does
    ``injection`` in injection.readthedocs.io). ``typescriptlang.org`` matches
    "typescript" because the label starts with the query word.
    """
    domain = _result_domain(url)
    if not domain or not query_terms:
        return False
    labels = [label for label in domain.split(".") if label]
    if len(labels) < 2 or labels[-1] not in _VENDOR_TLDS:
        return False
    core = labels[-3] if len(labels) >= 3 and labels[-2] in {"co", "com", "gov", "org"} else labels[-2]
    if core in _GENERIC_HOST_LABELS:
        return False
    return any(core == term or (len(term) >= 4 and core.startswith(term)) for term in query_terms)


def rerank_results_for_intent(
    query: str,
    routing_class: str,
    results: List[Dict[str, Any]],
    window: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Small authority reranker for classes where source authority beats snippet luck.

    ``window`` limits the rerank to the first N results; the rest keep their
    order behind it. Spare results fetched only to refill filtered slots must
    not jump ahead of the provider's own top N.
    """
    if window is not None and 0 < window < len(results):
        head, rest = results[:window], results[window:]
        if routing_class in _SOCIAL_SINK_CLASSES:
            social = [item for item in head if _is_social(item.get("url", ""))]
            spares = [item for item in rest if not _is_social(item.get("url", ""))]
            if social and spares:
                kept = [item for item in head if not _is_social(item.get("url", ""))]
                refill = spares[: len(social)]
                head = kept + refill
                rest = [item for item in rest if item not in refill] + social
        if routing_class in _VENDOR_PROMOTE_CLASSES:
            terms = _query_terms(query)
            if not any(_is_named_vendor_domain(item.get("url", ""), terms) for item in head):
                vendor = next((item for item in rest if _is_named_vendor_domain(item.get("url", ""), terms)), None)
                if vendor is not None:
                    rest = [head[-1]] + [item for item in rest if item is not vendor]
                    head = head[:-1] + [vendor]
        head, meta = rerank_results_for_intent(query, routing_class, head)
        return head + [item.copy() for item in rest], meta
    rules = CANONICAL_DOMAIN_RULES.get(routing_class, {})
    if not results or not rules:
        return results, {"reranked": False, "routing_class": routing_class}

    q = query.lower()
    query_terms = _query_terms(query)
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for idx, item in enumerate(results):
        url = item.get("url", "")
        title = (item.get("title") or "").lower()
        snippet = (item.get("snippet") or item.get("description") or "").lower()
        score = float(len(results) - idx) * 0.01
        if _is_named_vendor_domain(url, query_terms):
            # The project or vendor the query names is the primary source and
            # outranks generic hosts such as github.com or readthedocs.io:
            # "nginx proxy_pass" -> nginx.org, "Ivanti CVE" -> ivanti.com.
            score += 12.0
        elif any(_url_matches_rule(url, rule) for rule in rules.get("boost", [])):
            score += 10.0
        if any(_url_matches_rule(url, rule) for rule in rules.get("demote", [])):
            score -= 16.0 if score >= 10.0 else 6.0
        if "official" in q and ("official" in title or "official" in snippet):
            score += 1.0
        scored.append((score, idx, item))

    reranked = [item.copy() for _, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))]
    before_urls = [item.get("url", "") for item in results]
    after_urls = [item.get("url", "") for item in reranked]
    changed = before_urls != after_urls
    return reranked, {
        "reranked": changed,
        "routing_class": routing_class,
        "top_domain_before": _result_domain(results[0].get("url", "")) if results else None,
        "top_domain_after": _result_domain(reranked[0].get("url", "")) if reranked else None,
    }


def build_authority_signals(routing_class: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize primary-source authority signals for quality reports."""
    rules = CANONICAL_DOMAIN_RULES.get(routing_class, {})
    urls = [item.get("url", "") for item in results if item.get("url")]
    domains = [_result_domain(url) for url in urls]
    boosted_domains = []
    demoted_domains = []
    boosted_flags = []
    for url, domain in zip(urls, domains):
        boosted = any(_url_matches_rule(url, rule) for rule in rules.get("boost", []))
        demoted = any(_url_matches_rule(url, rule) for rule in rules.get("demote", []))
        boosted_flags.append(boosted)
        if boosted:
            boosted_domains.append(domain)
        if demoted:
            demoted_domains.append(domain)

    return {
        "routing_class": routing_class,
        "rules_applied": bool(rules),
        "top_domain": domains[0] if domains else None,
        "canonical_domain_hits": sorted(set(boosted_domains)),
        "demoted_domain_hits": sorted(set(demoted_domains)),
        "canonical_top_result": bool(boosted_flags and boosted_flags[0]),
    }


def _snippet_text(item: Dict[str, Any]) -> str:
    return " ".join(
        str(item.get(k) or "")
        for k in ("description", "snippet", "content", "raw_content", "summary")
    ).strip()


def build_quality_report(
    query: str,
    result: Dict[str, Any],
    routing_info: Dict[str, Any],
    providers_considered: List[str],
    eligible_providers: List[str],
    cooldown_skips: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    near_duplicate_threshold: float = DEFAULT_NEAR_DUPLICATE_THRESHOLD,
) -> Dict[str, Any]:
    """Build transparent search-quality diagnostics without changing results."""
    results = result.get("results", []) or []
    domains = [_result_domain(r.get("url", "")) for r in results]
    domains = [d for d in domains if d]
    unique_domains = sorted(set(domains))
    duplicate_count = int(result.get("metadata", {}).get("dedup_count", 0) or 0)

    short_snippets = 0
    for item in results:
        if len(_snippet_text(item)) < 40:
            short_snippets += 1

    extract_reasons: List[str] = []
    confidence_level = routing_info.get("confidence_level") or "unknown"
    confidence_score = routing_info.get("confidence")
    if confidence_level == "low" or (confidence_score is not None and float(confidence_score or 0) < 0.4):
        extract_reasons.append("low routing confidence")
    if len(results) < 3:
        extract_reasons.append("few search results")
    if results and len(unique_domains) <= 1:
        extract_reasons.append("low domain diversity")
    if duplicate_count:
        extract_reasons.append("duplicate results detected")
    if results and short_snippets / max(len(results), 1) >= 0.5:
        extract_reasons.append("thin snippets")

    skipped = []
    for item in cooldown_skips:
        skipped.append({
            "provider": item.get("provider"),
            "reason": "cooldown",
            "cooldown_remaining_seconds": item.get("cooldown_remaining_seconds"),
        })
    for err in errors:
        skipped.append({
            "provider": err.get("provider"),
            "reason": "error",
            "error": err.get("error"),
        })

    routing_class = routing_info.get("analysis_summary", {}).get("routing_class")
    authority_signals = build_authority_signals(routing_class, results) if routing_class else None

    report = {
        "query": query,
        "selected_provider": routing_info.get("provider") or result.get("provider"),
        "routing_reason": routing_info.get("reason"),
        "routing_policy": routing_info.get("routing_policy", ROUTING_POLICY),
        "routing_class": routing_class,
        "language_hint": routing_info.get("analysis_summary", {}).get("language_hint"),

        "confidence": confidence_level,
        "confidence_score": routing_info.get("confidence"),
        "providers_considered": providers_considered,
        "eligible_providers": eligible_providers,
        "skipped_providers": skipped,
        "result_count": len(results),
        "domain_count": len(unique_domains),
        "domains": unique_domains,
        "domain_diversity": (len(unique_domains) / len(results)) if results else 0.0,
        "duplicate_count": duplicate_count,
        "thin_snippet_count": short_snippets,
        "extract_recommended": bool(extract_reasons),
        "extract_reasons": extract_reasons,
        "scores": routing_info.get("scores", {}),
        "authority_signals": authority_signals,
        "diversity": score_diversity(
            results, near_duplicate_threshold=near_duplicate_threshold
        ),
    }
    truncated = (result.get("metadata") or {}).get("query_truncated")
    if truncated:
        report["query_truncated"] = truncated
    return report


def select_research_providers(
    primary_provider: str,
    provider_priority: List[str],
    available_providers: set,
    max_providers: int = 3,
) -> List[str]:
    """Pick a compact provider set for research mode."""
    preferred = [primary_provider, "linkup", "tavily", "exa", "firecrawl", "brave", "serper", "you", "querit"]
    ordered: List[str] = []
    for provider in preferred + provider_priority:
        if provider and provider in available_providers and provider not in ordered:
            ordered.append(provider)
        if len(ordered) >= max_providers:
            break
    return ordered
