"""Why one URL could not be extracted, in words the model can pass on.

Providers report a failed page in their own shapes: Tavily as a short text,
Exa as a crawl tag plus the page's HTTP status, Parallel and Firecrawl as the
page's status code, Linkup as an error code in an HTTP 400 body, Firecrawl as
an HTTP 403 for a site it refuses. This module maps those signals to a small
set of reasons with controlled messages. Provider text is matched, never
copied: it can carry URLs, account details or prompt-injection text.

A page that does not exist (HTTP 404/410 from the site, as reported by a
provider) is final: no other provider can read it, so the fallback chain stops
for that URL instead of paying for more attempts.

No engine imports: the HTTP client uses this module to classify error bodies.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

NOT_FOUND = "not_found"
GONE = "gone"
BLOCKED = "blocked"
UNSUPPORTED_SITE = "unsupported_site"
UNREACHABLE = "unreachable"
SITE_ERROR = "site_error"

# A provider cannot read a page that the site says does not exist.
FINAL_REASONS = frozenset({NOT_FOUND, GONE})

# When several providers fail one URL, the most telling reason is kept.
_RANK = {NOT_FOUND: 6, GONE: 6, BLOCKED: 4, UNSUPPORTED_SITE: 3, UNREACHABLE: 2, SITE_ERROR: 1}

# Linkup /fetch answers HTTP 400 with these codes for target problems.
TARGET_ERROR_CODES = {
    "fetch_target_not_found": NOT_FOUND,
    "fetch_target_unreachable": UNREACHABLE,
    "fetch_target_forbidden": BLOCKED,
    "fetch_target_blocked": BLOCKED,
}

# Firecrawl answers HTTP 403 "we do not support this site" for sites it refuses.
_UNSUPPORTED_SITE_WORDING = re.compile(
    r"(?:do not|don't|does not|doesn't|cannot|can't) (?:support|scrape|crawl) this (?:site|website|domain|url|page)"
)


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


def reason_for_status(status: Any) -> Optional[str]:
    """Reason for the HTTP status the site itself answered, if it is a failure."""
    if isinstance(status, bool) or not isinstance(status, int) or status < 400:
        return None
    if status == 404:
        return NOT_FOUND
    if status == 410:
        return GONE
    if status in {401, 403, 407, 451}:
        return BLOCKED
    if status in {408, 504, 522, 523, 524}:
        return UNREACHABLE
    if status == 429:
        return BLOCKED
    if status >= 500:
        return SITE_ERROR
    return None


def reason_for_text(text: Any) -> Optional[str]:
    """Reason from a provider's short failure text (Tavily ``failed_results``)."""
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    # Only a status code counts as "does not exist": that reason ends the
    # fallback chain, and "content not found" can mean an unreadable page.
    if re.search(r"\b404\b", lowered):
        return NOT_FOUND
    if re.search(r"\b410\b", lowered):
        return GONE
    if re.search(r"\b40[13]\b|forbidden|blocked|captcha|access denied", lowered):
        return BLOCKED
    if re.search(r"time(?:d)? ?out|unreachable|could not connect|connection", lowered):
        return UNREACHABLE
    return None


def reason_for_error_body(status: int, text: str, codes: list) -> Optional[str]:
    """Target failure carried in a provider's HTTP error body, or None.

    Used by the HTTP client for classification only: a target failure is not
    an account problem, so it must not open the provider's circuit.
    """
    for code in codes:
        if code in TARGET_ERROR_CODES:
            return TARGET_ERROR_CODES[code]
    if status == 403 and _UNSUPPORTED_SITE_WORDING.search(text.lower()):
        return UNSUPPORTED_SITE
    return None


def failure_fields(reason: Optional[str], status: Optional[int] = None,
                   provider: Optional[str] = None) -> Dict[str, Any]:
    """Fields for an error item: ``error`` text plus ``error_reason`` (and status)."""
    if reason is None:
        return {}
    fields: Dict[str, Any] = {"error": message(reason, status, provider), "error_reason": reason}
    if isinstance(status, int):
        fields["target_status"] = status
    return fields


def better(current: Optional[Dict[str, Any]], candidate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The error item that says more about why the page failed."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    if _RANK.get(str(candidate.get("error_reason")), 0) > _RANK.get(str(current.get("error_reason")), 0):
        return candidate
    return current


def is_final(item: Optional[Dict[str, Any]]) -> bool:
    return isinstance(item, dict) and item.get("error_reason") in FINAL_REASONS


def summary(results: list, tried: list, not_tried: int, attempt_limit: int) -> str:
    """One line for a request where no URL could be read."""
    reasons = [item.get("error_reason") for item in results if isinstance(item, dict)]
    count = len(results)
    if count == 1 and reasons and reasons[0]:
        head = f"Could not read the page: {results[0].get('error')}"
    elif count == 1:
        head = "Could not read the page"
    else:
        head = f"Could not read any of the {count} pages"
    if reasons and all(reason in FINAL_REASONS for reason in reasons):
        return head
    parts = [head]
    if tried:
        parts.append("tried " + ", ".join(tried))
    if not_tried:
        parts.append(
            f"{not_tried} more provider{'s' if not_tried != 1 else ''} not tried "
            f"(limit of {attempt_limit} provider attempts per request)"
        )
    return "; ".join(parts)


def split_status(value: Any) -> Tuple[Optional[str], Optional[int]]:
    """Reason and status for a page status value (int or numeric string)."""
    try:
        status = int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        status = None
    return reason_for_status(status), status
