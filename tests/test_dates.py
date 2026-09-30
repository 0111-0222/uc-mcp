from datetime import datetime, timezone

import pytest

import uc_dates

UTC = timezone.utc


@pytest.mark.parametrize("text, expected", [
    # Forum renders GMT+1, so every local time is one hour ahead of UTC.
    ("12th December 2015, 11:38 AM", datetime(2015, 12, 12, 10, 38, tzinfo=UTC)),
    ("1st January 2020, 12:05 AM", datetime(2019, 12, 31, 23, 5, tzinfo=UTC)),
    ("4th January 2025 at 03:12 PM", datetime(2025, 1, 4, 14, 12, tzinfo=UTC)),
    ("22nd Feb 2024, 12:00 PM", datetime(2024, 2, 22, 11, 0, tzinfo=UTC)),
    ("Today, 11:33 PM", datetime(2026, 9, 30, 22, 33, tzinfo=UTC)),
    ("Today 11:21 PM", datetime(2026, 9, 30, 22, 21, tzinfo=UTC)),
    ("Yesterday, 04:02 AM", datetime(2026, 9, 29, 3, 2, tzinfo=UTC)),
    ("12-25-2015", datetime(2015, 12, 24, 23, 0, tzinfo=UTC)),
])
def test_parse_shapes(text, expected, now):
    assert uc_dates.parse(text, 1, now) == expected


def test_parse_respects_offset(now):
    assert uc_dates.parse("1st June 2024, 10:00 AM", -5, now) == \
        datetime(2024, 6, 1, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize("text", ["", "no date here", "Page 3 of 9"])
def test_parse_rejects_non_dates(text, now):
    assert uc_dates.parse(text, 1, now) is None


def test_detect_tz_offset():
    assert uc_dates.detect_tz_offset("All times are GMT +1. The time now is") == 1
    assert uc_dates.detect_tz_offset("All times are GMT -5.") == -5
    assert uc_dates.detect_tz_offset("no footer") is None


@pytest.mark.parametrize("days, expected", [
    (10, "fresh"), (200, "aging"), (800, "old"), (4000, "ancient"),
])
def test_staleness(days, expected, now):
    from datetime import timedelta
    assert uc_dates.staleness(now - timedelta(days=days), now) == expected


def test_staleness_unknown():
    assert uc_dates.staleness(None) == "unknown"


def test_stamp_and_age(now):
    assert uc_dates.stamp(datetime(2015, 12, 12, tzinfo=UTC), now) == "2015-12-12 (10y)"
    assert uc_dates.stamp(datetime(2022, 3, 30, tzinfo=UTC), now) == "2022-03-30 (4.5y)"
    assert uc_dates.stamp(None) == "date?"
    assert uc_dates.age(datetime(2026, 9, 30, 9, 0, tzinfo=UTC), now) == "3h"
