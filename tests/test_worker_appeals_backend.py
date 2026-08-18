"""Backend (db.py/billing.py) pieces behind the worker appeal feature:
photo+note exceptions (kind='worker_reported'), idempotent submission,
and reversing a mistaken 'catch' billing event when an appeal (or any
exception) is resolved to a real manifest line.
"""

import billing
import db


def _setup(conn, free_allowance=0, price=900):
    account_id = db.create_account(conn, "Acme", price, free_allowance)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    return account_id, worker_id, session_id


def _reject_scan(conn, session_id, uuid_):
    db.insert_scan(
        conn,
        {
            "uuid": uuid_,
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "reject",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )


def test_create_worker_appeal_is_idempotent(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn)
    _reject_scan(conn, session_id, "r1")

    first = db.create_worker_appeal(conn, "r1", "/photos/r1.jpg", "I think this is right")
    second = db.create_worker_appeal(conn, "r1", "/photos/r1.jpg", "retry after network drop")
    assert first is not None
    assert second is None

    rows = db.query(conn, "SELECT * FROM exceptions WHERE scan_uuid = 'r1'")
    assert len(rows) == 1
    assert rows[0]["kind"] == "worker_reported"
    assert rows[0]["photo_path"] == "/photos/r1.jpg"
    assert rows[0]["worker_note"] == "I think this is right"


def test_list_open_exceptions_includes_worker_name_and_photo(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn)
    _reject_scan(conn, session_id, "r1")
    db.create_worker_appeal(conn, "r1", "/photos/r1.jpg", "note here")

    exceptions = db.list_open_exceptions(conn, account_id)
    assert len(exceptions) == 1
    assert exceptions[0]["worker_name"] == "Bob"
    assert exceptions[0]["photo_path"] == "/photos/r1.jpg"


def test_reverse_if_billed_reverses_a_catch(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn, free_allowance=0, price=900)
    _reject_scan(conn, session_id, "r1")
    billing.process_scan_for_billing(conn, account_id, "r1")
    assert billing.net_amount_owed_cents(conn, account_id) == 900

    reversal_id = billing.reverse_if_billed(conn, "r1", "Worker appeal approved", "owner@acme.test")
    assert reversal_id is not None
    assert billing.net_amount_owed_cents(conn, account_id) == 0

    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "billing.reverse" for a in audit_rows)


def test_reverse_if_billed_is_none_when_scan_was_never_billed(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn)
    _reject_scan(conn, session_id, "r1")
    # Never billed -- e.g. it was 'unresolved', not 'reject'.
    assert billing.reverse_if_billed(conn, "r1", "n/a", "owner@acme.test") is None


def test_worker_stats_billed_cents_nets_out_reversals(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn, free_allowance=0, price=900)
    _reject_scan(conn, session_id, "r1")
    billing.process_scan_for_billing(conn, account_id, "r1")

    stats_before = db.worker_stats_for_account(conn, account_id)
    assert stats_before[0]["billed_cents"] == 900

    billing.reverse_if_billed(conn, "r1", "appeal approved", "owner@acme.test")

    stats_after = db.worker_stats_for_account(conn, account_id)
    assert stats_after[0]["billed_cents"] == 0
    assert stats_after[0]["reject_count"] == 1  # the scan itself is still counted


def test_worker_stats_breaks_down_result_counts_per_worker(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn)
    for i, result in enumerate(["ok", "ok", "reject", "duplicate", "unresolved"]):
        db.insert_scan(
            conn,
            {
                "uuid": f"s{i}",
                "session_id": session_id,
                "raw_payload": "X",
                "normalized": "X",
                "matched_tier": 1 if result in ("ok", "duplicate") else None,
                "result": result,
                "ts_client": "2026-07-25T00:00:00Z",
                "bundle_version": 0,
            },
        )
    stats = db.worker_stats_for_account(conn, account_id)
    row = stats[0]
    assert row["total_scans"] == 5
    assert row["ok_count"] == 2
    assert row["reject_count"] == 1
    assert row["duplicate_count"] == 1
    assert row["unresolved_count"] == 1


def test_worker_session_stats_matches_that_sessions_scans_only(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, worker_id, session_id = _setup(conn)
    other_worker_id = db.get_or_create_worker(conn, account_id, "Other")
    other_session_id = db.create_session(
        conn, db.get_session(conn, session_id)["shift_id"], other_worker_id, "ua"
    )

    _reject_scan(conn, session_id, "mine-1")
    db.insert_scan(
        conn,
        {
            "uuid": "other-1",
            "session_id": other_session_id,
            "raw_payload": "Y",
            "normalized": "Y",
            "matched_tier": 1,
            "result": "ok",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )

    stats = db.worker_session_stats(conn, session_id)
    assert stats["total_scans"] == 1
    assert stats["reject_count"] == 1
    assert stats["ok_count"] == 0
