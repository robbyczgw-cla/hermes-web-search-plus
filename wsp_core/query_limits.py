"""Per-provider query length limits.

A provider that documents a hard limit rejects a longer query with a 4xx. The
site operators WSP appends for domain filters count towards that limit, so the
limit is applied to the final query: the free text is shortened at a word
boundary, the operators stay.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

MAX_QUERY_CHARS = 2000  # generic cap in the engine entry, before any provider


class QueryLimit(NamedTuple):
    chars: int
    words: int


# Only limits the provider documents. Do not add a guess.
PROVIDER_QUERY_LIMITS: Dict[str, QueryLimit] = {
    "brave": QueryLimit(chars=400, words=50),  # Brave Web Search API: q
}


def _fits(text: str, limit: QueryLimit) -> bool:
    return len(text) <= limit.chars and len(text.split()) <= limit.words


def fit_query(
    provider: str, text: str, operators: Sequence[str] = ()
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return ``(query, info)`` for ``text`` plus appended ``operators``.

    ``info`` is None when the query is sent unchanged. Otherwise it describes
    the change (sizes only, never query text) for result metadata. Trailing
    ``-site:`` operators are dropped only when the operators alone leave no room
    for any free text; ``ValueError`` when the include filter alone is too long.
    """
    text = text or ""
    ops: List[str] = [op for op in operators if op]
    full = " ".join(p for p in [text, *ops] if p).strip()
    limit = PROVIDER_QUERY_LIMITS.get(provider)
    if limit is None or _fits(full, limit):
        return full, None

    words = text.split()
    dropped = 0
    while True:
        suffix = " ".join(ops)
        sep = 1 if (suffix and words) else 0
        room_chars = limit.chars - len(suffix) - sep
        room_words = limit.words - len(suffix.split())
        floor = 1 if words else 0
        if room_chars >= floor and room_words >= floor:
            break
        if not ops or not ops[-1].startswith("-site:"):
            raise ValueError(
                f"Too many domain filters for {provider}: the query limit is "
                f"{limit.chars} characters / {limit.words} words"
            )
        ops.pop()
        dropped += 1

    kept: List[str] = []
    used = 0
    for word in words:
        add = len(word) + (1 if kept else 0)
        if used + add > room_chars or len(kept) + 1 > room_words:
            break
        kept.append(word)
        used += add
    if words and not kept:
        kept = [words[0][:room_chars]]  # one token longer than the limit
    query = " ".join([*kept, *ops]).strip()

    info: Dict[str, Any] = {
        "provider": provider,
        "limit_chars": limit.chars,
        "limit_words": limit.words,
        "original_chars": len(full),
        "original_words": len(full.split()),
        "sent_chars": len(query),
        "sent_words": len(query.split()),
    }
    if dropped:
        info["operators_dropped"] = dropped
    return query, info
