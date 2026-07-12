"""Typed, privacy-safe provider error classification for the v3 engine."""

from __future__ import annotations

from contract_v3 import ErrorClass, ErrorV3
from http_client import ProviderRequestError


_MESSAGES = {
    ErrorClass.CONFIG: "Provider configuration is invalid",
    ErrorClass.AUTH: "Provider authentication failed",
    ErrorClass.QUOTA: "Provider quota is exhausted",
    ErrorClass.RATE_LIMIT: "Provider rate limit was reached",
    ErrorClass.TRANSIENT: "Provider is temporarily unavailable",
    ErrorClass.TIMEOUT: "Provider request timed out",
    ErrorClass.PROVIDER_CONTRACT: "Provider returned an invalid response",
    ErrorClass.INTERNAL: "Provider execution failed",
}

_CODES = {
    ErrorClass.CONFIG: "wsp.config.provider_invalid",
    ErrorClass.AUTH: "wsp.provider.auth",
    ErrorClass.QUOTA: "wsp.provider.quota",
    ErrorClass.RATE_LIMIT: "wsp.provider.rate_limit",
    ErrorClass.TRANSIENT: "wsp.provider.transient",
    ErrorClass.TIMEOUT: "wsp.provider.timeout",
    ErrorClass.PROVIDER_CONTRACT: "wsp.provider.contract",
    ErrorClass.INTERNAL: "wsp.provider.internal",
}


class ProviderContractFailure(Exception):
    """Provider returned a structurally unusable result without a transport error."""



def classify_provider_error(error: BaseException, *, provider: str) -> ErrorV3:
    """Map an arbitrary provider exception to the frozen ErrorV3 taxonomy.

    Exception messages are deliberately not copied: upstream messages routinely
    contain request URLs, credentials, query text, or response fragments.
    """

    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after", None)
    class_name = type(error).__name__

    if class_name == "ProviderConfigError":
        error_class = ErrorClass.CONFIG
    elif isinstance(error, (TimeoutError,)):
        error_class = ErrorClass.TIMEOUT
    elif status in {401, 403}:
        error_class = ErrorClass.AUTH
    elif status == 402:
        error_class = ErrorClass.QUOTA
    elif status == 429:
        error_class = ErrorClass.RATE_LIMIT
    elif isinstance(error, ProviderRequestError) and (
        bool(getattr(error, "transient", False))
        or (isinstance(status, int) and status >= 500)
    ):
        error_class = ErrorClass.TRANSIENT
    elif isinstance(error, ProviderContractFailure):
        error_class = ErrorClass.PROVIDER_CONTRACT
    elif isinstance(error, (TypeError, KeyError)):
        error_class = ErrorClass.PROVIDER_CONTRACT
    else:
        error_class = ErrorClass.INTERNAL

    retryable = error_class in {
        ErrorClass.RATE_LIMIT,
        ErrorClass.TRANSIENT,
        ErrorClass.TIMEOUT,
    }
    return ErrorV3(
        error_class=error_class,
        code=_CODES[error_class],
        message=_MESSAGES[error_class],
        retryable=retryable,
        provider=provider,
        http_status=status if isinstance(status, int) else None,
        retry_after_seconds=(
            float(retry_after) if isinstance(retry_after, (int, float)) else None
        ),
    )
