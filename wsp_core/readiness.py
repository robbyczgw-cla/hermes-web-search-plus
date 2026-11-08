"""Whether a provider can run, why not, and how to fix it: one answer for every caller.

Before 5.1.8 about thirty places answered "is provider X configured?" with their
own rules: the engine, the Hermes status command, the tools' availability check,
setup confirmations, the onboarding hint, the native backend and the MCP status.
Under Hermes they disagreed: ``setup --keyless-public`` reported "Enabled", the
tools were listed, and every search failed with "serper: Missing API key".

This module is the single source of truth. It does not re-implement credential
rules: the engine's own ``get_api_key`` / ``keyless_public_allowed`` decide
whether a value is accepted. Around that decision it records the state, the
source, a stable reason code, one sentence for people and models, and the
command that fixes it. Callers render it; they do not re-derive it.

States (``Readiness``):

- ``ready``: accepted value, automatic routing may use it.
- ``explicit_only``: accepted, but ``auto_allow`` is off; only ``provider="X"`` uses it.
- ``disabled``: accepted, but listed in ``auto_routing.disabled_providers``.
- ``not_configured``: no value anywhere this host reads.
- ``ignored_source``: a value exists only where this host does not read it
  (config.json under a Hermes profile resolver).
- ``invalid_value``: a value exists but cannot work (template placeholder,
  blocked SearXNG URL, missing DonSeTch binary).
- ``scope_error``: the host refused to read the credential.

Runtime admission (circuit breaker, cooldowns, budgets) is deliberately not part
of this: readiness answers "can this configuration ever work", admission answers
"may we try right now". Readiness never reads the state store or calls a vendor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from . import config as _config
from .credentials import file_credentials_allowed, get_secret, is_secret_scope_error
from .env_loader import clean_env_value, is_truthy
from .provider_registry import PROVIDER_SPECS, keyless_public_env_var

# Host style for fix commands. The MCP copy of this file sets "mcp".
HOST = "hermes"


class Readiness(str, Enum):
    READY = "ready"
    EXPLICIT_ONLY = "explicit_only"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    IGNORED_SOURCE = "ignored_source"
    INVALID_VALUE = "invalid_value"
    SCOPE_ERROR = "scope_error"


# States whose value the engine accepts (an explicit provider="X" call can run).
ACCEPTED = frozenset({Readiness.READY, Readiness.EXPLICIT_ONLY, Readiness.DISABLED})
# States where the user did something that does not work: say so instead of
# "nothing is configured".
ACTIONABLE = frozenset({
    Readiness.IGNORED_SOURCE, Readiness.INVALID_VALUE, Readiness.SCOPE_ERROR,
    Readiness.DISABLED, Readiness.EXPLICIT_ONLY,
})



@dataclass(frozen=True)
class ProviderReadiness:
    provider: str
    state: Readiness
    reason_code: str
    reason: str
    fix: Tuple[str, ...] = ()
    source: Optional[str] = None
    # In-process only: never serialised, never shown.
    credential: Optional[str] = field(default=None, repr=False, compare=False)

    @property
    def accepted(self) -> bool:
        """The engine accepts the value: an explicit call can run."""
        return self.state in ACCEPTED

    @property
    def auto_ready(self) -> bool:
        return self.state is Readiness.READY

    def to_public(self) -> Dict[str, Any]:
        """Redacted view for status JSON and tool errors: no credential, no URL."""
        public: Dict[str, Any] = {
            "provider": self.provider,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }
        if self.source:
            public["source"] = self.source
        if self.fix:
            public["fix"] = list(self.fix)
        return public


def _display(provider: str) -> str:
    spec = PROVIDER_SPECS.get(provider)
    return spec.display_name if spec else provider


def _set_command(env_var: str, value: str = "<value>") -> str:
    if HOST == "mcp":
        return f"set {env_var}={value} in the MCP client's env block or the server's .env"
    return f"hermes config set {env_var} {value}"


def _setup_command(provider: str) -> str:
    if HOST == "mcp":
        return f"web-search-plus-mcp setup {provider}"
    return f"hermes web-search-plus setup {provider}"


def _source_of_key(provider: str, config: Mapping[str, Any]) -> str:
    if file_credentials_allowed() and _config.file_api_key(provider, dict(config)):
        return "config_json"
    return "environment" if file_credentials_allowed() else "hermes_profile"


def _raw_secret(name: str) -> Optional[str]:
    value = get_secret(name)
    return value if isinstance(value, str) and value.strip() else None


def _searxng_file_url(config: Mapping[str, Any]) -> Optional[str]:
    section = config.get("searxng") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        url = section.get("base_url") or section.get("instance_url")
        if isinstance(url, str) and url.strip():
            return url
    return None


def _donsetch_file_binary(config: Mapping[str, Any]) -> Optional[str]:
    section = config.get("donsetch") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        binary = section.get("binary")
        if isinstance(binary, str) and binary.strip():
            return binary.strip()
    return None


def _accepted_state(provider: str, config: Mapping[str, Any]) -> Tuple[Readiness, str, str, Tuple[str, ...]]:
    """State for an accepted value: ready, explicit-only or disabled."""
    from .routing import _provider_auto_allowed

    auto = config.get("auto_routing") if isinstance(config, Mapping) else None
    auto = auto if isinstance(auto, Mapping) else {}
    disabled = auto.get("disabled_providers") or []
    name = _display(provider)
    if isinstance(disabled, (list, tuple)) and provider in disabled:
        command = (f"web-search-plus-mcp config enable {provider}" if HOST == "mcp"
                   else f"hermes web-search-plus config enable {provider}")
        return (Readiness.DISABLED, "disabled_in_config",
                f"{name} is set up but disabled in auto_routing.disabled_providers.", (command,))
    if not _provider_auto_allowed(provider, dict(auto)):
        command = (f"web-search-plus-mcp config set-auto-allow {provider} on" if HOST == "mcp"
                   else f"hermes web-search-plus config set-auto-allow {provider} on")
        return (Readiness.EXPLICIT_ONLY, "explicit_only",
                f"{name} is set up but only runs when asked for (provider=\"{provider}\").",
                (f'provider="{provider}"', command))
    return Readiness.READY, "ready", f"{name} is ready.", ()


def evaluate_provider(provider: str, config: Optional[Mapping[str, Any]] = None, *,
                      raise_scope_errors: bool = False) -> ProviderReadiness:
    """The readiness of one provider under the current host's credential rules.

    ``raise_scope_errors`` keeps the engine contract: a host refusal propagates
    instead of turning into a state (status and setup want the state).
    """
    config = config if isinstance(config, Mapping) else {}
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        return ProviderReadiness(provider, Readiness.NOT_CONFIGURED, "unknown_provider",
                                 f"{provider} is not a known provider.")
    name = spec.display_name
    try:
        return _evaluate(provider, spec, name, config)
    except Exception as exc:  # noqa: BLE001 - a host refusal must not look like "not configured"
        if is_secret_scope_error(exc) and not raise_scope_errors:
            return ProviderReadiness(
                provider, Readiness.SCOPE_ERROR, "credential_scope_refused",
                f"Hermes refused to read the credential for {name} in this profile.",
                ("hermes config show", "check the active Hermes profile"),
            )
        raise


def _evaluate(provider: str, spec: Any, name: str, config: Mapping[str, Any]) -> ProviderReadiness:
    cfg = dict(config)
    env_var = spec.env_var

    # SearXNG: an instance URL, validated against SSRF.
    if provider == "searxng":
        try:
            url = _config.get_searxng_instance_url(cfg)
        except ValueError as exc:
            text = str(exc)
            private = "private/internal" in text
            fix = ((_set_command("SEARXNG_ALLOW_PRIVATE", "1"),) if private
                   else (_set_command("SEARXNG_INSTANCE_URL", "https://your-searxng.example"),))
            reason = ("The SearXNG URL points to a private or internal address, which is blocked by default."
                      if private else "The SearXNG URL is not usable (wrong scheme, missing host or unresolvable).")
            return ProviderReadiness(provider, Readiness.INVALID_VALUE,
                                     "searxng_url_private" if private else "searxng_url_invalid", reason, fix)
        if url:
            state, code, reason, fix = _accepted_state(provider, cfg)
            source = "config_json" if file_credentials_allowed() and _searxng_file_url(cfg) else (
                "environment" if file_credentials_allowed() else "hermes_profile")
            return ProviderReadiness(provider, state, code, reason, fix, source, credential=url)
        if _searxng_file_url(cfg) and not file_credentials_allowed():
            return ProviderReadiness(
                provider, Readiness.IGNORED_SOURCE, "searxng_url_in_ignored_config",
                "A SearXNG URL is set in config.json or Hermes Desktop, which a Hermes profile does not read "
                "(the URL may carry credentials).",
                (_set_command("SEARXNG_INSTANCE_URL", "<url>"), "/reload"),
            )
        raw = _raw_secret("SEARXNG_INSTANCE_URL")
        if raw and not clean_env_value(raw):
            return _placeholder(provider, name, "SEARXNG_INSTANCE_URL")
        return ProviderReadiness(provider, Readiness.NOT_CONFIGURED, "missing_instance_url",
                                 "No SearXNG instance URL is set.",
                                 (_setup_command(provider),))

    key = _config.get_api_key(provider, cfg)

    # DonSeTch: the "key" is the path of a local binary.
    if provider == "donsetch":
        if key:
            path = key if os.path.isabs(key) else None
            if path is not None and not (os.path.isfile(path) and os.access(path, os.X_OK)):
                return ProviderReadiness(
                    provider, Readiness.INVALID_VALUE, "binary_not_executable",
                    "The configured DonSeTch binary does not exist or is not executable.",
                    (_set_command(env_var, "/path/to/donsetch"),),
                )
            state, code, reason, fix = _accepted_state(provider, cfg)
            source = ("config_json" if _donsetch_file_binary(cfg) and not _raw_secret(env_var)
                      else _source_of_key(provider, cfg))
            return ProviderReadiness(provider, state, code, reason, fix, source, credential=key)

    if key:
        # Key format is the vendor's business (search still rejects < 10 chars at
        # dispatch); readiness only answers whether a value is there.
        state, code, reason, fix = _accepted_state(provider, cfg)
        return ProviderReadiness(provider, state, code, reason, fix, _source_of_key(provider, cfg), credential=key)

    if spec.keyless and _config.keyless_public_allowed(provider, cfg):
        state, code, reason, fix = _accepted_state(provider, cfg)
        if state is Readiness.READY:
            code, reason = "keyless_public", f"{name} uses its public endpoint (no key)."
        source = "environment" if is_truthy(get_secret(keyless_public_env_var(provider))) else "config_json"
        if source == "environment" and not file_credentials_allowed():
            source = "hermes_profile"
        return ProviderReadiness(provider, state, code, reason, fix, source)

    # Not accepted: find out why.
    raw = _raw_secret(env_var)
    if raw and not clean_env_value(raw):
        return _placeholder(provider, name, env_var)
    if not file_credentials_allowed():
        if _config.file_api_key(provider, cfg):
            return ProviderReadiness(
                provider, Readiness.IGNORED_SOURCE, "api_key_in_ignored_config",
                f"config.json holds an API key for {name}, but Hermes reads keys only from the profile.",
                (_set_command(env_var, "<key>"), "/reload"),
            )
        section = cfg.get(spec.config_section)
        if spec.keyless and isinstance(section, Mapping) and is_truthy(section.get("allow_public")):
            return ProviderReadiness(
                provider, Readiness.IGNORED_SOURCE, "keyless_flag_in_ignored_config",
                f"{name}'s public tier is switched on in config.json, which Hermes ignores.",
                (_set_command(keyless_public_env_var(provider), "1"), "/reload"),
            )
    if spec.keyless:
        return ProviderReadiness(
            provider, Readiness.NOT_CONFIGURED, "keyless_not_opted_in",
            f"{name} has no key and its public tier is not switched on.",
            (_set_command(env_var, "<key>"), _set_command(keyless_public_env_var(provider), "1")),
        )
    return ProviderReadiness(provider, Readiness.NOT_CONFIGURED, "missing_api_key",
                             f"No API key for {name} is set.",
                             (_setup_command(provider),))


def _placeholder(provider: str, name: str, env_var: str) -> ProviderReadiness:
    return ProviderReadiness(
        provider, Readiness.INVALID_VALUE, "placeholder_value",
        f"{env_var} still holds a template placeholder, not a real value for {name}.",
        (_set_command(env_var, "<real value>"),),
    )


def evaluate(config: Optional[Mapping[str, Any]] = None,
             providers: Optional[Iterable[str]] = None) -> Dict[str, ProviderReadiness]:
    """Readiness for every (or the given) provider, in registry order."""
    names = list(providers) if providers is not None else list(PROVIDER_SPECS)
    return {name: evaluate_provider(name, config) for name in names}


def capability_providers(capability: str) -> List[str]:
    return [name for name, spec in PROVIDER_SPECS.items() if getattr(spec, f"supports_{capability}", False)]


def capability_ready(report: Mapping[str, ProviderReadiness], capability: str) -> bool:
    """Whether a tool for ``capability`` can run at all (any accepted provider)."""
    return any(report[name].accepted for name in capability_providers(capability) if name in report)


def most_relevant_problem(report: Mapping[str, ProviderReadiness],
                          candidates: Iterable[str]) -> Optional[ProviderReadiness]:
    """The first candidate the user tried to set up but that does not work.

    "Nothing configured" is the least helpful answer when the user did configure
    something (ignored, invalid, disabled or explicit-only).
    """
    order = (Readiness.IGNORED_SOURCE, Readiness.INVALID_VALUE, Readiness.SCOPE_ERROR,
             Readiness.DISABLED, Readiness.EXPLICIT_ONLY)
    picked = [report[name] for name in candidates if name in report]
    for state in order:
        for item in picked:
            if item.state is state:
                return item
    return None
