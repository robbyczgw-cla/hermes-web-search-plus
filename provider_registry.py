"""Central provider registry with additive ``providers.d`` discovery.

Built-ins remain the compatibility baseline.  At startup, self-contained
provider modules in ``providers.d`` contribute one ``PROVIDER`` declaration;
their formal execute callables are consumed by :mod:`provider_dispatch`.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from pathlib import Path
from typing import Dict, Iterable

from provider_adapter_protocol import (
    SEARCH_ADAPTER_PARAMETERS as _SEARCH_PARAMETERS,
    EXTRACT_ADAPTER_PARAMETERS as _EXTRACT_PARAMETERS,
    _signature_matches,
)

from wsp_sdk import (
    DuplicateProviderError,
    ProviderRegistrationError,
    ProviderSpec,
    ProviderStartupDiagnostic,
)


PROVIDERS_DIRECTORY = Path(__file__).resolve().with_name("providers.d")
NON_PRODUCTION_DISCOVERY_ENV = "WSP_SDK_ALLOW_NON_PRODUCTION"


def _non_production_discovery_allowed() -> bool:
    value = os.environ.get(NON_PRODUCTION_DISCOVERY_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_VAR_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


# These records retain every pre-SDK default explicitly.  Discovered providers
# default to explicit-only (``auto_allowed_by_default=False``) in ProviderSpec.
_BUILTIN_PROVIDER_SPECS = (
    ProviderSpec(
        provider="serper", env_var="SERPER_API_KEY", display_name="Serper",
        description="Google-like SERP results for facts, shopping, local and news queries, plus webpage scraping.",
        config_section="serper", supports_search=True, supports_extract=True,
        capability_labels=("search", "news", "shopping", "local", "extract"),
        auto_allowed_by_default=True, free_tier="2,500 one-time credits",
        signup_url="https://serper.dev/api-key",
    ),
    ProviderSpec(
        provider="serpbase", env_var="SERPBASE_API_KEY", display_name="SerpBase",
        description="Cheap Google-like SERP fallback; WSP exposes search only, explicit/fallback-only by default.",
        config_section="serpbase", supports_search=True, supports_extract=False,
        capability_labels=("search",), auto_allowed_by_default=False,
        free_tier="100 free searches, paid packs available", signup_url="https://www.serpbase.dev",
        upstream_capabilities=("images", "news", "videos", "maps_search", "maps_detail"),
    ),
    ProviderSpec(
        provider="brave", env_var="BRAVE_API_KEY", display_name="Brave Search",
        description="Independent general web index in the Routing v2 default pool.",
        config_section="brave", supports_search=True, supports_extract=False,
        capability_labels=("search", "news", "local"), auto_allowed_by_default=True,
        free_tier="$5 free monthly credits", signup_url="https://api.search.brave.com/app/keys",
    ),
    ProviderSpec(
        provider="tavily", env_var="TAVILY_API_KEY", display_name="Tavily",
        description="Research/tutorial provider in the Routing v2 default pool.",
        config_section="tavily", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract", "research"), auto_allowed_by_default=True,
        recommended=True, free_tier="1,000 free searches/month", signup_url="https://tavily.com",
    ),
    ProviderSpec(
        provider="querit", env_var="QUERIT_API_KEY", display_name="Querit",
        description="Multilingual and real-time search candidate.", config_section="querit",
        supports_search=True, supports_extract=False, capability_labels=("search", "multilingual"),
        auto_allowed_by_default=False, free_tier="1,000 free searches/month",
        signup_url="https://www.querit.ai",
    ),
    ProviderSpec(
        provider="linkup", env_var="LINKUP_API_KEY", display_name="Linkup",
        description="Best starter for cheap clean extraction and citation-grounded retrieval.",
        config_section="linkup", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract", "citations"), auto_allowed_by_default=True,
        recommended=True, free_tier="€5 free monthly credits (~5,000 standard extracts)",
        signup_url="https://www.linkup.so",
    ),
    ProviderSpec(
        provider="exa", env_var="EXA_API_KEY", display_name="Exa",
        description="Semantic discovery, alternatives, docs, academic and long-form discovery.",
        config_section="exa", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract", "semantic"), auto_allowed_by_default=True,
        free_tier="1,000 free searches/month", signup_url="https://dashboard.exa.ai/api-keys",
    ),
    ProviderSpec(
        provider="firecrawl", env_var="FIRECRAWL_API_KEY", display_name="Firecrawl",
        description="Robust scraping/extraction fallback, especially for JS-heavy pages.",
        config_section="firecrawl", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract", "js"), auto_allowed_by_default=True,
        free_tier="500 one-time credits", signup_url="https://www.firecrawl.dev/app/api-keys",
    ),
    ProviderSpec(
        provider="parallel", env_var="PARALLEL_API_KEY", display_name="Parallel",
        description="LLM-ready web search and fast URL extraction with long source excerpts.",
        config_section="parallel", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract", "citations"), auto_allowed_by_default=True,
        signup_url="https://platform.parallel.ai",
    ),
    ProviderSpec(
        provider="perplexity", env_var="PERPLEXITY_API_KEY", display_name="Perplexity",
        description="Rejected legacy answer endpoint; no source-only mode is registered.",
        config_section="perplexity", supports_search=False, supports_extract=False,
        capability_labels=(), auto_allowed_by_default=False,
        signup_url="https://www.perplexity.ai/settings/api",
        rejected_reason="no_verified_source_only_endpoint",
    ),
    ProviderSpec(
        provider="kilo-perplexity", env_var="KILOCODE_API_KEY", display_name="Kilo Code Perplexity bridge",
        description="Rejected legacy answer bridge; no source-only mode is registered.",
        config_section="kilo-perplexity", supports_search=False, supports_extract=False,
        capability_labels=(), auto_allowed_by_default=False, free_tier="Depends on Kilo account",
        signup_url="https://kilo.ai", rejected_reason="no_verified_source_only_endpoint",
    ),
    ProviderSpec(
        provider="you", env_var="YOU_API_KEY", display_name="You.com",
        description="Fast Routing v2 core provider for current, multilingual, and LLM-ready search.",
        config_section="you", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract"), auto_allowed_by_default=True,
        recommended=True, free_tier="Limited/API key required", signup_url="https://api.you.com",
    ),
    ProviderSpec(
        provider="searxng", env_var="SEARXNG_INSTANCE_URL", display_name="SearXNG",
        description="Self-hosted/privacy-preserving metasearch instance URL.",
        config_section="searxng", supports_search=True, supports_extract=False,
        capability_labels=("search", "self-hosted"), auto_allowed_by_default=True,
        free_tier="Free if self-hosted", signup_url="https://docs.searxng.org/admin/installation.html",
    ),
    ProviderSpec(
        provider="keenable", env_var="KEENABLE_API_KEY", display_name="Keenable",
        description="Independent web index for search and extraction; works keyless, optional key raises rate limits.",
        config_section="keenable", supports_search=True, supports_extract=True,
        capability_labels=("search", "extract"), auto_allowed_by_default=True,
        free_tier="Keyless public tier; optional key for higher limits", signup_url="https://keenable.ai",
        keyless=True,
    ),
)

_BUILTIN_EXTRACT_PROVIDER_IDS = (
    "tavily", "exa", "linkup", "parallel", "firecrawl", "you", "keenable", "serper",
)
_BUILTIN_DEFAULT_PROVIDER_PRIORITY = (
    "you", "serper", "exa", "firecrawl", "tavily", "linkup", "brave", "parallel",
    "serpbase", "querit", "searxng", "keenable",
)
_provider_specs = list(_BUILTIN_PROVIDER_SPECS)
_discovered_provider_ids: set[str] = set()

PROVIDER_SPECS: Dict[str, ProviderSpec] = {}
SEARCH_PROVIDER_IDS: tuple[str, ...] = ()
EXTRACT_PROVIDER_IDS: tuple[str, ...] = ()
DEFAULT_PROVIDER_PRIORITY: tuple[str, ...] = ()
DEFAULT_AUTO_ALLOW: dict[str, bool] = {}
PROVIDER_ENV_KEYS: tuple[str, ...] = ()
EXTRACT_PROVIDER_ENV_KEYS: tuple[str, ...] = ()
KEYLESS_PROVIDER_IDS: tuple[str, ...] = ()
KEYLESS_EXTRACT_PROVIDER_IDS: tuple[str, ...] = ()
PROVIDER_STARTUP_DIAGNOSTICS: tuple[ProviderStartupDiagnostic, ...] = ()


def _refresh_derived_surfaces() -> None:
    """Refresh registry-derived read-only views after one successful registration."""
    global SEARCH_PROVIDER_IDS, EXTRACT_PROVIDER_IDS, DEFAULT_PROVIDER_PRIORITY
    global DEFAULT_AUTO_ALLOW, PROVIDER_ENV_KEYS, EXTRACT_PROVIDER_ENV_KEYS
    global KEYLESS_PROVIDER_IDS, KEYLESS_EXTRACT_PROVIDER_IDS

    PROVIDER_SPECS.clear()
    PROVIDER_SPECS.update((spec.provider, spec) for spec in _provider_specs)
    discovered = [spec for spec in _provider_specs if spec.provider in _discovered_provider_ids]
    SEARCH_PROVIDER_IDS = tuple(
        spec.provider for spec in _BUILTIN_PROVIDER_SPECS if spec.supports_search
    ) + tuple(spec.provider for spec in discovered if spec.supports_search)
    EXTRACT_PROVIDER_IDS = _BUILTIN_EXTRACT_PROVIDER_IDS + tuple(
        spec.provider for spec in discovered if spec.supports_extract
    )
    DEFAULT_PROVIDER_PRIORITY = _BUILTIN_DEFAULT_PROVIDER_PRIORITY + tuple(
        spec.provider
        for spec in discovered
        if spec.supports_search and spec.auto_allowed_by_default
    )
    DEFAULT_AUTO_ALLOW = {
        spec.provider: False
        for spec in _provider_specs
        if spec.supports_search and not spec.auto_allowed_by_default
    }
    PROVIDER_ENV_KEYS = tuple(spec.env_var for spec in _provider_specs)
    EXTRACT_PROVIDER_ENV_KEYS = tuple(
        spec.env_var for spec in _provider_specs if spec.supports_extract
    )
    KEYLESS_PROVIDER_IDS = tuple(spec.provider for spec in _provider_specs if spec.keyless)
    KEYLESS_EXTRACT_PROVIDER_IDS = tuple(
        spec.provider for spec in _provider_specs if spec.keyless and spec.supports_extract
    )


def _validate_discovered_spec(spec: ProviderSpec) -> None:
    if not isinstance(spec, ProviderSpec):
        raise ProviderRegistrationError("PROVIDER must be a ProviderSpec")
    if not _PROVIDER_ID_PATTERN.fullmatch(spec.provider):
        raise ProviderRegistrationError("provider id must use lowercase letters, digits, and hyphens")
    if not _ENV_VAR_PATTERN.fullmatch(spec.env_var):
        raise ProviderRegistrationError("env_var must be a non-empty uppercase environment variable name")
    if not all((spec.display_name, spec.description, spec.config_section, spec.signup_url)):
        raise ProviderRegistrationError("display_name, description, config_section, and signup_url are required")
    if spec.supports_search and not _signature_matches(spec.execute_search, _SEARCH_PARAMETERS):
        raise ProviderRegistrationError("search provider must define a conforming execute_search callable")
    if spec.supports_extract and not _signature_matches(spec.execute_extract, _EXTRACT_PARAMETERS):
        raise ProviderRegistrationError("extract provider must define a conforming execute_extract callable")
    if spec.execute_search is not None and not spec.supports_search:
        raise ProviderRegistrationError("execute_search requires kind=search or kind=both")
    if spec.execute_extract is not None and not spec.supports_extract:
        raise ProviderRegistrationError("execute_extract requires kind=extract or kind=both")
    if spec.auto_allowed_by_default and not spec.supports_search:
        raise ProviderRegistrationError("only search providers may opt into the auto pool")


def _statically_non_production(path: Path) -> bool:
    """Detect ``production=False`` without importing (and thus executing) the module.

    Non-production fixtures must declare the flag as a literal keyword so the
    gate can act before any module code runs.  A module whose flag cannot be
    read statically is treated as production here; the post-import gate in
    :func:`discover_providers` remains as the authoritative backstop.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "production"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            ):
                return True
    return False


def _load_provider_file(path: Path) -> ProviderSpec:
    module_name = "wsp_provider_" + re.sub(r"[^a-zA-Z0-9_]", "_", path.stem)
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ProviderRegistrationError("module_load_failed")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    if not hasattr(module, "PROVIDER"):
        raise ProviderRegistrationError("missing_PROVIDER")
    provider = module.PROVIDER
    if not isinstance(provider, ProviderSpec):
        raise ProviderRegistrationError("invalid_PROVIDER")
    return provider


def discover_providers(
    directory: Path | str | None = None,
    *,
    existing_ids: Iterable[str] = (),
) -> tuple[tuple[ProviderSpec, ...], tuple[ProviderStartupDiagnostic, ...]]:
    """Load provider declarations without mutating the live registry.

    Broken modules are returned as typed diagnostics and excluded.  A duplicate
    id is a deliberate security/configuration conflict and raises
    :class:`DuplicateProviderError` instead of allowing an import-order winner.
    """
    root = Path(directory) if directory is not None else PROVIDERS_DIRECTORY
    if not root.exists():
        return (), ()
    seen = set(existing_ids)
    discovered = []
    diagnostics = []
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("_"):
            continue
        if not _non_production_discovery_allowed() and _statically_non_production(path):
            # Skip before import: non-production fixture code must not execute
            # in a production process at all, not merely stay unregistered.
            continue
        try:
            spec = _load_provider_file(path)
            _validate_discovered_spec(spec)
            if spec.provider in seen:
                raise DuplicateProviderError(f"duplicate provider id: {spec.provider}")
        except DuplicateProviderError:
            raise
        except ProviderRegistrationError as exc:
            diagnostics.append(ProviderStartupDiagnostic(path.stem, str(exc)))
            continue
        except Exception:
            diagnostics.append(ProviderStartupDiagnostic(path.stem, "module_load_failed"))
            continue
        if not spec.production and not _non_production_discovery_allowed():
            # Non-production specs (e.g. the SDK example fixture) must never
            # widen the default provider surface; they are discoverable only
            # behind an explicit operator/test opt-in.
            continue
        seen.add(spec.provider)
        discovered.append(spec)
    return tuple(discovered), tuple(diagnostics)


def register_provider(spec: ProviderSpec) -> ProviderSpec:
    """Register one already-validated provider spec into this process registry.

    This is primarily a test/integration seam.  Normal provider modules are
    registered during startup discovery through their module-level ``PROVIDER``.
    """
    _validate_discovered_spec(spec)
    if spec.provider in PROVIDER_SPECS:
        raise DuplicateProviderError(f"duplicate provider id: {spec.provider}")
    _provider_specs.append(spec)
    _discovered_provider_ids.add(spec.provider)
    _refresh_derived_surfaces()
    return spec


_refresh_derived_surfaces()
_discovered_specs, _startup_diagnostics = discover_providers(existing_ids=PROVIDER_SPECS)
for _spec in _discovered_specs:
    register_provider(_spec)
PROVIDER_STARTUP_DIAGNOSTICS = _startup_diagnostics


def startup_diagnostics() -> tuple[ProviderStartupDiagnostic, ...]:
    """Return safe typed diagnostics for provider modules excluded at startup."""
    return PROVIDER_STARTUP_DIAGNOSTICS


def keyless_public_env_var(provider: str) -> str:
    return f"{PROVIDER_SPECS[provider].config_section.upper()}_ALLOW_PUBLIC"


def doctor_catalog() -> Dict[str, Dict[str, object]]:
    """Return the legacy doctor JSON metadata shape from the complete registry."""
    return {
        provider: {
            "env_var": spec.env_var,
            "search_capable": spec.supports_search,
            "extract_capable": spec.supports_extract,
        }
        for provider, spec in PROVIDER_SPECS.items()
    }


def plugin_catalog() -> list[Dict[str, object]]:
    """Return setup/onboarding provider metadata from the complete registry."""
    catalog = []
    for spec in PROVIDER_SPECS.values():
        item: Dict[str, object] = {
            "provider": spec.provider,
            "env": spec.env_var,
            "display_name": spec.display_name,
            "description": spec.description,
            "free_tier": spec.free_tier,
            "signup_url": spec.signup_url,
            "capabilities": list(spec.capability_labels),
            "recommended": spec.recommended,
            "production": spec.production,
            "freshness_supported": spec.supports_freshness,
        }
        if spec.upstream_capabilities:
            item["upstream_capabilities"] = list(spec.upstream_capabilities)
        catalog.append(item)
    return catalog
