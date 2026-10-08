"""Public Provider SDK for Web Search Plus 3.x.

This API is additive-only throughout the 3.x series.  Provider modules should
depend on this package rather than private registry or dispatch modules.
"""

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
