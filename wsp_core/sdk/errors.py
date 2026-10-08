"""Public, typed errors for provider modules.

The names in this module are part of the additive-only WSP 3.x provider SDK.
Providers may raise them without importing private plugin modules.
"""

from __future__ import annotations


class ProviderSDKError(Exception):
    """Base class for errors raised at the public provider boundary."""


class ProviderConfigError(ProviderSDKError):
    """A provider cannot run because its local configuration is invalid."""


class ProviderContractFailure(ProviderSDKError):
    """A provider returned a structurally unusable source-only envelope."""


class ProviderRegistrationError(ProviderSDKError):
    """A discovered provider specification is incomplete or inconsistent."""


class DuplicateProviderError(ProviderRegistrationError):
    """Two provider specifications claimed the same stable provider id."""


class ProviderDiscoveryError(ProviderSDKError):
    """A provider module could not be loaded during startup discovery."""


class ProviderStartupDiagnostic(ProviderDiscoveryError):
    """A safe startup diagnostic for one excluded provider module.

    Diagnostics intentionally retain only the module stem and stable code, not
    source paths or exception text, because startup reports are often shared.
    """

    def __init__(self, module: str, code: str) -> None:
        self.module = module
        self.code = code
        super().__init__(f"provider startup diagnostic: {module}:{code}")
