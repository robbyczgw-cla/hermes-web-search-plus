"""Deterministic local deduplication and source-independence estimation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid"}
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_MINHASH_PERMUTATIONS = 32
_MINHASH_THRESHOLD = 0.65


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def _tokens(result: Dict[str, Any]) -> List[str]:
    text = " ".join(
        str(result.get(field) or "") for field in ("title", "snippet", "text")
    )
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _TOKEN_RE.findall(normalized)


def _shingles(result: Dict[str, Any]) -> set[str]:
    tokens = _tokens(result)
    if len(tokens) < 5:
        return set()
    return {" ".join(tokens[index : index + 3]) for index in range(len(tokens) - 2)}


def _minhash(shingles: Iterable[str]) -> Optional[Tuple[int, ...]]:
    values = tuple(sorted(set(shingles)))
    if not values:
        return None
    signature = []
    for seed in range(_MINHASH_PERMUTATIONS):
        prefix = seed.to_bytes(2, "big")
        signature.append(
            min(
                int.from_bytes(
                    hashlib.sha256(prefix + value.encode("utf-8")).digest()[:8],
                    "big",
                )
                for value in values
            )
        )
    return tuple(signature)


def _similarity(left: Tuple[int, ...], right: Tuple[int, ...]) -> float:
    return sum(a == b for a, b in zip(left, right)) / len(left)


class _UnionFind:
    def __init__(self, identifiers: Sequence[str]):
        self.parent = {identifier: identifier for identifier in identifiers}

    def find(self, identifier: str) -> str:
        parent = self.parent[identifier]
        if parent != identifier:
            self.parent[identifier] = self.find(parent)
        return self.parent[identifier]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first


def _providers(result: Dict[str, Any]) -> List[str]:
    providers = {
        str(item.get("provider"))
        for item in result.get("provenance") or []
        if isinstance(item, dict) and item.get("provider")
    }
    return sorted(providers)


def _source_family(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def analyze_source_independence(
    results: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Cluster returned results and estimate independence without network access."""
    if not results:
        return [], None
    ordered = sorted((dict(item) for item in results), key=lambda item: item["result_id"])
    identifiers = [str(item["result_id"]) for item in ordered]
    union = _UnionFind(identifiers)
    canonical = {
        str(item["result_id"]): canonicalize_url(
            str(item.get("canonical_url") or item.get("url") or "")
        )
        for item in ordered
    }
    signatures = {
        str(item["result_id"]): _minhash(_shingles(item)) for item in ordered
    }
    for index, left in enumerate(ordered):
        left_id = str(left["result_id"])
        for right in ordered[index + 1 :]:
            right_id = str(right["result_id"])
            if canonical[left_id] and canonical[left_id] == canonical[right_id]:
                union.union(left_id, right_id)
                continue
            left_signature = signatures[left_id]
            right_signature = signatures[right_id]
            if (
                left_signature is not None
                and right_signature is not None
                and _similarity(left_signature, right_signature) >= _MINHASH_THRESHOLD
            ):
                union.union(left_id, right_id)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in ordered:
        grouped.setdefault(union.find(str(item["result_id"])), []).append(item)

    clusters = []
    for members in grouped.values():
        member_ids = sorted(str(item["result_id"]) for item in members)
        canonical_id = member_ids[0]
        providers = sorted(
            {provider for item in members for provider in _providers(item)}
        )
        digest = hashlib.sha256("\x1f".join(member_ids).encode("utf-8")).hexdigest()[:16]
        clusters.append(
            {
                "cluster_id": f"cluster_{digest}",
                "canonical_result_id": canonical_id,
                "member_result_ids": member_ids,
                "canonical_url": canonical[canonical_id],
                "providers": providers,
            }
        )
    clusters.sort(key=lambda item: item["cluster_id"])

    result_count = len(ordered)
    cluster_count = len(clusters)
    source_families = {
        _source_family(canonical[identifier])
        for identifier in identifiers
        if _source_family(canonical[identifier])
    }
    providers = {
        provider for item in ordered for provider in _providers(item)
    }
    text_capable = sum(signature is not None for signature in signatures.values()) >= 2
    score = round(
        0.7 * (cluster_count / result_count)
        + 0.3 * (len(source_families) / result_count),
        3,
    )
    estimate = {
        "score": min(1.0, max(0.0, score)),
        "unique_cluster_count": cluster_count,
        "result_count": result_count,
        "source_family_count": len(source_families),
        "provider_count": len(providers),
        "method": "url+snippet-minhash-v1" if text_capable else "url-v1",
        "confidence": "medium" if text_capable and result_count > 1 else "low",
        "method_degraded": not text_capable,
        "limitations": [
            "shared upstream indexes and syndication without textual overlap may be undetectable"
        ],
    }
    return clusters, estimate
