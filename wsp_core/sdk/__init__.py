"""Public Provider SDK for Web Search Plus 3.x.

This API is additive-only throughout the 3.x series.  Provider modules should
depend on this package rather than private registry or dispatch modules.
"""

import importlib
import sys

from .api import (
    ExtractExecute,
    ProviderSpec,
    SearchExecute,
    extract_result,
    make_extract_result,
    make_search_result,
    register_provider,
    search_result,
    source_result,
)
from .errors import (
    DuplicateProviderError,
    ProviderConfigError,
    ProviderContractFailure,
    ProviderDiscoveryError,
    ProviderRegistrationError,
    ProviderSDKError,
    ProviderStartupDiagnostic,
)
from ..http_client import ProviderRequestError

__all__ = [
    "DuplicateProviderError",
    "ExtractExecute",
    "ProviderConfigError",
    "ProviderContractFailure",
    "ProviderDiscoveryError",
    "ProviderRegistrationError",
    "ProviderRequestError",
    "ProviderSDKError",
    "ProviderSpec",
    "ProviderStartupDiagnostic",
    "SearchExecute",
    "extract_result",
    "make_extract_result",
    "make_search_result",
    "register_provider",
    "search_result",
    "source_result",
]

# Submodules reachable under the public name: ``wsp_sdk.<name>``.
_PUBLIC_SUBMODULES = ("api", "conformance", "errors")


def _bind_public_name(name: str = "wsp_sdk") -> None:
    """Make ``name`` and ``name.<submodule>`` resolve to this package's own modules.

    Left alone, ``import wsp_sdk.errors`` would find the file through the
    package ``__path__`` and execute it a second time under the public name:
    the provider would raise a distinct ``ProviderConfigError`` that the
    engine's ``isinstance`` checks miss, and ``conformance`` would lose the
    package its relative imports need. Registering the modules themselves keeps
    exactly one object per name in the process. Not part of the SDK surface.
    """
    sys.modules[name] = sys.modules[__name__]
    for submodule in _PUBLIC_SUBMODULES:
        sys.modules[f"{name}.{submodule}"] = importlib.import_module(f"{__name__}.{submodule}")
