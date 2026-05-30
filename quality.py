"""Result normalization, deduplication, reranking, and quality-report helpers."""

import hashlib
import math
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse


ROUTING_POLICY = "routing-v2"

# Default weight applied to the normalized lexical-relevance score during
# reranking. Kept below the authority boost/demote magnitudes in
# CANONICAL_DOMAIN_RULES (+10 / -6) so canonical-source authority still wins;
# lexical relevance reorders results *within* the same authority tier and
# provides the primary ordering signal for classes that have no authority rules.
DEFAULT_LEXICAL_WEIGHT = 3.0

# Conservative multilingual stopword set (English + German, the two languages
# this plugin routes most heavily) plus common search operators. Dropped from
# the query so boilerplate terms do not dominate the relevance signal.
_LEXICAL_STOPWORDS = frozenset({
    # English
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "with", "you", "your",
    "do", "does", "i", "me", "my", "we", "us", "can", "will", "vs",
    # German
    "der", "die", "das", "und", "oder", "ist", "im", "in", "von", "zu", "den",
    "dem", "ein", "eine", "einen", "fuer", "für", "mit", "auf", "wie", "was",
    "wer", "wo", "warum", "wann", "welche", "welcher", "ich", "wir", "sie",
    # Search operators (e.g. site:, filetype:) tokenize into these keywords.
    "site", "filetype", "inurl", "intitle", "intext", "url",
})

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


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
    if not url:
        return ""
    parsed = urlparse(url.strip())
    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    return f"{netloc}{path}"


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

def _choose_tie_winner(query: str, winners: List[str], priority: List[str]) -> str:
    """Break score ties deterministically per query.

    Uses a stable hash of the query to distribute ties across providers while
    keeping the same query reproducible across runs.
    """
    ordered_winners = [p for p in priority if p in winners]
    if not ordered_winners:
        ordered_winners = sorted(winners)
    if len(ordered_winners) == 1:
        return ordered_winners[0]
    digest = hashlib.sha256(f"{query}|{'|'.join(ordered_winners)}".encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(ordered_winners)
    return ordered_winners[idx]


def _result_domain(url: str) -> str:
    try:
        netloc = urlparse(url or "").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


CANONICAL_DOMAIN_RULES: Dict[str, Dict[str, List[str]]] = {
    "official_vendor_release": {
        "boost": [
            "mistral.ai", "anthropic.com", "openai.com", "googleblog.com",
            "blog.google", "ai.google.dev", "meta.com", "ai.meta.com",
            "nvidia.com", "developer.nvidia.com", "apple.com", "microsoft.com",
        ],
        "demote": ["youtube.com", "youtu.be", "medium.com", "aizolo.com", "reddit.com"],
    },
    "official_docs": {
        "boost": ["docs.", "developer.", "github.com", "readthedocs.io", "modelcontextprotocol.io"],
        "demote": ["medium.com", "dev.to", "reddit.com", "stackoverflow.com", "youtube.com"],
    },
    "policy_pdf": {
        "boost": ["europa.eu", "ec.europa.eu", "nist.gov", "nvlpubs.nist.gov", "oecd.org", "who.int", "gov.uk", "federalregister.gov"],
        "demote": ["scribd.com", "researchgate.net", "universityofcalifornia.edu", "slideshare.net"],
    },
    "finance_earnings_official": {
        "boost": ["investor.", "ir.", "nvidia.com", "sec.gov", "nasdaq.com"],
        "demote": ["reddit.com", "fool.com", "seekingalpha.com", "youtube.com"],
    },
    "security_advisory": {
        "boost": ["nvd.nist.gov", "cve.org", "github.com", "github.com/advisories", "security.", "cert.europa.eu", "kb.cert.org"],
        "demote": ["youtube.com", "medium.com", "reddit.com"],
    },
}


def _domain_matches_rule(domain: str, rule: str) -> bool:
    return domain == rule or domain.endswith(f".{rule}") or domain.startswith(rule)


def _url_matches_rule(url: str, rule: str) -> bool:
    domain = _result_domain(url)
    if "/" not in rule:
        return _domain_matches_rule(domain, rule)
    normalized = normalize_result_url(url)
    normalized_rule = rule.lower().strip().rstrip("/")
    return normalized == normalized_rule or normalized.startswith(f"{normalized_rule}/")


def _tokenize(text: str) -> List[str]:
    """Lowercase word-tokenize, dropping stopwords and single chars."""
    tokens = []
    for tok in _TOKEN_RE.findall((text or "").lower()):
        if tok in _LEXICAL_STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        tokens.append(tok)
    return tokens


def _result_document(item: Dict[str, Any]) -> str:
    """Build the text a result is scored against: title + snippet + url path."""
    title = item.get("title") or ""
    snippet = item.get("snippet") or item.get("description") or item.get("content") or ""
    # Path/slug tokens often carry the most query-relevant terms for SEO pages.
    path = ""
    try:
        parsed = urlparse(item.get("url") or "")
        path = (parsed.path or "").replace("-", " ").replace("_", " ").replace("/", " ")
    except Exception:
        path = ""
    return f"{title} {snippet} {path}"


def compute_lexical_relevance(
    query: str,
    results: List[Dict[str, Any]],
    k1: float = 1.5,
    b: float = 0.75,
) -> Tuple[List[float], Dict[str, Any]]:
    """Score how well each result's text matches the query terms (BM25, stdlib).

    Returns ``(scores, detail)`` where ``scores`` is aligned to ``results`` and
    normalized to ``[0, 1]`` relative to the best-matching result (best == 1.0),
    so the weight applied during reranking has a predictable magnitude. ``detail``
    reports whether the signal applied and summary metrics for diagnostics.
    """
    query_terms = _tokenize(query)
    detail: Dict[str, Any] = {
        "applied": False,
        "query_terms": query_terms,
        "top_relevance": 0.0,
        "mean_relevance": 0.0,
    }
    if not results or not query_terms:
        return [0.0] * len(results), detail

    docs = [_tokenize(_result_document(item)) for item in results]
    doc_lengths = [len(d) for d in docs]
    total_len = sum(doc_lengths)
    if total_len == 0:
        return [0.0] * len(results), detail
    avg_len = total_len / len(docs)

    n_docs = len(docs)
    doc_freq: Dict[str, int] = {}
    unique_terms = set(query_terms)
    for doc in docs:
        doc_set = set(doc)
        for term in unique_terms:
            if term in doc_set:
                doc_freq[term] = doc_freq.get(term, 0) + 1

    idf = {
        term: math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for term, df in doc_freq.items()
    }

    raw_scores: List[float] = []
    for doc, length in zip(docs, doc_lengths):
        if not doc:
            raw_scores.append(0.0)
            continue
        counts: Dict[str, int] = {}
        for term in doc:
            if term in idf:
                counts[term] = counts.get(term, 0) + 1
        score = 0.0
        for term, tf in counts.items():
            denom = tf + k1 * (1 - b + b * (length / avg_len))
            score += idf[term] * (tf * (k1 + 1)) / denom
        raw_scores.append(score)

    max_raw = max(raw_scores)
    if max_raw <= 0:
        return [0.0] * len(results), detail

    scores = [round(s / max_raw, 4) for s in raw_scores]
    detail["applied"] = True
    detail["top_relevance"] = max(scores)
    detail["mean_relevance"] = round(sum(scores) / len(scores), 4)
    return scores, detail


def rerank_results_for_intent(
    query: str,
    routing_class: str,
    results: List[Dict[str, Any]],
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
    enable_lexical: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rerank by query relevance plus source authority.

    Two signals combine:

    * **Authority** — for classes in ``CANONICAL_DOMAIN_RULES`` only, canonical
      sources are boosted (+10) and aggregators demoted (-6); this dominates so a
      canonical primary source always wins its tier.
    * **Lexical relevance** — a normalized BM25 score (``[0, 1]`` × ``lexical_weight``)
      applied to *every* class, so results whose title/snippet actually match the
      query outrank provider-order luck. Its weight stays below the authority gap,
      so it reorders within an authority tier and orders rule-free classes.
    """
    rules = CANONICAL_DOMAIN_RULES.get(routing_class, {})
    if not results:
        return results, {"reranked": False, "routing_class": routing_class, "lexical_applied": False}

    if enable_lexical:
        lexical_scores, lexical_detail = compute_lexical_relevance(query, results)
    else:
        lexical_scores, lexical_detail = [0.0] * len(results), {"applied": False}
    lexical_applied = bool(lexical_detail.get("applied"))

    # Nothing to reorder by: no authority rules and no usable relevance signal.
    if not rules and not lexical_applied:
        return results, {"reranked": False, "routing_class": routing_class, "lexical_applied": False}

    q = query.lower()
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for idx, item in enumerate(results):
        url = item.get("url", "")
        domain = _result_domain(url)
        title = (item.get("title") or "").lower()
        snippet = (item.get("snippet") or item.get("description") or "").lower()
        # Original provider order as a small, stable tie-breaker.
        score = float(len(results) - idx) * 0.01
        score += lexical_weight * lexical_scores[idx]
        if rules:
            if any(_url_matches_rule(url, rule) for rule in rules.get("boost", [])):
                score += 10.0
            if any(_url_matches_rule(url, rule) for rule in rules.get("demote", [])):
                score -= 6.0
            if routing_class == "official_vendor_release" and any(term in domain for term in ("mistral", "anthropic", "openai", "nvidia", "google", "meta")):
                score += 3.0
            if routing_class == "policy_pdf" and (url.lower().endswith(".pdf") or "pdf" in title):
                score += 2.0
            if "official" in q and ("official" in title or "official" in snippet):
                score += 1.0
        item_copy = item.copy()
        if lexical_applied:
            item_copy["relevance"] = lexical_scores[idx]
        scored.append((score, idx, item_copy))

    reranked = [item for _, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))]
    before_urls = [item.get("url", "") for item in results]
    after_urls = [item.get("url", "") for item in reranked]
    changed = before_urls != after_urls
    return reranked, {
        "reranked": changed,
        "routing_class": routing_class,
        "lexical_applied": lexical_applied,
        "top_relevance": lexical_detail.get("top_relevance", 0.0),
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

    relevance_scores, relevance_detail = compute_lexical_relevance(query, results)
    relevance_signals = {
        "applied": relevance_detail.get("applied", False),
        "top_relevance": relevance_detail.get("top_relevance", 0.0),
        "mean_relevance": relevance_detail.get("mean_relevance", 0.0),
        "low_relevance_count": sum(1 for s in relevance_scores if s < 0.15),
    }

    return {
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
        "relevance_signals": relevance_signals,
    }


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
