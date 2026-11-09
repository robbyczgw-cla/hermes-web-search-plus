"""Why one URL could not be extracted, in words the model can pass on.

Providers report a failed page in their own shapes: Tavily as a short text,
Exa as a crawl tag plus the page's HTTP status, Parallel and Firecrawl as the
page's status code, Linkup as an error code in an HTTP 400 body, Firecrawl as
an HTTP 403 for a site it refuses. Each adapter (and the HTTP client, for error
bodies) maps its signal to one of the small set of reasons below; this module
holds the reasons and their controlled messages. Provider text is matched,
never copied: it can carry URLs, account details or prompt-injection text.

A page that does not exist (HTTP 404/410 from the site, as reported by a
provider) is final: no other provider can read it, so the fallback chain stops
for that URL instead of paying for more attempts.

No engine imports: the HTTP client and the provider adapters use this module.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

NOT_FOUND = "not_found"
GONE = "gone"
BLOCKED = "blocked"
UNSUPPORTED_SITE = "unsupported_site"
UNREACHABLE = "unreachable"
SITE_ERROR = "site_error"
# The provider itself failed for every URL (not the pages): it did not answer
# in time, or it cannot start and needs setup. Not final: another provider may read the page.
PROVIDER_TIMEOUT = "provider_timeout"
PROVIDER_SETUP = "provider_setup"
PROVIDER_REASONS = frozenset({PROVIDER_TIMEOUT, PROVIDER_SETUP})

# A provider cannot read a page that the site says does not exist.
FINAL_REASONS = frozenset({NOT_FOUND, GONE})


def message(reason: str, status: Optional[int] = None, provider: Optional[str] = None) -> str:
    """The controlled text for a reason, as shown to the model."""
    code = f" (HTTP {status})" if isinstance(status, int) else ""
    if reason == NOT_FOUND:
        return f"page not found{code or ' (HTTP 404)'}"
    if reason == GONE:
        return f"page removed{code or ' (HTTP 410)'}"
    if reason == BLOCKED:
        return f"the site refused automated access{code}"
    if reason == UNSUPPORTED_SITE:
        return f"{provider or 'this provider'} does not support this site"
    if reason == UNREACHABLE:
        return "the site could not be reached or timed out"
    if reason == SITE_ERROR:
        return f"the site answered with an error{code}"
    return "could not read this page"


def failure_fields(reason: Optional[str], status: Optional[int] = None,
                   provider: Optional[str] = None) -> Dict[str, Any]:
    """Fields for an error item: ``error`` text plus ``error_reason`` (and status)."""
    if reason is None:
        return {}
    fields: Dict[str, Any] = {"error": message(reason, status, provider), "error_reason": reason}
    if isinstance(status, int):
        fields["target_status"] = status
    return fields


def split_status(value: Any) -> Tuple[Optional[str], Optional[int]]:
    """Reason and status for the page status a provider reports (int or numeric string).

    The reason is None unless the site itself answered with a failure status.
    """
    try:  # provider answer: the status may be any JSON value
        status = int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        status = None
    if status is None or status < 400:
        return None, status
    if status == 404:
        return NOT_FOUND, status
    if status == 410:
        return GONE, status
    if status in {401, 403, 407, 429, 451}:
        return BLOCKED, status
    if status in {408, 504, 522, 523, 524}:
        return UNREACHABLE, status
    if status >= 500:
        return SITE_ERROR, status
    return None, status
