"""Namespaced, ownership-safe response cache for the frozen v3 contract."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from contract_v3 import RequestV3


CACHE_SCHEMA_VERSION = 1
CACHE_OWNER = "web-search-plus:v3"


@dataclass(frozen=True)
class CacheLookupV3:
    disposition: str
    payload: Optional[Dict[str, Any]] = None
    entry_id: Optional[str] = None
    age_seconds: Optional[int] = None
    source_contract_version: Optional[str] = None
    legacy_payload: Optional[Dict[str, Any]] = None


def derive_cache_key(request: RequestV3) -> str:
    """Hash execution semantics, deliberately excluding request/cache policy IDs."""
    material = {
        "contract_version": request.contract_version,
        "capability": request.capability.value,
        "input": request.input,
        "options": request.options,
        "routing": request.routing,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    return f"{request.capability.value}_{digest}"


class ResponseCacheV3:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.response_root = self.root / "v3" / "response"

    def path_for(self, request: RequestV3) -> Path:
        entry_id = derive_cache_key(request)
        return self.response_root / request.capability.value / f"{entry_id}.json"

    @staticmethod
    def _owned_envelope(value: object) -> bool:
        return (
            isinstance(value, dict)
            and value.get("owner") == CACHE_OWNER
            and value.get("cache_schema_version") == CACHE_SCHEMA_VERSION
            and value.get("contract_version") == "3.0"
            and isinstance(value.get("payload"), dict)
        )

    @staticmethod
    def _read(path: Path) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if ResponseCacheV3._owned_envelope(value) else None

    @staticmethod
    def _atomic_write(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def put(
        self,
        request: RequestV3,
        payload: Dict[str, Any],
        *,
        now: int,
        legacy_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        entry_id = derive_cache_key(request)
        envelope = {
            "owner": CACHE_OWNER,
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "contract_version": "3.0",
            "entry_id": entry_id,
            "created_at": int(now),
            "payload": payload,
            "legacy_payload": legacy_payload,
        }
        self._atomic_write(self.path_for(request), envelope)
        return entry_id

    def get(
        self,
        request: RequestV3,
        *,
        ttl_seconds: int,
        allow_stale_seconds: int,
        now: int,
    ) -> CacheLookupV3:
        path = self.path_for(request)
        envelope = self._read(path)
        if envelope is None:
            return CacheLookupV3("miss")
        created_at = envelope.get("created_at")
        if not isinstance(created_at, int):
            return CacheLookupV3("miss")
        age = max(0, int(now) - created_at)
        entry_id = str(envelope.get("entry_id") or derive_cache_key(request))
        if age <= max(0, int(ttl_seconds)):
            return CacheLookupV3(
                "fresh_hit",
                dict(envelope["payload"]),
                entry_id,
                age,
                "3.0",
                dict(envelope["legacy_payload"])
                if isinstance(envelope.get("legacy_payload"), dict)
                else None,
            )
        if age <= max(0, int(ttl_seconds)) + max(0, int(allow_stale_seconds)):
            return CacheLookupV3(
                "stale_hit",
                dict(envelope["payload"]),
                entry_id,
                age,
                "3.0",
                dict(envelope["legacy_payload"])
                if isinstance(envelope.get("legacy_payload"), dict)
                else None,
            )
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return CacheLookupV3("miss")

    def stats(self) -> Dict[str, int]:
        entries = 0
        size_bytes = 0
        if self.response_root.exists():
            for path in self.response_root.rglob("*.json"):
                if self._read(path) is None:
                    continue
                entries += 1
                try:
                    size_bytes += path.stat().st_size
                except OSError:
                    pass
        return {"entries": entries, "size_bytes": size_bytes}

    def clear(self) -> int:
        cleared = 0
        if not self.response_root.exists():
            return cleared
        for path in self.response_root.rglob("*.json"):
            if self._read(path) is None:
                continue
            try:
                path.unlink()
                cleared += 1
            except OSError:
                pass
        return cleared


def peek_legacy_search(
    root: str | Path,
    *,
    query: str,
    provider: str,
    max_results: int,
    params: Optional[Dict[str, Any]],
    ttl_seconds: int,
    now: int,
) -> CacheLookupV3:
    """Read a v2 search entry without modifying, deleting, or refreshing it."""
    material: Dict[str, Any] = {
        "query": query,
        "provider": provider,
        "max_results": max_results,
    }
    if params:
        material.update(params)
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    entry_id = hashlib.sha256(encoded).hexdigest()[:32]
    path = Path(root) / f"{entry_id}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CacheLookupV3("miss")
    marker_fields = {
        "_cache_timestamp",
        "_cache_key",
        "_cache_query",
        "_cache_provider",
    }
    if not isinstance(cached, dict) or not marker_fields.issubset(cached):
        return CacheLookupV3("miss")
    timestamp = cached.get("_cache_timestamp")
    if not isinstance(timestamp, (int, float)):
        return CacheLookupV3("miss")
    age = max(0, int(now - timestamp))
    if age > max(0, int(ttl_seconds)):
        return CacheLookupV3("miss")
    legacy_payload = {
        key: value for key, value in cached.items() if not key.startswith("_cache_")
    }
    legacy_payload["cached"] = True
    legacy_payload["cache_age_seconds"] = age
    return CacheLookupV3(
        "fresh_hit",
        entry_id=entry_id,
        age_seconds=age,
        source_contract_version="2.x",
        legacy_payload=legacy_payload,
    )


def _legacy_canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def sanitize_legacy_search(path: str | Path) -> Dict[str, Any]:
    """Read and sanitize a v2 search entry without ever modifying its bytes."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"cache_status": "legacy_rejected", "observations": [], "warnings": []}
    if not isinstance(payload, dict):
        return {"cache_status": "legacy_rejected", "observations": [], "warnings": []}

    banned = {"answer", "full_synthesis", "claim", "verification", "truth_confidence"}
    dropped = sorted(key for key in payload if key in banned)
    provider = str(payload.get("_cache_provider") or "legacy")
    observations = []
    for index, raw in enumerate(payload.get("results") or []):
        if not isinstance(raw, dict):
            continue
        result = dict(raw)
        for key in banned:
            if key in result:
                dropped.append(key)
                result.pop(key, None)
        if result.get("type") == "synthesis":
            dropped.append("type:synthesis")
            result.pop("type", None)
        url = result.get("url")
        snippet = result.get("snippet")
        if not isinstance(url, str) or not url or not isinstance(snippet, str):
            continue
        observations.append(
            {
                "observation_id": f"obs_legacy_{index}",
                "provider_attempt_id": "attempt_legacy_cache",
                "provider_result_index": index,
                "provider": provider,
                "endpoint_id": f"{provider}:search",
                "kind": "search_result",
                "url": {"observed": url, "canonical": _legacy_canonical_url(url)},
                "title": str(result.get("title")) if result.get("title") is not None else None,
                "snippet": snippet,
                "text": None,
                "provider_rank": index + 1,
                "provider_score": None,
                "published_at": None,
                "provider_fields": {},
            }
        )

    warnings = []
    if dropped:
        warnings.append(
            {
                "code": "wsp.cache.legacy_field_dropped",
                "reason": "LEGACY_FIELD_DROPPED",
                "details": {"fields": sorted(set(dropped))},
            }
        )
    return {
        "cache_status": "legacy_hit" if observations else "legacy_rejected",
        "observations": observations,
        "warnings": warnings,
    }
