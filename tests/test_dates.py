"""Publication date of a search result: parser table, key order, rejects."""

from datetime import datetime, timedelta, timezone

import pytest

from wsp_core.dates import published_date

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw, expected", [
    # ISO 8601 / RFC 3339
    ("2026-01-05", "2026-01-05"),
    ("2026-01-05T10:00:00Z", "2026-01-05"),
    ("2026-09-05T00:00:00.000Z", "2026-09-05"),
    ("2026-01-05T10:00:00", "2026-01-05"),  # no zone: read as UTC
    ("2026-01-05 10:00:00", "2026-01-05"),
    ("2026-01-05T23:30:00-05:00", "2026-01-06"),  # offsets resolve to the UTC date
    ("2026-01-05T00:30:00+02:00", "2026-01-04"),
    ("2026-01-05T23:30:00-0500", "2026-01-06"),
    ("2026-01-05T10:00:00.123456789+00:00", "2026-01-05"),
    ("  2026-01-05  ", "2026-01-05"),
    # RFC 2822
    ("Mon, 05 Jan 2026 10:00:00 GMT", "2026-01-05"),
    ("Mon, 05 Jan 2026 23:30:00 -0500", "2026-01-06"),
    ("Tue, 07 Oct 2026 08:15:00 +0000", "2026-10-07"),
    # month names
    ("Jan 5, 2026", "2026-01-05"),
    ("January 5, 2026", "2026-01-05"),
    ("5 Jan 2026", "2026-01-05"),
    ("05 January 2026", "2026-01-05"),
    ("Sept. 5, 2026", "2026-09-05"),
    ("dec 31 2025", "2025-12-31"),
    # relative to NOW = 2026-10-08 14:30 UTC
    ("yesterday", "2026-10-07"),
    ("Yesterday", "2026-10-07"),
    ("1 day ago", "2026-10-07"),
    ("3 days ago", "2026-10-05"),
    ("0 days ago", "2026-10-08"),
    ("2 weeks ago", "2026-09-24"),
    ("1 month ago", "2026-09-08"),  # a month is 30 days
    ("2 months ago", "2026-08-09"),
    ("1 year ago", "2025-10-08"),  # a year is 365 days
    ("2 years ago", "2024-10-08"),
    ("5 minutes ago", "2026-10-08"),
    ("17 mins ago", "2026-10-08"),
    ("14 hours ago", "2026-10-08"),
    ("15 hours ago", "2026-10-07"),  # 14:30 minus 15 h crosses midnight
    ("2 HOURS AGO", "2026-10-08"),
    ("1 hour ago", "2026-10-08"),
])
def test_accepted_forms(raw, expected):
    assert published_date({"date": raw}, now=NOW) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "not a date",
    "today",
    "just now",
    "last week",
    "3 days from now",
    "in 3 days",
    "3 fortnights ago",
    "2026",
    "2026-01",
    "20260105",
    "2026-13-01",  # month out of range
    "2026-02-30",  # day out of range
    "2026-01-05T25:00:00Z",
    "2026-01-05T10:00:00+25:00",
    "Foo 5, 2026",
    "Feb 30, 2026",
    "Mon, 05 Jan 2026",  # RFC 2822 shape without a time
    "1989-12-31",  # before 1990-01-01
    "1970-01-01T00:00:00Z",  # epoch placeholder
    "Dec 31, 1989",
    "\u0663 days ago",  # non-ASCII digits
    "\uff12\uff10\uff12\uff16-01-05",
    "40 years ago",
    "99999999 years ago",
    "2026-10-10",  # more than a day after NOW
    "2026-10-09T14:31:00Z",  # one minute past the one-day allowance
    "Oct 20, 2026",
    "2099-01-01T00:00:00Z",
    "x" * 5000,
])
def test_rejected_values(raw):
    assert published_date({"date": raw}, now=NOW) is None


@pytest.mark.parametrize("raw, expected", [
    ("1990-01-01", "1990-01-01"),  # the lower bound is inclusive
    ("2026-10-08T14:30:00Z", "2026-10-08"),  # now
    ("2026-10-09", "2026-10-09"),  # a day ahead tolerates clock and zone skew
    ("2026-10-09T14:30:00Z", "2026-10-09"),  # exactly one day after now
])
def test_range_boundaries(raw, expected):
    assert published_date({"published_at": raw}, now=NOW) == expected


@pytest.mark.parametrize("value", [None, 0, 20260105, 1.5, True, ["2026-01-05"], {"d": "2026-01-05"}])
def test_non_string_values_are_ignored(value):
    assert published_date({"date": value}, now=NOW) is None


@pytest.mark.parametrize("key", [
    "published_at", "published_date", "publish_date", "publishedDate", "date", "page_age", "age",
])
def test_every_documented_key_is_read(key):
    assert published_date({key: "2026-03-04"}, now=NOW) == "2026-03-04"


def test_key_order_prefers_the_first_usable_key():
    order = ("published_at", "published_date", "publish_date", "publishedDate", "date", "page_age")
    item = {key: f"2026-10-0{6 - i}" for i, key in enumerate(order)}
    item["age"] = "5 days ago"
    for i, key in enumerate(order):
        assert published_date(item, now=NOW) == f"2026-10-0{6 - i}"
        del item[key]
    assert published_date(item, now=NOW) == "2026-10-03"  # only "age" is left: 5 days before NOW


def test_an_unusable_value_falls_through_to_the_next_key():
    # An epoch placeholder or free text in an earlier key must not hide a good later one.
    item = {"published_date": "1970-01-01T00:00:00Z", "date": "last week", "age": "2 days ago"}
    assert published_date(item, now=NOW) == "2026-10-06"


def test_missing_and_empty_items_return_none():
    assert published_date({}, now=NOW) is None
    assert published_date({"title": "t", "url": "https://a.test"}, now=NOW) is None


def test_now_is_converted_to_utc_and_naive_now_is_read_as_utc():
    plus_two = timezone(timedelta(hours=2))
    # 2026-10-08 01:30 +02:00 is 2026-10-07 23:30 UTC.
    local_now = datetime(2026, 10, 8, 1, 30, tzinfo=plus_two)
    assert published_date({"date": "1 hour ago"}, now=local_now) == "2026-10-07"
    assert published_date({"date": "yesterday"}, now=local_now) == "2026-10-06"
    assert published_date({"date": "1 hour ago"}, now=datetime(2026, 10, 8, 0, 30)) == "2026-10-07"


def test_hours_cross_the_calendar_date_relative_to_now():
    early = datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc)
    assert published_date({"date": "30 minutes ago"}, now=early) == "2026-03-01"
    assert published_date({"date": "2 hours ago"}, now=early) == "2026-02-28"
    assert published_date({"date": "3 days ago"}, now=early) == "2026-02-26"


@pytest.mark.parametrize("bad_item", [None, "2026-01-05", 5, [("date", "2026-01-05")]])
def test_never_raises_on_bad_items(bad_item):
    assert published_date(bad_item, now=NOW) is None


def test_never_raises_on_bad_now():
    assert published_date({"date": "2026-01-05"}, now=None) is None
    assert published_date({"date": "3 days ago"}, now="2026-10-08") is None
