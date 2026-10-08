"""The publication date of a search result, as one ``YYYY-MM-DD`` string (UTC).

Providers send the date under different keys and in different forms: ISO
datetimes (Exa, Brave ``page_age``), RFC 2822 ("Mon, 05 Jan 2026 10:00:00
GMT"), "Jan 5, 2026" and "3 days ago" (Serper, Brave ``age``).
``published_date`` reads the first usable value of an item and returns a date
that can be shown and compared, or None when nothing can be trusted. It never
raises and uses no network or clock of its own: relative forms resolve against
the ``now`` the caller passes.

A month in a relative form counts as 30 days and a year as 365 days. Dates
before 1990-01-01 (epoch placeholders) and dates more than one day after
``now`` are not usable; the day of slack covers clock and time-zone skew.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

# Order matters: explicit publication keys first, ``date`` next, then the
# provider-specific page age (ISO, more precise) before the free-text ``age``.
DATE_KEYS = ("published_at", "published_date", "publish_date", "publishedDate", "date", "page_age", "age")

_EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
_MAX_LENGTH = 64
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?\s*(?:[Zz]|([+-])(\d{2})(?::?(\d{2}))?)?)?",
    re.ASCII,
)
_RELATIVE = re.compile(r"(\d{1,6})\s+([a-z]+?)s?\s+ago", re.IGNORECASE | re.ASCII)
_UNITS = {
    "min": timedelta(minutes=1), "minute": timedelta(minutes=1),
    "hr": timedelta(hours=1), "hour": timedelta(hours=1),
    "day": timedelta(days=1), "week": timedelta(weeks=1),
    "month": timedelta(days=30), "year": timedelta(days=365),
}
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([a-z]{3,9})\.?,?\s+(\d{4})", re.IGNORECASE | re.ASCII)
_MONTH_DAY_YEAR = re.compile(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", re.IGNORECASE | re.ASCII)
_MONTHS = {"sept": 9}
for _number, _name in enumerate(
    ("january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"), 1
):
    _MONTHS[_name] = _MONTHS[_name[:3]] = _number


def _as_utc(moment: datetime) -> datetime:
    """Aware UTC; a naive datetime is taken to be UTC already."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_iso(text: str) -> Optional[datetime]:
    match = _ISO.fullmatch(text)
    if not match:
        return None
    year, month, day, hour, minute, second, sign, offset_hour, offset_minute = match.groups()
    moment = datetime(
        int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0),
        tzinfo=timezone.utc,
    )
    if sign:
        if int(offset_hour) > 23 or int(offset_minute or 0) > 59:
            return None
        offset = timedelta(hours=int(offset_hour), minutes=int(offset_minute or 0))
        moment = moment - offset if sign == "+" else moment + offset
    return moment


def _parse_relative(text: str, now: datetime) -> Optional[datetime]:
    if text.lower() == "yesterday":
        return now - timedelta(days=1)
    match = _RELATIVE.fullmatch(text)
    unit = _UNITS.get(match.group(2).lower()) if match else None
    if unit is None:
        return None
    return now - int(match.group(1)) * unit


def _parse_words(text: str) -> Optional[datetime]:
    match = _DAY_MONTH_YEAR.fullmatch(text)
    if match:
        day, name, year = match.groups()
    else:
        match = _MONTH_DAY_YEAR.fullmatch(text)
        if not match:
            return None
        name, day, year = match.groups()
    month = _MONTHS.get(name.lower())
    if month is None:
        return None
    return datetime(int(year), month, int(day), tzinfo=timezone.utc)


def _parse(text: str, now: datetime) -> Optional[datetime]:
    """One value to an aware UTC datetime, or None. May raise on odd input."""
    text = text.strip()
    if not text or len(text) > _MAX_LENGTH:
        return None
    for parse in (_parse_iso, _parse_words):
        moment = parse(text)
        if moment is not None:
            return moment
    moment = _parse_relative(text, now)
    if moment is not None:
        return moment
    return _as_utc(parsedate_to_datetime(text))  # RFC 2822


def published_date(item: dict, *, now: datetime) -> Optional[str]:
    """``YYYY-MM-DD`` (UTC) from the first usable date key of ``item``, else None.

    A value that cannot be parsed or is out of range does not stop the search:
    the next key is tried.
    """
    try:
        now = _as_utc(now)
        latest = now + timedelta(days=1)
        for key in DATE_KEYS:
            value = item.get(key)
            if not isinstance(value, str):
                continue
            try:
                moment = _parse(value, now)
            except Exception:
                continue
            if moment is not None and _EARLIEST <= moment <= latest:
                return moment.date().isoformat()
    except Exception:
        pass
    return None
