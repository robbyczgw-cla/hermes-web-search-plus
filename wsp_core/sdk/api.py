"""Stable provider-spec and source-envelope helpers for WSP 3.x."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .errors import ProviderRegistrationError


SearchExecute = Callable[[Any, str, Any, str | None, Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
ExtractExecute = Callable[
    [Any, str, list[str], str | None, str, bool, bool, bool, Mapping[str, Any], bool],
    dict[str, Any],
]


@dataclass(frozen=True, init=False)
class ProviderSpec:
    """A stable description of one provider module.

    ``id``/``kind`` are the SDK spelling.  ``provider`` and the capability
    booleans remain accepted for built-in compatibility during the 3.x line.
    New provider modules should use ``id`` and ``kind`` plus the matching
    ``execute_search`` and/or ``execute_extract`` callable.
    """

    provider: str
    env_var: str
    display_name: str
    description: str
    config_section: str
    supports_search: bool
    supports_extract: bool
    capability_labels: tuple[str, ...]
    auto_allowed_by_default: bool
    recommended: bool
    free_tier: str
    signup_url: str
    upstream_capabilities: tuple[str, ...]
    keyless: bool
    search_output_semantics: str | None
    extract_output_semantics: str | None
    provider_fields_allowlist: tuple[str, ...]
    rejected_reason: str | None
    execute_search: SearchExecute | None
    execute_extract: ExtractExecute | None
    supports_freshness: bool
    production: bool
    kind: str

    def __init__(
        self,
        provider: str | None = None,
        env_var: str = "",
        display_name: str = "",
        description: str = "",
        config_section: str = "",
        supports_search: bool | None = None,
        supports_extract: bool | None = None,
        capability_labels: Sequence[str] = (),
        auto_allowed_by_default: bool = False,
        recommended: bool = False,
        free_tier: str = "API key required",
        signup_url: str = "",
        upstream_capabilities: Sequence[str] = (),
        keyless: bool = False,
        search_output_semantics: str | None = "source_results",
        extract_output_semantics: str | None = "source_text",
        provider_fields_allowlist: Sequence[str] = (),
        rejected_reason: str | None = None,
        *,
        id: str | None = None,
        kind: str | None = None,
        execute_search: SearchExecute | None = None,
        execute_extract: ExtractExecute | None = None,
        supports_freshness: bool = False,
        freshness_supported: bool | None = None,
        production: bool = True,
    ) -> None:
        identifier = id if id is not None else provider
        if id is not None and provider is not None and id != provider:
            raise ProviderRegistrationError("provider id and provider must match")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ProviderRegistrationError("provider id is required")
        identifier = identifier.strip().lower()
        if kind is not None:
            if kind not in {"search", "extract", "both", "disabled"}:
                raise ProviderRegistrationError("kind must be search, extract, both, or disabled")
            kind_search = kind in {"search", "both"}
            kind_extract = kind in {"extract", "both"}
            if supports_search is not None and bool(supports_search) != kind_search:
                raise ProviderRegistrationError("kind conflicts with supports_search")
            if supports_extract is not None and bool(supports_extract) != kind_extract:
                raise ProviderRegistrationError("kind conflicts with supports_extract")
            supports_search = kind_search
            supports_extract = kind_extract
        search_enabled = bool(supports_search)
        extract_enabled = bool(supports_extract)
        resolved_kind = (
            "both" if search_enabled and extract_enabled else "search" if search_enabled
            else "extract" if extract_enabled else "disabled"
        )
        if freshness_supported is not None:
            supports_freshness = bool(freshness_supported)

        values = {
            "provider": identifier,
            "env_var": str(env_var),
            "display_name": str(display_name),
            "description": str(description),
            "config_section": str(config_section),
            "supports_search": search_enabled,
            "supports_extract": extract_enabled,
            "capability_labels": tuple(str(label) for label in capability_labels),
            "auto_allowed_by_default": bool(auto_allowed_by_default),
            "recommended": bool(recommended),
            "free_tier": str(free_tier),
            "signup_url": str(signup_url),
            "upstream_capabilities": tuple(str(label) for label in upstream_capabilities),
            "keyless": bool(keyless),
            "search_output_semantics": search_output_semantics,
            "extract_output_semantics": extract_output_semantics,
            "provider_fields_allowlist": tuple(str(item) for item in provider_fields_allowlist),
            "rejected_reason": rejected_reason,
            "execute_search": execute_search,
            "execute_extract": execute_extract,
            "supports_freshness": bool(supports_freshness),
            "production": bool(production),
            "kind": resolved_kind,
        }
        for field_name, value in values.items():
            object.__setattr__(self, field_name, value)
        self._validate_source_only_semantics()

    @property
    def id(self) -> str:
        """Stable SDK alias for the historical ``provider`` field."""
        return self.provider

    @property
    def freshness_supported(self) -> bool:
        """Alias kept for readable capability metadata."""
        return self.supports_freshness

    def _validate_source_only_semantics(self) -> None:
        allowed = {"source_results", "source_text"}
        if self.supports_search and self.search_output_semantics not in allowed:
            raise ProviderRegistrationError(f"{self.provider}: search mode is not source-only")
        if self.supports_extract and self.extract_output_semantics not in allowed:
            raise ProviderRegistrationError(f"{self.provider}: extract mode is not source-only")


def register_provider(spec: ProviderSpec) -> ProviderSpec:
    """Validate and return a module-level provider declaration.

    Discovery registers the returned ``PROVIDER`` declaration atomically after
    its module loads.  Keeping this helper side-effect-free lets the same
    self-contained provider module be imported safely by documentation and
    test tooling before the plugin registry is initialized.
    """
    if not isinstance(spec, ProviderSpec):
        raise ProviderRegistrationError("register_provider requires ProviderSpec")
    return spec


def source_result(url: str, **fields: Any) -> dict[str, Any]:
    """Build one source result item for a search or extraction envelope."""
    result = {"url": url}
    result.update(fields)
    return result


def search_result(
    provider: str,
    query: str,
    results: Sequence[Mapping[str, Any]],
    *,
    images: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a source-only search envelope accepted by WSP's protocol gate."""
    return {
        "provider": provider,
        "query": query,
        "results": [dict(item) for item in results],
        "images": [dict(item) for item in images] if images is not None else [],
        "metadata": dict(metadata) if metadata is not None else {},
    }


def extract_result(
    provider: str,
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a source-only extraction envelope accepted by WSP's protocol gate."""
    return {"provider": provider, "results": [dict(item) for item in results]}


# Explicit aliases make the constructors easy to discover without breaking the
# concise names in the 3.1 SDK guide.
make_search_result = search_result
make_extract_result = extract_result
