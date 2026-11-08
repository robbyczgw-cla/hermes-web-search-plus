"""Where the engine reads credentials and profile-scoped settings.

The engine never decides where a secret comes from. A host installs a resolver
with :func:`set_secret_resolver`: the Hermes plugin installs
``agent.secret_scope.get_secret`` so every read honours the active Hermes
profile (Hermes plugin catalog rule 11). Without a resolver, :func:`get_secret`
reads the process environment, which is what the MCP server and other
standalone hosts fill from their own configuration.

Every provider key, ``SEARXNG_INSTANCE_URL``, the keyless opt-ins and other
per-profile values are read through :func:`get_secret`. The engine does not
read ``.env`` files itself.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

SecretResolver = Callable[[str, Optional[str]], Optional[str]]

_resolver: Optional[SecretResolver] = None
# A fixed set of values for the current call only (status/setup render a given
# snapshot). A ContextVar, so threads and tasks never see each other's view and
# the host policy (file_credentials_allowed) is unchanged.
_secrets_override: ContextVar[Optional[Mapping[str, str]]] = ContextVar("wsp_secrets_override", default=None)


@contextmanager
def secrets_view(values: Mapping[str, str]) -> Iterator[None]:
    """Answer :func:`get_secret` from ``values`` inside the block (this context only)."""
    token = _secrets_override.set(dict(values))
    try:
        yield
    finally:
        _secrets_override.reset(token)


def set_secret_resolver(resolver: Optional[SecretResolver]) -> None:
    """Install the host's resolver (``None`` returns to the process environment)."""
    global _resolver
    _resolver = resolver


def get_secret_resolver() -> Optional[SecretResolver]:
    return _resolver


def file_credentials_allowed() -> bool:
    """Whether API keys written into ``config.json`` may be used.

    Under a host resolver (Hermes) every credential comes from the active
    profile through that resolver; a key in the shared ``config.json`` would be
    used by every profile, so it is ignored. Hosts without a resolver (MCP
    server, OpenClaw skill, standalone CLI) keep reading ``config.json``.
    """
    return _resolver is None


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return the value of credential/setting ``name`` for the active scope.

    Errors from the host resolver propagate: a resolver that refuses an unscoped
    read (Hermes multiplexing) must fail the call, not fall back to another
    profile's value in ``os.environ``.
    """
    override = _secrets_override.get()
    if override is not None:
        value = override.get(name)
        return value if value is not None else default
    if _resolver is not None:
        try:
            return _resolver(name, default)
        except Exception as exc:
            _mark_secret_scope_error(exc)
            raise
    value = os.environ.get(name)
    return value if value is not None else default


_SCOPE_ERROR_MARK = "_wsp_secret_scope_error"


def _mark_secret_scope_error(exc: BaseException) -> None:
    try:
        setattr(exc, _SCOPE_ERROR_MARK, True)
    except Exception:  # pragma: no cover - exotic exception types without __dict__
        pass


def is_secret_scope_error(exc: BaseException) -> bool:
    """Whether ``exc`` came from the host resolver refusing or failing a read.

    Engine code that turns exceptions into "provider unavailable" must re-raise
    these, so a scoping error reaches the tool output instead of reading as
    "no keys configured".
    """
    return bool(getattr(exc, _SCOPE_ERROR_MARK, False))


_SECRET_KEY_RE = re.compile(r"(^|_)(key|api_?keys?|auth|token|secret|password|passwd|authorization|credentials?)$", re.I)
_SECRET_KEY_CAMEL_RE = re.compile(r"(apikey|accesstoken|authtoken|clientsecret|secretkey|bearer)$", re.I)


def is_secret_field(name: Any) -> bool:
    """Whether a config/JSON field name holds a secret (``api_key``, ``apiKey``, ``*_API_KEY``, ...)."""
    # Only complete names/suffixes match, never substrings such as keyless.
    text = str(name).replace("-", "_")
    return bool(_SECRET_KEY_RE.search(text) or _SECRET_KEY_CAMEL_RE.search(text.replace("_", "")))


def redact_url(value: str, *, origin_only: bool = False) -> str:
    """Remove URL credentials, query and fragment; optionally keep only the origin."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not parsed.scheme or not host:
            return "[redacted URL]"
        host = f"[{host}]" if ":" in host else host
        authority = f"{host}:{parsed.port}" if parsed.port is not None else host
        return urlunsplit((parsed.scheme, authority, "" if origin_only else parsed.path, "", ""))
    except ValueError:
        return "[redacted URL]"


def redact_secrets(value: Any) -> Any:
    """Copy of ``value`` with every secret field replaced by ``"set"`` / ``"not set"``.

    Use it for every status, config, dry-run or JSON output that is built from
    config.json or the credential environment. The secret itself is never kept.
    """
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if is_secret_field(key):
                out[key] = "set" if item else "not set"
            elif isinstance(item, str) and str(key).lower().replace("-", "_") in {
                "base_url", "instance_url", "searxng_instance_url", "endpoint", "url",
            }:
                out[key] = redact_url(item) if item else item
            else:
                out[key] = redact_secrets(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value
