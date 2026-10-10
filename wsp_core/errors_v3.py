"""Typed, privacy-safe provider error classification for the v3 engine."""

from __future__ import annotations

from .contract_v3 import ErrorClass, ErrorV3
from .http_client import ProviderRequestError
from .query_limits import PROVIDER_QUERY_LIMITS
from .sdk.errors import ProviderConfigError, ProviderContractFailure
import json


_MESSAGES = {
    ErrorClass.CONFIG: "Provider configuration is invalid",
    ErrorClass.AUTH: "Provider authentication failed",
    ErrorClass.QUOTA: "Provider quota is exhausted",
    ErrorClass.RATE_LIMIT: "Provider rate limit was reached",
    ErrorClass.TRANSIENT: "Provider is temporarily unavailable",
    ErrorClass.TIMEOUT: "Provider request timed out",
    ErrorClass.PROVIDER_CONTRACT: "Provider returned an invalid response",
    ErrorClass.INVALID_REQUEST: "Provider rejected the request as invalid",
    ErrorClass.INTERNAL: "Provider execution failed",
}

# WSP's own text for an account the provider reports as empty (HTTP 402, or
# Linkup's 429 / Serper's 400 with credit wording). More useful than the
# generic quota line, and never copied from the provider's answer.
_OUT_OF_CREDIT_MESSAGE = "Out of credits: the provider account has no funds left; top it up or remove its key"

_CODES = {
    ErrorClass.CONFIG: "wsp.config.provider_invalid",
    ErrorClass.AUTH: "wsp.provider.auth",
    ErrorClass.QUOTA: "wsp.provider.quota",
    ErrorClass.RATE_LIMIT: "wsp.provider.rate_limit",
    ErrorClass.TRANSIENT: "wsp.provider.transient",
    ErrorClass.TIMEOUT: "wsp.provider.timeout",
    ErrorClass.PROVIDER_CONTRACT: "wsp.provider.contract",
    ErrorClass.INVALID_REQUEST: "wsp.provider.invalid_request",
    ErrorClass.INTERNAL: "wsp.provider.internal",
}


# The provider answered "your request is malformed": a request problem, not a
# provider health problem. A bare 400 is ambiguous (Serper and Linkup use it for
# empty accounts, others for anything), so it counts only for providers with a
# documented query limit.
_VALIDATION_STATUSES = frozenset({413, 414, 422})


def _is_validation_error(provider: str, status: object) -> bool:
    return status in _VALIDATION_STATUSES or (
        status == 400 and provider in PROVIDER_QUERY_LIMITS
    )


def _invalid_request_message(provider: str, status: object) -> str:
    """WSP's own text for a 4xx validation error; never the provider's answer."""
    limit = PROVIDER_QUERY_LIMITS.get(provider)
    if limit is None:
        return f"{_MESSAGES[ErrorClass.INVALID_REQUEST]} (HTTP {status}); the query may be too long or malformed"
    return (
        f"Query rejected by {provider} (HTTP {status}); it may be too long "
        f"({provider} accepts up to {limit.chars} characters / {limit.words} words)"
    )


class MissingProviderKeyError(ProviderConfigError):
    """WSP's own error for a provider without a key or SearXNG URL.

    The message stays the JSON guidance older readers parse. Code that shows
    guidance rebuilds it with ``missing_key_guidance`` from the registry and
    never copies exception text.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(json.dumps(missing_key_guidance(provider)))


def missing_key_guidance(provider: str) -> dict:
    """Setup guidance for a provider without credentials, from the registry."""
    if provider == "searxng":
        return {
            "error": "Missing SearXNG instance URL",
            "env_var": "SEARXNG_INSTANCE_URL",
            "how_to_fix": [
                "1. Set up your own SearXNG instance: https://docs.searxng.org/admin/installation.html",
                "2. Add to config.json: \"searxng\": {\"instance_url\": \"https://your-instance.example.com\"}",
                "3. Or set environment variable: export SEARXNG_INSTANCE_URL=\"https://your-instance.example.com\"",
                "Note: SearXNG requires a self-hosted instance with JSON format enabled.",
            ],
            "provider": provider,
        }
    from .provider_registry import PROVIDER_SPECS  # lazy: the registry imports the SDK

    spec = PROVIDER_SPECS[provider]
    env_var = spec.env_var
    return {
        "error": f"Missing API key for {provider}",
        "env_var": env_var,
        "how_to_fix": [
            f"1. Get your API key from {spec.signup_url}",
            f"2. Run: python3 ~/.hermes/plugins/web-search-plus/setup.py setup {provider}",
            f"3. Or add {env_var} to your Hermes profile .env",
            f"4. Or set environment variable: export {env_var}=\"your-key\"",
        ],
        "provider": provider,
    }


def _setup_guidance(error: BaseException) -> tuple[str, dict] | None:
    """Return WSP's own missing-key guidance, rebuilt from the registry.

    Only ``MissingProviderKeyError`` (raised by ``config.validate_api_key``)
    qualifies. Any other configuration error, including one whose text looks
    like WSP guidance, stays redacted.
    """
    if not isinstance(error, MissingProviderKeyError):
        return None
    try:
        guidance = missing_key_guidance(error.provider)
    except KeyError:
        return None
    return guidance["error"], {
        "env_var": guidance["env_var"],
        "how_to_fix": list(guidance["how_to_fix"]),
        "setup_required": True,
    }


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
    elif isinstance(error, ProviderRequestError) and getattr(error, "out_of_credit", False):
        # Keeps the provider's real status in http_status (Linkup answers 429).
        error_class = ErrorClass.QUOTA
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
    elif isinstance(error, ProviderRequestError) and _is_validation_error(provider, status):
        error_class = ErrorClass.INVALID_REQUEST
    elif isinstance(error, ProviderContractFailure):
        error_class = ErrorClass.PROVIDER_CONTRACT
    elif isinstance(error, (TypeError, KeyError)):
        error_class = ErrorClass.PROVIDER_CONTRACT
    else:
        error_class = ErrorClass.INTERNAL

    message = _MESSAGES[error_class]
    if error_class is ErrorClass.QUOTA and getattr(error, "out_of_credit", False):
        message = _OUT_OF_CREDIT_MESSAGE
    if error_class is ErrorClass.INVALID_REQUEST:
        message = _invalid_request_message(provider, status)
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
