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

from .cache_identity_v3 import (
    EXTRACTION_CACHE_IDENTITY_VERSION,
    ExtractionCacheIdentityV3,
)
from .contract_v3 import RequestV3, cache_hit_routing_receipt_v3


CACHE_SCHEMA_VERSION = 3
CACHE_OWNER = "web-search-plus:v3"
NORMALIZER_VERSION = "runtime-v3-amendment-002"
_EXTRACTION_IDENTITY_VARY_KEY = "extraction_cache_identity"


@dataclass(frozen=True)
class CacheLookupV3:
    disposition: str
    payload: Optional[Dict[str, Any]] = None
    entry_id: Optional[str] = None
    age_seconds: Optional[int] = None
    source_contract_version: Optional[str] = None
    legacy_payload: Optional[Dict[str, Any]] = None


def cache_material_from_response(
    payload: Dict[str, Any], *, legacy_payload: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """Reduce a wire response to cache-owned evidence material."""
    attempts = payload.get("provider_attempts") or []
    successful = next(
        (item for item in reversed(attempts) if item.get("outcome") == "success"),
        {},
    )
    routing = payload.get("routing_receipt") or {}
    capability = str(payload.get("capability") or "")
    extract_legacy = (legacy_payload or {}) if capability == "extract" else {}
    legacy_provider = (legacy_payload or {}).get("provider")
    if not isinstance(legacy_provider, str):
        legacy_provider = None
    legacy_routing_hint = extract_legacy.get("routing")
    legacy_projection_hints = []
    for item in extract_legacy.get("results") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        hint: Dict[str, Any] = {
            "url": str(item["url"]),
            "raw_content_alias": "raw_content" in item,
        }
        if isinstance(item.get("provider"), str):
            hint["provider"] = item["provider"]
        for scalar_field in ("favicon", "published_date"):
            if isinstance(item.get(scalar_field), str):
                hint[scalar_field] = item[scalar_field]
        legacy_projection_hints.append(hint)
    return {
        "origin_execution_id": str(
            payload.get("execution_id") or "exec_cache_origin_unknown"
        ),
        "origin_provider": (
            routing.get("selected_provider")
            or legacy_provider
            or successful.get("provider")
        ),
        "endpoint_id": successful.get("endpoint_id"),
        "normalizer_version": NORMALIZER_VERSION,
        "contract_version": "3.0",
        "capability": payload.get("capability"),
        "status": payload.get("status"),
        "observations": list(payload.get("observations") or []),
        "policy_actions": list(payload.get("policy_actions") or []),
        "source_diversity": dict(payload.get("source_diversity") or {}),
        "projection": list(payload.get("results") or []),
        "routing_receipt": dict(routing),
        "limits_applied": dict(payload.get("limits_applied") or {}),
        "stored_content": list(payload.get("stored_content") or []),
        "dedup_clusters": list(payload.get("dedup_clusters") or []),
        "warnings": list(payload.get("warnings") or []),
        "legacy_routing_hint": (
            dict(legacy_routing_hint)
            if isinstance(legacy_routing_hint, dict)
            else None
        ),
        "legacy_projection_hints": legacy_projection_hints,
        "engine": payload.get("engine"),
        "error": payload.get("error"),
    }


def response_payload_from_cache_material(
    material: Dict[str, Any],
    *,
    request_id: str,
    execution_id: str,
    disposition: str,
    entry_id: str,
    age_seconds: int,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """Build a fresh wire response from normalized cached evidence."""
    payload = {
        "contract_version": "3.0",
        "request_id": request_id,
        "execution_id": execution_id,
        "capability": material.get("capability"),
        "status": material.get("status"),
        "results": list(material.get("projection") or []),
        "observations": list(material.get("observations") or []),
        "policy_actions": list(material.get("policy_actions") or []),
        "source_diversity": dict(material.get("source_diversity") or {}),
        "provider_attempts": [],
        "routing_receipt": cache_hit_routing_receipt_v3(
            dict(material.get("routing_receipt") or {}),
            origin_execution_id=str(
                material.get("origin_execution_id") or "exec_cache_origin_unknown"
            ),
        ),
        "cache_status": {
            "disposition": disposition,
            "entry_id": entry_id,
            "age_seconds": age_seconds,
            "ttl_seconds": ttl_seconds,
            "served_stale": disposition == "stale_hit",
            "source_contract_version": "3.0",
            "origin_execution_id": material.get("origin_execution_id"),
        },
        "limits_applied": dict(material.get("limits_applied") or {}),
        "stored_content": list(material.get("stored_content") or []),
        "dedup_clusters": list(material.get("dedup_clusters") or []),
        "warnings": list(material.get("warnings") or []),
    }
    if material.get("engine") is not None:
        payload["engine"] = material["engine"]
    if material.get("error") is not None:
        payload["error"] = material["error"]
    return payload


def legacy_payload_from_cache_material(material: Dict[str, Any]) -> Dict[str, Any]:
    """Create a source-only legacy projection without storing legacy payload bytes."""
    capability = str(material.get("capability") or "search")
    hints_by_url = {
        str(item.get("url")): item
        for item in material.get("legacy_projection_hints") or []
        if isinstance(item, dict) and item.get("url")
    }
    results = []
    for item in material.get("projection") or []:
        title = item.get("title")
        snippet = item.get("snippet")
        text = item.get("text")
        legacy_item = {
            "title": title.get("text") if isinstance(title, dict) else None,
            "url": (item.get("url") or {}).get("observed"),
        }
        if capability == "extract":
            content = (
                text.get("text") if isinstance(text, dict) else None
            )
            legacy_item["content"] = content
            hint = hints_by_url.get(str(legacy_item["url"])) or {}
            if hint.get("raw_content_alias") is True:
                legacy_item["raw_content"] = content
            if isinstance(hint.get("provider"), str):
                legacy_item["provider"] = hint["provider"]
            for scalar_field in ("favicon", "published_date"):
                if isinstance(hint.get(scalar_field), str):
                    legacy_item[scalar_field] = hint[scalar_field]
        else:
            legacy_item["snippet"] = (
                snippet.get("text")
                if isinstance(snippet, dict)
                else text.get("text")
                if isinstance(text, dict)
                else None
            )
        results.append(legacy_item)
    legacy = {
        "provider": material.get("origin_provider"),
        "results": results,
        "cached": True,
    }
    routing_hint = material.get("legacy_routing_hint")
    if isinstance(routing_hint, dict):
        legacy["routing"] = dict(routing_hint)
    return legacy


def derive_cache_key(
    request: RequestV3, *, vary: Optional[Dict[str, Any]] = None
) -> str:
    """Hash execution semantics, deliberately excluding request/cache policy IDs."""
    extraction_identity = _extraction_identity_from_vary(vary)
    if extraction_identity is not None:
        # The typed form already contains every extraction input that affects
        # evidence. Unlike legacy v3 keys, this is the complete SHA-256.
        return f"extract_{extraction_identity.key}"
    material = {
        "contract_version": request.contract_version,
        "capability": request.capability.value,
        "input": request.input,
        "options": request.options,
        "routing": request.routing,
        "budget": request.budget,
    }
    if vary:
        material["vary"] = vary
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    return f"{request.capability.value}_{digest}"


def _extraction_identity_from_vary(
    vary: Optional[Dict[str, Any]],
) -> ExtractionCacheIdentityV3 | None:
    if not vary or _EXTRACTION_IDENTITY_VARY_KEY not in vary:
        return None
    if set(vary) != {_EXTRACTION_IDENTITY_VARY_KEY}:
        raise ValueError("typed extraction cache identity cannot have extra vary fields")
    raw_identity = vary[_EXTRACTION_IDENTITY_VARY_KEY]
    if not isinstance(raw_identity, dict):
        raise ValueError("typed extraction cache identity must be an object")
    return ExtractionCacheIdentityV3.from_canonical_form(raw_identity)


class ResponseCacheV3:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.response_root = self.root / "v3" / "response"

    def path_for(
        self, request: RequestV3, *, vary: Optional[Dict[str, Any]] = None
    ) -> Path:
        entry_id = derive_cache_key(request, vary=vary)
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
    def _read_for_lookup(
        path: Path,
        *,
        expected_entry_id: str,
        extraction_identity: ExtractionCacheIdentityV3 | None,
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        """Classify a candidate without ever treating malformed data as a hit."""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "missing", None
        except (OSError, ValueError, UnicodeError):
            return "corrupt", None
        if not isinstance(value, dict):
            return "corrupt", None
        if value.get("owner") != CACHE_OWNER:
            return "foreign", None
        if not ResponseCacheV3._owned_envelope(value):
            return "corrupt", None
        if value.get("entry_id") != expected_entry_id:
            return "corrupt", None
        if extraction_identity is None:
            return "valid", value

        identity_version = value.get("identity_version")
        if (
            isinstance(identity_version, bool)
            or not isinstance(identity_version, int)
            or identity_version != EXTRACTION_CACHE_IDENTITY_VERSION
        ):
            # Previous/unknown identity versions are intact historical cache
            # data, not corruption. They are never reinterpreted.
            return "identity_miss", None
        raw_identity = value.get("identity")
        if not isinstance(raw_identity, dict):
            return "corrupt", None
        nested_version = raw_identity.get("identity_version")
        if (
            isinstance(nested_version, bool)
            or not isinstance(nested_version, int)
        ):
            return "corrupt", None
        if nested_version != EXTRACTION_CACHE_IDENTITY_VERSION:
            return "identity_miss", None
        try:
            stored_identity = ExtractionCacheIdentityV3.from_canonical_form(
                raw_identity
            )
        except ValueError:
            return "corrupt", None
        if stored_identity.canonical_form() != extraction_identity.canonical_form():
            return "corrupt", None
        return "valid", value

    def _quarantine(self, path: Path) -> None:
        """Move unreadable owned-path bytes aside without overwriting evidence."""
        try:
            data = path.read_bytes()
        except OSError:
            return
        digest = hashlib.sha256(data).hexdigest()[:16]
        capability = path.parent.name
        quarantine = self.response_root / "quarantine" / capability
        try:
            quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return
        target = quarantine / f"{path.stem}.{digest}.corrupt.json"
        suffix = 1
        while target.exists():
            try:
                if target.read_bytes() == data:
                    path.unlink(missing_ok=True)
                    return
            except OSError:
                return
            target = quarantine / f"{path.stem}.{digest}.{suffix}.corrupt.json"
            suffix += 1
        try:
            os.replace(path, target)
        except OSError:
            return

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
        vary: Optional[Dict[str, Any]] = None,
    ) -> str:
        extraction_identity = _extraction_identity_from_vary(vary)
        entry_id = derive_cache_key(request, vary=vary)
        envelope = {
            "owner": CACHE_OWNER,
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "contract_version": "3.0",
            "entry_id": entry_id,
            "created_at": int(now),
            "payload": cache_material_from_response(
                payload, legacy_payload=legacy_payload
            ),
        }
        if extraction_identity is not None:
            envelope.update(
                {
                    "identity_version": extraction_identity.identity_version,
                    "identity": extraction_identity.canonical_form(),
                }
            )
        self._atomic_write(self.path_for(request, vary=vary), envelope)
        return entry_id

    def get(
        self,
        request: RequestV3,
        *,
        ttl_seconds: int,
        allow_stale_seconds: int,
        now: int,
        vary: Optional[Dict[str, Any]] = None,
    ) -> CacheLookupV3:
        path = self.path_for(request, vary=vary)
        entry_id = derive_cache_key(request, vary=vary)
        extraction_identity = _extraction_identity_from_vary(vary)
        read_status, envelope = self._read_for_lookup(
            path,
            expected_entry_id=entry_id,
            extraction_identity=extraction_identity,
        )
        if read_status == "corrupt":
            self._quarantine(path)
            return CacheLookupV3("miss")
        if envelope is None:
            return CacheLookupV3("miss")
        created_at = envelope.get("created_at")
        if not isinstance(created_at, int):
            self._quarantine(path)
            return CacheLookupV3("miss")
        age = max(0, int(now) - created_at)
        entry_id = str(envelope["entry_id"])
        if age <= max(0, int(ttl_seconds)):
            return CacheLookupV3(
                "fresh_hit",
                dict(envelope["payload"]),
                entry_id,
                age,
                "3.0",
                legacy_payload_from_cache_material(envelope["payload"]),
            )
        if age <= max(0, int(ttl_seconds)) + max(0, int(allow_stale_seconds)):
            return CacheLookupV3(
                "stale_hit",
                dict(envelope["payload"]),
                entry_id,
                age,
                "3.0",
                legacy_payload_from_cache_material(envelope["payload"]),
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
