"""Typed, privacy-safe provider error classification for the v3 engine."""

from __future__ import annotations

from contract_v3 import ErrorClass, ErrorV3
from http_client import ProviderRequestError
from wsp_sdk.errors import ProviderConfigError, ProviderContractFailure
import json
import re


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


_SETUP_MESSAGE = re.compile(r"^Missing (API key for [a-z0-9_-]{1,32}|SearXNG instance URL)$")
_ENV_VAR = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _setup_guidance(error: BaseException) -> tuple[str, dict] | None:
    """Return WSP's own missing-key guidance, never upstream text.

    ``config.validate_api_key`` raises ``ProviderConfigError`` with a JSON body
    that WSP itself generated. Only that exact shape is surfaced: a fixed
    message pattern, an env-var name and the how-to-fix steps. Anything else
    (including the SearXNG ``provided`` URL) stays redacted.
    """
    try:
        body = json.loads(str(error))
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    message = body.get("error")
    env_var = body.get("env_var")
    steps = body.get("how_to_fix")
    if not (isinstance(message, str) and _SETUP_MESSAGE.match(message)):
        return None
    if not (isinstance(env_var, str) and _ENV_VAR.match(env_var)):
        return None
    if not (isinstance(steps, list) and steps and all(isinstance(x, str) and len(x) <= 300 for x in steps)):
        return None
    return message, {"env_var": env_var, "how_to_fix": list(steps[:6]), "setup_required": True}


def classify_provider_error(error: BaseException, *, provider: str) -> ErrorV3:
    """Map an arbitrary provider exception to the frozen ErrorV3 taxonomy.

    Exception messages are deliberately not copied: upstream messages routinely
    contain request URLs, credentials, query text, or response fragments.
    """

    status = getattr(error, "status_code", None)
    retry_after = getattr(error, "retry_after", None)
    class_name = type(error).__name__

    if isinstance(error, ProviderConfigError) or class_name == "ProviderConfigError":
        error_class = ErrorClass.CONFIG
    elif isinstance(error, (TimeoutError,)):
        error_class = ErrorClass.TIMEOUT
    elif status in {401, 403}:
        error_class = ErrorClass.AUTH
    elif status in {402, 432}:
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

    message = _MESSAGES[error_class]
    details: dict = {}
    if error_class is ErrorClass.CONFIG:
        guidance = _setup_guidance(error)
        if guidance is not None:
            message, details = guidance

    retryable = error_class in {
        ErrorClass.RATE_LIMIT,
        ErrorClass.TRANSIENT,
        ErrorClass.TIMEOUT,
    }
    return ErrorV3(
        error_class=error_class,
        code=_CODES[error_class],
        message=message,
        retryable=retryable,
        provider=provider,
        http_status=status if isinstance(status, int) else None,
        retry_after_seconds=(
            float(retry_after) if isinstance(retry_after, (int, float)) else None
        ),
        details=details,
    )
