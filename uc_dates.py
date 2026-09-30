"""Date normalisation for UnknownCheats pages.

Everything the forum renders is in the logged-in user's vBulletin timezone
(UC's footer reports it, e.g. "All times are GMT +1"), and it comes in three
shapes:

    "12th December 2015, 11:38 AM"   long form, post headers
    "Today, 11:33 PM" / "Today 11:21 PM"
    "Yesterday, 04:02 AM"
    "4th January 2025 at 03:12 PM"  edit markers use "at" as the separator
    "12-25-2015"                     bare month-day-year, rare

Every one is converted to an aware UTC datetime, then rendered as an ISO date
plus a human age, because on a game-hacking forum the age of a post is the
single most important thing about it: offsets, signatures and bypass
techniques rot within weeks.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)
}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})

_LONG = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})"
    r"(?:\s*(?:,|\bat\b)?\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?)?"
)
_REL = re.compile(r"\b(Today|Yesterday)\b\s*,?\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")
_NUM = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")
_TZ = re.compile(r"All times are GMT\s*([+-]?\d{1,2})", re.I)

DEFAULT_TZ_OFFSET = 1  # UC default; overridden by what the live footer reports.


def detect_tz_offset(html: str) -> int | None:
    """Read the forum's declared timezone offset out of a page footer."""
    m = _TZ.search(html)
    return int(m.group(1)) if m else None


def _to_24h(hour: int, meridiem: str | None) -> int:
    if not meridiem:
        return hour
    meridiem = meridiem.lower()
    if meridiem == "pm" and hour != 12:
        return hour + 12
    if meridiem == "am" and hour == 12:
        return 0
    return hour


def parse(text: str, tz_offset: int = DEFAULT_TZ_OFFSET,
          now: datetime | None = None) -> datetime | None:
    """Parse any UC-rendered date string into an aware UTC datetime."""
    if not text:
        return None
    tz = timezone(timedelta(hours=tz_offset))
    now = now or datetime.now(timezone.utc)
    local_today: date = now.astimezone(tz).date()

    m = _REL.search(text)
    if m:
        word, hh, mm, mer = m.groups()
        day = local_today if word.lower() == "today" else local_today - timedelta(days=1)
        local = datetime(day.year, day.month, day.day,
                         _to_24h(int(hh), mer), int(mm), tzinfo=tz)
        return local.astimezone(timezone.utc)

    m = _LONG.search(text)
    if m:
        dd, mon, yyyy, hh, mm, mer = m.groups()
        month = MONTHS.get(mon.lower()) or MONTHS.get(mon[:3].lower())
        if month:
            local = datetime(int(yyyy), month, int(dd),
                             _to_24h(int(hh), mer) if hh else 0,
                             int(mm) if mm else 0, tzinfo=tz)
            return local.astimezone(timezone.utc)

    m = _NUM.search(text)
    if m:
        mo, dd, yyyy = (int(x) for x in m.groups())
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            return datetime(yyyy, mo, dd, tzinfo=tz).astimezone(timezone.utc)
    return None


def age(dt: datetime | None, now: datetime | None = None) -> str:
    """Compact human age: 3m, 5h, 12d, 7mo, 4y."""
    if dt is None:
        return "?"
    now = now or datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "0m"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m"
    if mins < 1440:
        return f"{int(mins // 60)}h"
    days = mins / 1440
    if days < 60:
        return f"{int(days)}d"
    if days < 365:
        return f"{int(days // 30)}mo"
    years = days / 365.25
    return f"{years:.1f}y" if years < 10 else f"{int(years)}y"


# Rot tiers, tuned for a game-hacking forum where offsets and bypasses die fast.
FRESH_DAYS = 90
AGING_DAYS = 365
OLD_DAYS = 3 * 365


def staleness(dt: datetime | None, now: datetime | None = None) -> str:
    """One of: fresh, aging, old, ancient, unknown."""
    if dt is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    days = (now - dt).days
    if days <= FRESH_DAYS:
        return "fresh"
    if days <= AGING_DAYS:
        return "aging"
    if days <= OLD_DAYS:
        return "old"
    return "ancient"


def stamp(dt: datetime | None, now: datetime | None = None) -> str:
    """The canonical inline rendering used everywhere: `2015-12-12 (10y)`."""
    if dt is None:
        return "date?"
    return f"{dt.strftime('%Y-%m-%d')} ({age(dt, now)})"


def iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ") if dt else "?"
