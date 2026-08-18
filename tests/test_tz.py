from datetime import UTC, datetime

import tz


def test_is_valid_timezone():
    assert tz.is_valid_timezone("America/Denver")
    assert tz.is_valid_timezone("UTC")
    assert not tz.is_valid_timezone("Not/AZone")
    assert not tz.is_valid_timezone(None)
    assert not tz.is_valid_timezone("")


def test_normalize_timezone_falls_back_to_utc():
    assert tz.normalize_timezone("America/Denver") == "America/Denver"
    assert tz.normalize_timezone("bogus") == "UTC"
    assert tz.normalize_timezone(None) == "UTC"


def test_to_local_converts_known_offset():
    # 2026-07-25 20:30 UTC -> summer in Denver is UTC-6 (MDT).
    result = tz.to_local("2026-07-25T20:30:00.000Z", "America/Denver")
    assert result == "2026-07-25 14:30 MDT"


def test_to_local_utc_passthrough():
    result = tz.to_local("2026-07-25T20:30:00.000Z", "UTC")
    assert result == "2026-07-25 20:30 UTC"


def test_to_local_handles_empty_and_unparseable_input():
    assert tz.to_local(None, "America/Denver") == ""
    assert tz.to_local("", "America/Denver") == ""
    assert tz.to_local("not-a-timestamp", "America/Denver") == "not-a-timestamp"


def test_to_local_invalid_timezone_falls_back_to_utc():
    result = tz.to_local("2026-07-25T20:30:00.000Z", "totally/bogus")
    assert result == "2026-07-25 20:30 UTC"


def test_common_timezones_are_all_valid():
    for name in tz.COMMON_TIMEZONES:
        assert tz.is_valid_timezone(name), f"{name} is not a real IANA timezone"


def test_today_local_rolls_over_on_the_accounts_day_not_the_servers():
    """The shift-date default must follow the warehouse's calendar.

    date.today() reads the host's timezone. On a UTC-configured server --
    the normal case -- an owner in Los Angeles preparing an evening shift
    after 17:00 local would get tomorrow's date pre-filled, and print a
    wall QR labelled with the wrong day.
    """
    # 2026-08-17 02:30 UTC is still 2026-08-16 in Los Angeles.
    moment = datetime(2026, 8, 17, 2, 30, tzinfo=UTC)
    assert tz.today_local("America/Los_Angeles", now=moment) == "2026-08-16"
    assert tz.today_local("UTC", now=moment) == "2026-08-17"
    # ...and ahead of UTC it can already be the next day.
    assert tz.today_local("Asia/Tokyo", now=moment) == "2026-08-17"

    evening = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
    assert tz.today_local("Asia/Tokyo", now=evening) == "2026-08-18"


def test_today_local_falls_back_to_utc_for_an_unknown_zone():
    moment = datetime(2026, 8, 17, 2, 30, tzinfo=UTC)
    assert tz.today_local("Not/AZone", now=moment) == "2026-08-17"
