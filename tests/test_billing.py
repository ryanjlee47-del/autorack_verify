import billing
import db
import pricing


def _setup(conn, free_allowance=2, price=900):
    account_id = db.create_account(conn, "Acme", price, free_allowance)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    return account_id, session_id


def _insert_reject(conn, session_id, uuid_):
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


def test_process_scan_for_billing_is_free_within_allowance(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _setup(conn, free_allowance=2, price=900)

    _insert_reject(conn, session_id, "r1")
    ev1 = billing.process_scan_for_billing(conn, account_id, "r1")
    assert db.query_one(conn, "SELECT cents FROM billing_events WHERE id = ?", (ev1,))["cents"] == 0

    _insert_reject(conn, session_id, "r2")
    ev2 = billing.process_scan_for_billing(conn, account_id, "r2")
    assert db.query_one(conn, "SELECT cents FROM billing_events WHERE id = ?", (ev2,))["cents"] == 0

    # Third catch is past the free allowance of 2 -- full price.
    _insert_reject(conn, session_id, "r3")
    ev3 = billing.process_scan_for_billing(conn, account_id, "r3")
    assert (
        db.query_one(conn, "SELECT cents FROM billing_events WHERE id = ?", (ev3,))["cents"] == 900
    )


def test_process_scan_for_billing_ignores_non_reject_scans(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _setup(conn)
    db.insert_scan(
        conn,
        {
            "uuid": "ok1",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": 1,
            "result": "ok",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    result = billing.process_scan_for_billing(conn, account_id, "ok1")
    assert result is None
    assert not db.billing_event_exists_for_scan(conn, "ok1")


def test_process_scan_for_billing_is_idempotent(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _setup(conn)
    _insert_reject(conn, session_id, "r1")
    first = billing.process_scan_for_billing(conn, account_id, "r1")
    second = billing.process_scan_for_billing(conn, account_id, "r1")
    assert first is not None
    assert second is None
    rows = db.query(conn, "SELECT * FROM billing_events WHERE scan_uuid = 'r1'")
    assert len(rows) == 1


def test_confirm_exception_as_billable_catch_requires_resolved_exception(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _setup(conn)
    db.insert_scan(
        conn,
        {
            "uuid": "u1",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "unresolved",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    exc_id = db.create_exception(conn, "u1", "unresolved")

    # Not yet resolved -- must not bill.
    assert billing.confirm_exception_as_billable_catch(conn, account_id, "u1") is None

    db.resolve_exception(conn, exc_id, "owner@acme.test", "confirmed wrong item")
    result = billing.confirm_exception_as_billable_catch(conn, account_id, "u1")
    assert result is not None
    assert db.billing_event_exists_for_scan(conn, "u1")


def test_reverse_billing_event_requires_reason_and_logs_audit(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _setup(conn, free_allowance=0, price=900)
    _insert_reject(conn, session_id, "r1")
    event_id = billing.process_scan_for_billing(conn, account_id, "r1")

    try:
        billing.reverse_billing_event(conn, event_id, "", "owner@acme.test")
        raise AssertionError("expected ValueError for empty reason")
    except ValueError:
        pass

    reversal_id = billing.reverse_billing_event(
        conn, event_id, "Customer disputed, item was correct", "owner@acme.test"
    )
    reversal = db.query_one(conn, "SELECT * FROM billing_events WHERE id = ?", (reversal_id,))
    assert reversal["kind"] == "reversal"
    assert reversal["cents"] == -900

    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "billing.reverse" for a in audit_rows)

    net = billing.net_amount_owed_cents(conn, account_id)
    assert net == 0  # catch (900) + reversal (-900)


def test_savings_to_date_uses_pricing_constant_not_billed_amount(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, session_id = _setup(conn, free_allowance=100, price=900)  # everything free
    _insert_reject(conn, session_id, "r1")
    billing.process_scan_for_billing(conn, account_id, "r1")

    savings = billing.savings_to_date_cents(conn, account_id)
    assert savings == pricing.SAVINGS_PER_CATCH_CENTS
    # Confirms savings framing is independent of what was actually billed
    # (here: $0 billed, since it was within the free allowance).
    assert billing.net_amount_owed_cents(conn, account_id) == 0
