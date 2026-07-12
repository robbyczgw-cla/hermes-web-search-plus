"""Release-blocking source-only gates for provider registration and requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from provider_registry import PROVIDER_SPECS

SOURCE_ONLY_SEMANTICS = frozenset({"source_results", "source_text"})
_BANNED_BODY_KEYS = frozenset(
    {
        "messages",
        "system",
        "system_prompt",
        "answer",
        "include_answer",
        "synthesis",
        "reasoning",
        "claim",
        "verification",
    }
)
_BANNED_INSTRUCTION_FRAGMENTS = (
    "answer the user",
    "provide an answer",
    "synthesize",
    "reason step by step",
    "verify the claim",
)


def validate_provider_mode(provider: str, capability: str) -> str:
    """Return declared semantics or fail before provider selection/network I/O."""
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        raise ValueError(f"unknown provider mode: {provider}")
    if spec.rejected_reason:
        raise ValueError(f"provider {provider} rejected: {spec.rejected_reason}")
    if capability == "search":
        supported = spec.supports_search
        semantics = spec.search_output_semantics
    elif capability == "extract":
        supported = spec.supports_extract
        semantics = spec.extract_output_semantics
    else:
        raise ValueError(f"unsupported capability: {capability}")
    if not supported or semantics not in SOURCE_ONLY_SEMANTICS:
        raise ValueError(f"provider {provider} has no source-only {capability} mode")
    return semantics


def _walk(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def validate_outbound_body(provider: str, body: Mapping[str, Any]) -> None:
    """Reject answer-shaped outbound payloads before transport."""
    for key, value in _walk(body):
        lowered = key.lower()
        if lowered in _BANNED_BODY_KEYS:
            if lowered == "include_answer" and value is False:
                continue
            raise ValueError(f"source-only request gate rejected field: {key}")
        if isinstance(value, str):
            text = value.lower()
            if any(fragment in text for fragment in _BANNED_INSTRUCTION_FRAGMENTS):
                raise ValueError("source-only request gate rejected answer instruction")

    if provider == "tavily" and body.get("include_answer") is not False:
        raise ValueError("tavily source-only mode requires include_answer=false")
    if provider == "linkup" and body.get("outputType") != "searchResults":
        raise ValueError("linkup source-only mode requires outputType=searchResults")
    if provider == "exa" and body.get("type") in {"deep", "deep-reasoning"}:
        raise ValueError("exa deep modes are not source-only")
    if provider in {"perplexity", "kilo-perplexity"}:
        raise ValueError(f"{provider} has no verified source-only endpoint")
