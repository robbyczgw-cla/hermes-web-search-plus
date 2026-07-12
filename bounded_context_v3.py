"""WS-2 bounded-context policy and owned full-text retention for WSP v3."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Protocol

from contract_v3 import Capability, RequestV3, ResponseStatus, ResponseV3
from runtime_v3 import segment_canonical_text

DEFAULT_MAX_URLS = 10
HARD_MAX_URLS = 50
DEFAULT_MAX_CONTEXT_CHARS = 60_000
MIN_CONTEXT_CHARS = 1_000
MAX_CONTEXT_CHARS = 200_000
DEFAULT_FULL_TEXT_TTL_SECONDS = 604_800
DEFAULT_FULL_TEXT_MAX_BYTES = 268_435_456
STORE_NAME = "web_text_v3"
_MEDIA_TYPE = "text/markdown"
_OWNED_MARKER = "<!-- wsp:web_text_v3 "
_KEY_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class ExtractContextPlan:
    request: RequestV3
    processed_urls: List[str]
    omitted_urls: List[str]
    max_urls: int
    max_context_chars: int


class ContentStore(Protocol):
    def store(self, url: str, text: str) -> Dict[str, Any]: ...


def _integer_option(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def prepare_extract_request(
    request: RequestV3, config: Mapping[str, Any]
) -> ExtractContextPlan:
    """Apply operator ceilings before any extraction provider sees the request."""
    if request.capability is not Capability.EXTRACT:
        raise ValueError("bounded extract planning requires capability=extract")
    policy = config.get("bounded_context") or {}
    if not isinstance(policy, Mapping):
        policy = {}

    requested_max_urls = _integer_option(
        request.options.get("max_urls"), "max_urls", DEFAULT_MAX_URLS
    )
    operator_max_urls = _integer_option(
        policy.get("max_urls"), "bounded_context.max_urls", DEFAULT_MAX_URLS
    )
    max_urls = min(
        max(1, requested_max_urls), max(1, operator_max_urls), HARD_MAX_URLS
    )

    operator_default_chars = _integer_option(
        policy.get("max_context_chars"),
        "bounded_context.max_context_chars",
        DEFAULT_MAX_CONTEXT_CHARS,
    )
    requested_chars = _integer_option(
        request.options.get("max_context_chars"),
        "max_context_chars",
        operator_default_chars,
    )
    max_context_chars = min(
        MAX_CONTEXT_CHARS, max(MIN_CONTEXT_CHARS, requested_chars)
    )

    urls = list(request.input["urls"])
    processed_urls = urls[:max_urls]
    omitted_urls = urls[max_urls:]
    options = dict(request.options)
    options["max_urls"] = max_urls
    options["max_context_chars"] = max_context_chars
    bounded_request = RequestV3(
        capability=request.capability,
        input={**request.input, "urls": processed_urls},
        request_id=request.request_id,
        options=options,
        cache=dict(request.cache),
        routing=dict(request.routing),
        budget=dict(request.budget),
        client=dict(request.client),
        contract_version=request.contract_version,
    )
    return ExtractContextPlan(
        request=bounded_request,
        processed_urls=processed_urls,
        omitted_urls=omitted_urls,
        max_urls=max_urls,
        max_context_chars=max_context_chars,
    )


def _fair_share_allocations(lengths: List[int], budget: int) -> List[int]:
    """Deterministic water-filling with stable-order remainder assignment."""
    if not lengths:
        return []
    if sum(lengths) <= budget:
        return list(lengths)
    allocations = [0] * len(lengths)
    active = list(range(len(lengths)))
    remaining = budget
    while active and remaining > 0:
        share, remainder = divmod(remaining, len(active))
        satisfied = [
            index for index in active if lengths[index] - allocations[index] <= share
        ]
        if satisfied:
            for index in satisfied:
                need = lengths[index] - allocations[index]
                allocations[index] += need
                remaining -= need
            active = [index for index in active if index not in satisfied]
            continue
        for position, index in enumerate(active):
            grant = share + (1 if position < remainder else 0)
            allocations[index] += grant
            remaining -= grant
        break
    return allocations


def _warning(code: str, message: str, details: Dict[str, Any]) -> Dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _append_warning(
    warnings: List[Dict[str, Any]], code: str, message: str, details: Dict[str, Any]
) -> None:
    if not any(item.get("code") == code for item in warnings):
        warnings.append(_warning(code, message, details))


def apply_bounded_context(
    response: ResponseV3,
    original_request: RequestV3,
    plan: ExtractContextPlan,
    *,
    store: ContentStore,
) -> ResponseV3:
    """Bound inline extracted text without destroying full source observations."""
    results = deepcopy(response.results)
    policy_actions = deepcopy(response.policy_actions)
    warnings = deepcopy(response.warnings)
    stored_content: List[Dict[str, Any]] = []

    ordered_positions = sorted(
        range(len(results)),
        key=lambda index: (
            int(results[index].get("engine_rank") or index + 1),
            next(
                (
                    int(observation.get("provider_result_index") or 0)
                    for observation in response.observations
                    if observation.get("observation_id")
                    == results[index].get("representative_observation_id")
                ),
                0,
            ),
        ),
    )
    content_positions = [
        index
        for index in ordered_positions
        if isinstance((results[index].get("text") or {}).get("text"), str)
    ]
    lengths = [len(results[index]["text"]["text"]) for index in content_positions]
    allocations = _fair_share_allocations(lengths, plan.max_context_chars)
    truncated_count = 0

    for position, allocation in zip(content_positions, allocations):
        projected = results[position]["text"]
        full_text = unicodedata.normalize("NFC", projected["text"])
        if len(full_text) <= allocation:
            continue
        truncated_count += 1
        inline_text = full_text[:allocation]
        projected["text"] = inline_text
        projected["text_sha256"] = hashlib.sha256(
            inline_text.encode("utf-8")
        ).hexdigest()
        transformations = list(projected["provenance"]["transformations"])
        if "deterministic_truncation" not in transformations:
            transformations.append("deterministic_truncation")
        projected["provenance"]["transformations"] = transformations
        projected["segments"] = segment_canonical_text(inline_text)

        observation_id = results[position]["representative_observation_id"]
        url = results[position]["url"]["canonical"]
        storage = dict(store.store(url, full_text))
        storage["observation_id"] = observation_id
        stored_content.append(storage)
        policy_actions.append(
            {
                "action": "truncated_by_limit",
                "observation_id": observation_id,
                "reason": "max_context_chars",
            }
        )
        if not storage.get("storage_succeeded"):
            _append_warning(
                warnings,
                "wsp.storage.full_text_unavailable",
                "Full extracted content could not be retained for page-on-demand access.",
                {"observation_id": observation_id},
            )

    if plan.omitted_urls:
        _append_warning(
            warnings,
            "wsp.extract.urls_omitted",
            "One or more requested URLs were omitted by the extraction fan-out cap.",
            {"omitted_url_count": len(plan.omitted_urls)},
        )
    if truncated_count:
        _append_warning(
            warnings,
            "wsp.content.truncated",
            "Inline extracted content was deterministically truncated to the call budget.",
            {"truncated_result_count": truncated_count},
        )

    context_chars_returned = sum(
        len(result["text"]["text"])
        for result in results
        if isinstance((result.get("text") or {}).get("text"), str)
    )
    limits_applied = deepcopy(response.limits_applied)
    limits_applied["extract"] = {
        "requested_url_count": len(original_request.input["urls"]),
        "processed_urls": list(plan.processed_urls),
        "omitted_urls": list(plan.omitted_urls),
        "omitted_url_count": len(plan.omitted_urls),
        "max_urls": plan.max_urls,
        "max_context_chars": plan.max_context_chars,
        "context_chars_returned": context_chars_returned,
        "truncated": bool(truncated_count),
    }
    status = response.status
    if status is not ResponseStatus.FAILED and (plan.omitted_urls or truncated_count):
        status = ResponseStatus.DEGRADED

    return replace(
        response,
        status=status,
        results=results,
        policy_actions=policy_actions,
        limits_applied=limits_applied,
        stored_content=stored_content,
        warnings=warnings,
    )


def _atomic_write_owned(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".wsp-v3-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class FullTextStore:
    """Age/size-bounded storage that owns only marked web_text_v3 entries."""

    def __init__(
        self,
        cache_root: Path,
        *,
        ttl_seconds: int = DEFAULT_FULL_TEXT_TTL_SECONDS,
        max_bytes: int = DEFAULT_FULL_TEXT_MAX_BYTES,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.web_dir = self.cache_root / "web"
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_bytes = max(0, int(max_bytes))
        self.now = now

    def path_for_key(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError("invalid web_text_v3 key")
        return self.web_dir / f"{key}.md"

    def _owned(self, path: Path) -> bool:
        if not _KEY_RE.fullmatch(path.stem) or path.suffix != ".md":
            return False
        try:
            with path.open("r", encoding="utf-8") as handle:
                return handle.readline().startswith(_OWNED_MARKER)
        except (OSError, UnicodeError):
            return False

    def _owned_files(self) -> Iterable[Path]:
        if not self.web_dir.exists():
            return []
        return [path for path in self.web_dir.glob("*.md") if self._owned(path)]

    def store(self, url: str, text: str) -> Dict[str, Any]:
        full_text = unicodedata.normalize("NFC", text)
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        metadata = json.dumps(
            {
                "version": 1,
                "key": key,
                "sha256": digest,
                "chars": len(full_text),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        path = self.path_for_key(key)
        try:
            _atomic_write_owned(path, f"{_OWNED_MARKER}{metadata} -->\n{full_text}")
            self.cleanup_orphans()
            self.enforce_retention()
            if not path.exists():
                raise OSError("entry exceeds configured retention bounds")
        except OSError:
            return {
                "storage_attempted": True,
                "storage_succeeded": False,
                "reference": None,
                "full_text_sha256": None,
                "full_text_chars": None,
            }
        return {
            "storage_attempted": True,
            "storage_succeeded": True,
            "reference": {
                "store": STORE_NAME,
                "key": key,
                "media_type": _MEDIA_TYPE,
            },
            "full_text_sha256": digest,
            "full_text_chars": len(full_text),
        }

    def lookup(self, key: str) -> str | None:
        path = self.path_for_key(key)
        try:
            stat = path.stat()
            if self.now() - stat.st_mtime > self.ttl_seconds:
                path.unlink(missing_ok=True)
                return None
            if not self._owned(path):
                return None
            payload = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        first, separator, text = payload.partition("\n")
        if not separator or not first.startswith(_OWNED_MARKER):
            return None
        return text

    def cleanup_orphans(self) -> Dict[str, int]:
        removed = 0
        errors = 0
        if not self.web_dir.exists():
            return {"orphan_temps_removed": 0, "errors": 0}
        for path in self.web_dir.glob(".wsp-v3-*.tmp"):
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                errors += 1
        return {"orphan_temps_removed": removed, "errors": errors}

    def enforce_retention(self) -> Dict[str, int]:
        ttl_evicted = 0
        size_evicted = 0
        errors = 0
        now = self.now()
        entries: List[tuple[float, str, int, Path]] = []
        for path in self._owned_files():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            except OSError:
                errors += 1
                continue
            if now - stat.st_mtime > self.ttl_seconds:
                try:
                    path.unlink(missing_ok=True)
                    ttl_evicted += 1
                except OSError:
                    errors += 1
                continue
            entries.append((stat.st_mtime, path.name, stat.st_size, path))

        total = sum(entry[2] for entry in entries)
        for _mtime, _name, size, path in sorted(entries):
            if total <= self.max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total -= size
                size_evicted += 1
            except OSError:
                errors += 1
        return {
            "ttl_evicted": ttl_evicted,
            "size_evicted": size_evicted,
            "errors": errors,
            "owned_bytes": total,
        }
