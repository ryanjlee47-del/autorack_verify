import db
import seed


def _setup(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    info = seed.ensure_seeded(conn)
    return conn, info["account_id"]


def test_merge_workers_reassigns_sessions_and_deletes_duplicate(tmp_path):
    conn, account_id = _setup(tmp_path)
    worker_a = db.get_or_create_worker(conn, account_id, "Bob")
    worker_b = db.get_or_create_worker(conn, account_id, "bob")
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    session_b = db.create_session(conn, shift_id, worker_b, "ua")

    db.merge_workers(conn, worker_b, worker_a)

    assert db.get_worker(conn, worker_b) is None
    session = db.get_session(conn, session_b)
    assert session["worker_id"] == worker_a


def test_merge_workers_preserves_scans_and_billing_via_session(tmp_path):
    conn, account_id = _setup(tmp_path)
    worker_a = db.get_or_create_worker(conn, account_id, "Bob")
    worker_b = db.get_or_create_worker(conn, account_id, "bob")
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    session_b = db.create_session(conn, shift_id, worker_b, "ua")
    db.insert_scan(
        conn,
        {
            "uuid": "s1",
            "session_id": session_b,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": 1,
            "result": "ok",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )

    db.merge_workers(conn, worker_b, worker_a)

    stats = db.worker_stats_for_account(conn, account_id)
    combined = next(r for r in stats if r["worker_id"] == worker_a)
    assert combined["total_scans"] == 1  # the scan is now attributed to worker_a
    # The scan row itself is untouched (append-only) -- only sessions.worker_id moved.
    scan = db.get_scan(conn, "s1")
    assert scan["session_id"] == session_b


def test_merge_workers_rejects_self_merge(tmp_path):
    conn, account_id = _setup(tmp_path)
    worker_a = db.get_or_create_worker(conn, account_id, "Bob")
    try:
        db.merge_workers(conn, worker_a, worker_a)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_rename_worker(tmp_path):
    conn, account_id = _setup(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    db.rename_worker(conn, worker_id, "Robert")
    assert db.get_worker(conn, worker_id)["display_name"] == "Robert"


def test_set_worker_active_toggles(tmp_path):
    conn, account_id = _setup(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    assert db.get_worker(conn, worker_id)["active"] == 1
    db.set_worker_active(conn, worker_id, False)
    assert db.get_worker(conn, worker_id)["active"] == 0
    db.set_worker_active(conn, worker_id, True)
    assert db.get_worker(conn, worker_id)["active"] == 1


def test_worker_stats_includes_active_flag(tmp_path):
    conn, account_id = _setup(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    db.set_worker_active(conn, worker_id, False)
    stats = db.worker_stats_for_account(conn, account_id)
    row = next(r for r in stats if r["worker_id"] == worker_id)
    assert row["active"] == 0
