"""Timezone display helpers for the owner web app.

Every timestamp is stored in UTC (SQLite's `strftime('...','now')` and
Python's `datetime.now(timezone.utc)` both produce UTC) -- that never
changes. This module converts a stored UTC string to an account's chosen
IANA timezone for DISPLAY only. Billing-critical logic (the ambiguity
guard, `ts_server` ordering, the ~5-billion-things that key off "when did
this actually happen on the server") always stays in UTC; nothing here
touches storage or comparison, only what a human reads on a page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TIMEZONE = "UTC"

# A curated subset for the signup dropdown -- the full IANA database
# (~500 zones) is more choice than a small warehouse picking one option
# needs. Any valid IANA name still works if set some other way (e.g. the
# operator GUI), this list is just what's offered up front.
COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Mexico_City",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
]

_VALID_TIMEZONES = available_timezones()


def is_valid_timezone(name: str | None) -> bool:
    return bool(name) and name in _VALID_TIMEZONES


def normalize_timezone(name: str | None) -> str:
    # Narrowed with an explicit `name is not None` rather than relying on
    # is_valid_timezone() to imply it: the truth is the same either way,
    # but only this form is visible to a type checker.
    if name is not None and is_valid_timezone(name):
        return name
    return DEFAULT_TIMEZONE


def local_day_bounds_utc(tz_name: str, now: datetime | None = None) -> tuple[str, str]:
    """The UTC half-open range [start, end) covering "today" in tz_name,
    formatted to match how timestamps are stored.

    Storage stays UTC, but deciding which day a scan falls in is a
    bucketing question, not a formatting one: an owner in Los Angeles
    expects "scans today" to roll over at their midnight, not at 16:00
    local when UTC happens to tick over mid-shift.
    """
    zone = ZoneInfo(normalize_timezone(tz_name))
    now_local = (now or datetime.now(UTC)).astimezone(zone)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)

    def _stamp(dt: datetime) -> str:
        # Millisecond precision, matching both SQLite's
        # strftime('%Y-%m-%dT%H:%M:%fZ') and app.py's _now_iso(), so these
        # bounds compare correctly against stored values as plain strings.
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return _stamp(start_local), _stamp(end_local)


def today_local(tz_name: str, now: datetime | None = None) -> str:
    """Today's calendar date in tz_name, as YYYY-MM-DD.

    `date.today()` reads the *server's* local timezone, which is whatever
    the host happens to be set to and has nothing to do with the warehouse
    using the app. On a UTC-configured box -- the normal case for a
    server -- an owner in Los Angeles preparing an evening shift after
    17:00 local gets tomorrow's date pre-filled in the shift form, because
    UTC has already rolled over. They then print a wall QR labelled with
    the wrong day.

    Same reasoning as local_day_bounds_utc above: storage stays UTC, but
    "which calendar day is it" is a question about the account's timezone.
    """
    zone = ZoneInfo(normalize_timezone(tz_name))
    return (now or datetime.now(UTC)).astimezone(zone).date().isoformat()


def to_local(utc_iso: str | None, tz_name: str, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Convert a stored UTC timestamp string to a formatted local-time
    string. Never raises -- a display helper degrades to the raw value
    on anything unparseable rather than breaking the page."""
    if not utc_iso:
        return ""
    try:
        dt = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return utc_iso
    local_dt = dt.astimezone(ZoneInfo(normalize_timezone(tz_name)))
    return local_dt.strftime(fmt)
