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
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

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

# error_type values of the engine's setup errors (config.add_provider_setup_guidance):
# WSP's own text, safe to pass on, never a provider failure.
SETUP_ERROR_TYPES = frozenset({
    "provider_setup_required", "requested_provider_not_configured", "explicit_only_provider_configured",
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


def _set_command(env_var: str, value: str) -> str:
    if HOST == "mcp":
        return f"set {env_var}={value} in the MCP client's env block or the server's .env"
    return f"hermes config set {env_var} {value}"


def _cli(command: str) -> str:
    return f"web-search-plus-mcp {command}" if HOST == "mcp" else f"hermes web-search-plus {command}"


def evaluate_provider(provider: str, config: Optional[Mapping[str, Any]] = None) -> ProviderReadiness:
    """The readiness of one provider under the current host's credential rules.

    A host refusal to read a credential propagates (the engine reports it as
    such); :func:`evaluate` turns it into ``scope_error`` for status views.
    """
    from .routing import _provider_auto_allowed

    cfg = dict(config or {})
    spec = PROVIDER_SPECS[provider]
    name = spec.display_name
    env_var = spec.env_var
    files_count = file_credentials_allowed()  # False under a Hermes profile resolver
    auto = cfg.get("auto_routing") if isinstance(cfg.get("auto_routing"), Mapping) else {}

    def accepted(source: Optional[str], credential: Optional[str] = None,
                 ready_code: str = "ready", ready_reason: str = "") -> ProviderReadiness:
        disabled = auto.get("disabled_providers") or []
        if isinstance(disabled, (list, tuple)) and provider in disabled:
            return ProviderReadiness(
                provider, Readiness.DISABLED, "disabled_in_config",
                f"{name} is set up but disabled in auto_routing.disabled_providers.",
                (_cli(f"config enable {provider}"),), source, credential)
        if not _provider_auto_allowed(provider, dict(auto)):
            return ProviderReadiness(
                provider, Readiness.EXPLICIT_ONLY, "explicit_only",
                f"{name} is set up but only runs when asked for (provider=\"{provider}\").",
                (f'provider="{provider}"', _cli(f"config set-auto-allow {provider} on")), source, credential)
        return ProviderReadiness(provider, Readiness.READY, ready_code,
                                 ready_reason or f"{name} is ready.", (), source, credential)

    def placeholder(name_of_var: str) -> ProviderReadiness:
        return ProviderReadiness(
            provider, Readiness.INVALID_VALUE, "placeholder_value",
            f"{name_of_var} still holds a template placeholder, not a real value for {name}.",
            (_set_command(name_of_var, "<real value>"),))

    def raw(name_of_var: str) -> str:
        return str(get_secret(name_of_var) or "").strip()

    env_source = "environment" if files_count else "hermes_profile"

    # SearXNG: an instance URL, validated against SSRF by the engine.
    if provider == "searxng":
        section = cfg.get("searxng") if isinstance(cfg.get("searxng"), Mapping) else {}
        file_url = section.get("base_url") or section.get("instance_url")
        try:
            url = _config.get_searxng_instance_url(cfg)
        except ValueError as exc:  # the engine's SSRF verdict, raised as ValueError
            private = "private/internal" in str(exc)
            return ProviderReadiness(
                provider, Readiness.INVALID_VALUE,
                "searxng_url_private" if private else "searxng_url_invalid",
                "The SearXNG URL points to a private or internal address, which is blocked by default."
                if private else "The SearXNG URL is not usable (wrong scheme, missing host or unresolvable).",
                (_set_command("SEARXNG_ALLOW_PRIVATE", "1") if private
                 else _set_command("SEARXNG_INSTANCE_URL", "https://your-searxng.example"),))
        if url:
            return accepted("config_json" if files_count and file_url else env_source, url)
        if file_url and not files_count:
            return ProviderReadiness(
                provider, Readiness.IGNORED_SOURCE, "searxng_url_in_ignored_config",
                "A SearXNG URL is set in config.json or Hermes Desktop, which a Hermes profile does not read "
                "(the URL may carry credentials).",
                (_set_command("SEARXNG_INSTANCE_URL", "<url>"), "/reload"))
        if raw("SEARXNG_INSTANCE_URL"):
            return placeholder("SEARXNG_INSTANCE_URL")
        return ProviderReadiness(provider, Readiness.NOT_CONFIGURED, "missing_instance_url",
                                 "No SearXNG instance URL is set.", (_cli(f"setup {provider}"),))

    key = _config.get_api_key(provider, cfg)
    key_source = "config_json" if files_count and _config.file_api_key(provider, cfg) else env_source

    if key and provider == "donsetch":
        # The "key" is the path of a local binary; config.json `binary` counts on every host.
        if os.path.isabs(key) and not (os.path.isfile(key) and os.access(key, os.X_OK)):
            return ProviderReadiness(
                provider, Readiness.INVALID_VALUE, "binary_not_executable",
                "The configured DonSeTch binary does not exist or is not executable.",
                (_set_command(env_var, "/path/to/donsetch"),))
        from_binary_field = isinstance(cfg.get("donsetch"), Mapping) and cfg["donsetch"].get("binary") and not raw(env_var)
        return accepted("config_json" if from_binary_field else key_source, key)

    if key:
        # Key format is the vendor's business (search still rejects < 10 chars
        # at dispatch); readiness only answers whether a value is there.
        return accepted(key_source, key)

    if spec.keyless and _config.keyless_public_allowed(provider, cfg):
        from_env = is_truthy(get_secret(keyless_public_env_var(provider)))
        return accepted(env_source if from_env else "config_json",
                        ready_code="keyless_public", ready_reason=f"{name} uses its public endpoint (no key).")

    # Not accepted: say why.
    if raw(env_var) and not clean_env_value(raw(env_var)):
        return placeholder(env_var)
    if not files_count and _config.file_api_key(provider, cfg):
        return ProviderReadiness(
            provider, Readiness.IGNORED_SOURCE, "api_key_in_ignored_config",
            f"config.json holds an API key for {name}, but Hermes reads keys only from the profile.",
            (_set_command(env_var, "<key>"), "/reload"))
    section = cfg.get(spec.config_section)
    if not files_count and spec.keyless and isinstance(section, Mapping) and is_truthy(section.get("allow_public")):
        return ProviderReadiness(
            provider, Readiness.IGNORED_SOURCE, "keyless_flag_in_ignored_config",
            f"{name}'s public tier is switched on in config.json, which Hermes ignores.",
            (_set_command(keyless_public_env_var(provider), "1"), "/reload"))
    if spec.keyless:
        return ProviderReadiness(
            provider, Readiness.NOT_CONFIGURED, "keyless_not_opted_in",
            f"{name} has no key and its public tier is not switched on.",
            (_set_command(env_var, "<key>"), _set_command(keyless_public_env_var(provider), "1")))
    return ProviderReadiness(provider, Readiness.NOT_CONFIGURED, "missing_api_key",
                             f"No API key for {name} is set.", (_cli(f"setup {provider}"),))


def evaluate(config: Optional[Mapping[str, Any]] = None,
             providers: Optional[Iterable[str]] = None) -> Dict[str, ProviderReadiness]:
    """Readiness for every (or the given) provider, in registry order.

    The one place a host refusal becomes a state: status and hints show
    ``scope_error`` instead of failing on the first refused read.
    """
    report: Dict[str, ProviderReadiness] = {}
    for name in (providers if providers is not None else PROVIDER_SPECS):
        try:
            report[name] = evaluate_provider(name, config)
        except Exception as exc:  # noqa: BLE001 - only scope refusals are turned into a state
            if not is_secret_scope_error(exc):
                raise
            report[name] = ProviderReadiness(
                name, Readiness.SCOPE_ERROR, "credential_scope_refused",
                f"Hermes refused to read the credential for {PROVIDER_SPECS[name].display_name} in this profile.",
                ("hermes config show", "check the active Hermes profile"))
    return report


def most_relevant_problem(report: Mapping[str, ProviderReadiness],
                          candidates: Iterable[str]) -> Optional[ProviderReadiness]:
    """The first candidate the user tried to set up but that does not work.

    "Nothing configured" is the least helpful answer when the user did configure
    something (ignored, invalid, disabled or explicit-only).
    """
    picked = [report[name] for name in candidates if name in report]
    for state in (Readiness.IGNORED_SOURCE, Readiness.INVALID_VALUE, Readiness.SCOPE_ERROR,
                  Readiness.DISABLED, Readiness.EXPLICIT_ONLY):
        for item in picked:
            if item.state is state:
                return item
    return None
