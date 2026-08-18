"""Regression tests for billing, timezone and atomicity defects.

As with test_security_regressions.py, each docstring records the bug the
test exists to prevent from coming back.
"""

from datetime import UTC, datetime

import pytest

import billing
import db
import manifest_ingest
import tz


def _account(conn, free_allowance=2, price=900, tz_name="UTC"):
    account_id = db.create_account(conn, "Acme", price, free_allowance, timezone=tz_name)
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-26", "tok", "2099-01-01T00:00:00.000Z", "h", 0
    )
    worker_id = db.get_or_create_worker(conn, account_id, "W")
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    return account_id, session_id


def _catch(conn, session_id, scan_uuid, ts_server=None):
    row = {
        "uuid": scan_uuid,
        "session_id": session_id,
        "raw_payload": "X",
        "normalized": "X",
        "result": "reject",
        "ts_client": "2026-07-26T00:00:00.000Z",
        "bundle_version": 0,
    }
    if ts_server:
        row["ts_server"] = ts_server
    db.insert_scan(conn, row)


# ---------------------------------------------------------------------------
# Reversed catches
# ---------------------------------------------------------------------------


def test_reversed_catch_does_not_consume_the_free_allowance(tmp_path):
    """count_billable_catches counted every 'catch' row regardless of an
    offsetting reversal, so a reject that was later disproven still ate a
    free-tier slot and pushed the next genuine catch into being billed."""
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _account(conn, free_allowance=2)

    for scan_uuid in ("c1", "c2"):
        _catch(conn, session_id, scan_uuid)
        billing.process_scan_for_billing(conn, account_id, scan_uuid)
    assert db.count_billable_catches(conn, account_id) == 2

    billing.reverse_if_billed(conn, "c2", "appeal approved: item was on the manifest", "owner")
    assert db.count_billable_catches(conn, account_id) == 1

    # Only one catch still stands, so with an allowance of 2 the next one
    # is still free.
    _catch(conn, session_id, "c3")
    billing.process_scan_for_billing(conn, account_id, "c3")
    assert (
        db.query_one(
            conn, "SELECT cents FROM billing_events WHERE scan_uuid='c3' AND kind='catch'"
        )["cents"]
        == 0
    )

    # And the allowance still runs out at the right point.
    _catch(conn, session_id, "c4")
    billing.process_scan_for_billing(conn, account_id, "c4")
    assert (
        db.query_one(
            conn, "SELECT cents FROM billing_events WHERE scan_uuid='c4' AND kind='catch'"
        )["cents"]
        == 900
    )


def test_reversed_catch_is_not_counted_as_money_saved(tmp_path):
    """savings_to_date_cents (owner dashboard, savings report, marketing
    figure) counted catches the system itself had concluded weren't
    catches."""
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _account(conn, free_allowance=0)

    for scan_uuid in ("s1", "s2"):
        _catch(conn, session_id, scan_uuid)
        billing.process_scan_for_billing(conn, account_id, scan_uuid)
    savings_before = billing.savings_to_date_cents(conn, account_id)

    billing.reverse_if_billed(conn, "s2", "mistaken reject", "owner")
    assert billing.savings_to_date_cents(conn, account_id) == savings_before // 2
    assert billing.net_amount_owed_cents(conn, account_id) == 900


def test_credits_are_scoped_to_the_requested_window(tmp_path):
    """Events were date-filtered but credits were not, so any per-period
    invoice would subtract every credit ever granted, in every period."""
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _account(conn, free_allowance=0)
    _catch(conn, session_id, "s1")
    billing.process_scan_for_billing(conn, account_id, "s1")

    db.grant_credit(conn, account_id, 900, "goodwill", "operator")
    conn.execute("UPDATE account_credits SET created_at = '2020-01-01T00:00:00.000Z'")

    # Whole-history view: the credit applies.
    assert billing.net_amount_owed_cents(conn, account_id) == 0
    # A window that excludes the credit must not apply it.
    assert billing.net_amount_owed_cents(conn, account_id, start="2026-01-01T00:00:00.000Z") == 900


# ---------------------------------------------------------------------------
# Timezone-aware "today"
# ---------------------------------------------------------------------------


def test_local_day_bounds_follow_the_account_timezone():
    """'Scans today' compared date(ts_server) to UTC date('now'), so the
    counter rolled over at 16:00 local in Los Angeles and 09:00 in Tokyo
    instead of at the account's own midnight."""
    now = datetime(2026, 7, 26, 23, 30, tzinfo=UTC)
    assert tz.local_day_bounds_utc("UTC", now) == (
        "2026-07-26T00:00:00.000Z",
        "2026-07-27T00:00:00.000Z",
    )
    assert tz.local_day_bounds_utc("America/Los_Angeles", now) == (
        "2026-07-26T07:00:00.000Z",
        "2026-07-27T07:00:00.000Z",
    )
    assert tz.local_day_bounds_utc("Asia/Tokyo", now) == (
        "2026-07-26T15:00:00.000Z",
        "2026-07-27T15:00:00.000Z",
    )


def test_invalid_timezone_falls_back_to_utc():
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    assert tz.local_day_bounds_utc("Not/AZone", now) == tz.local_day_bounds_utc("UTC", now)


def test_scans_today_counts_the_accounts_local_day(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _account(conn, tz_name="Asia/Tokyo")

    # 23:30 UTC on the 26th is already 08:30 on the 27th in Tokyo, so this
    # scan belongs to the Tokyo account's *next* day, not the UTC one.
    _catch(conn, session_id, "late", ts_server="2026-07-26T23:30:00.000Z")

    now = datetime(2026, 7, 26, 23, 45, tzinfo=UTC)
    utc_start, utc_end = tz.local_day_bounds_utc("UTC", now)
    tokyo_start, tokyo_end = tz.local_day_bounds_utc("Asia/Tokyo", now)

    assert db.count_scans_today(conn, account_id, utc_start, utc_end) == 1
    assert db.count_scans_today(conn, account_id, tokyo_start, tokyo_end) == 1

    # Earlier the same UTC day, but the previous Tokyo day.
    _catch(conn, session_id, "early", ts_server="2026-07-26T02:00:00.000Z")
    assert db.count_scans_today(conn, account_id, utc_start, utc_end) == 2
    assert db.count_scans_today(conn, account_id, tokyo_start, tokyo_end) == 1


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_every_write_in_the_group(tmp_path):
    """Connections are autocommit, so without an explicit transaction a
    failure halfway through a multi-write operation left partial state."""
    conn = db.init_db(tmp_path / "t.db")
    account_id, _session_id = _account(conn)
    before = db.query_one(conn, "SELECT COUNT(*) n FROM workers")["n"]

    with pytest.raises(RuntimeError), db.transaction(conn):
        db.get_or_create_worker(conn, account_id, "Partial A")
        db.get_or_create_worker(conn, account_id, "Partial B")
        raise RuntimeError("boom")

    assert db.query_one(conn, "SELECT COUNT(*) n FROM workers")["n"] == before


def test_transaction_commits_on_success(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, _session_id = _account(conn)
    with db.transaction(conn):
        db.get_or_create_worker(conn, account_id, "Committed")
    assert (
        db.query_one(conn, "SELECT COUNT(*) n FROM workers WHERE display_name = 'Committed'")["n"]
        == 1
    )


def test_transaction_is_reentrant(tmp_path):
    """Nested use must join the outer transaction rather than raising
    SQLite's 'cannot start a transaction within a transaction'."""
    conn = db.init_db(tmp_path / "t.db")
    account_id, _session_id = _account(conn)
    with db.transaction(conn), db.transaction(conn):
        db.get_or_create_worker(conn, account_id, "Nested")
    assert (
        db.query_one(conn, "SELECT COUNT(*) n FROM workers WHERE display_name = 'Nested'")["n"] == 1
    )


def test_commit_manifest_is_atomic(tmp_path, monkeypatch):
    """A manifest marked 'committed' but missing some line_keys silently
    fails to match items that really are on it -- which a worker sees as a
    false REJECT."""
    conn = db.init_db(tmp_path / "t.db")
    account_id = db.create_account(conn, "Acme", 900, 25)
    rows = [
        {
            "line_no": i,
            "sku": f"S{i}",
            "description": "d",
            "qty_expected": 1,
            "raw_barcode": f"00000{i:05d}",
        }
        for i in range(1, 6)
    ]

    monkeypatch.setattr(
        db, "insert_line_keys", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        manifest_ingest.commit_manifest(conn, account_id, "REF", None, rows, False, 8)

    assert db.query_one(conn, "SELECT COUNT(*) n FROM manifests")["n"] == 0
    assert db.query_one(conn, "SELECT COUNT(*) n FROM manifest_lines")["n"] == 0


def test_merge_workers_is_atomic(tmp_path, monkeypatch):
    """Reattributing the sessions but failing to delete the source worker
    leaves a phantom zero-scan worker; the reverse orphans every session
    that pointed at the deleted row."""
    conn = db.init_db(tmp_path / "t.db")
    account_id, _ = _account(conn)
    shift_id = db.query_one(conn, "SELECT id FROM shifts LIMIT 1")["id"]
    keep = db.get_or_create_worker(conn, account_id, "Keep")
    drop = db.get_or_create_worker(conn, account_id, "Drop")
    drop_session = db.create_session(conn, shift_id, drop, "ua")

    real_execute = db.execute
    calls = {"n": 0}

    def flaky(conn_, sql, params=()):
        calls["n"] += 1
        if calls["n"] == 2:  # the DELETE half of the merge
            raise RuntimeError("boom")
        return real_execute(conn_, sql, params)

    monkeypatch.setattr(db, "execute", flaky)
    with pytest.raises(RuntimeError):
        db.merge_workers(conn, drop, keep)
    monkeypatch.undo()

    # Neither half applied: the source worker still exists, and the
    # session it owned was not reattributed to the survivor.
    assert db.get_worker(conn, drop) is not None
    assert db.get_session(conn, drop_session)["worker_id"] == drop

    # And the merge succeeds normally once the failure is removed.
    db.merge_workers(conn, drop, keep)
    assert db.get_worker(conn, drop) is None
    assert db.get_session(conn, drop_session)["worker_id"] == keep
