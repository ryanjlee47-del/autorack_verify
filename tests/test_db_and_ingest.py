import barcode
import db
import manifest_ingest
import seed


def _fresh_conn(tmp_path):
    return db.init_db(tmp_path / "test.db")


def test_migration_creates_all_tables(tmp_path):
    conn = _fresh_conn(tmp_path)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in [*db.KNOWN_TABLES, "schema_migrations"]:
        assert t in tables


def test_table_page_is_stable_across_calls_with_no_order_by(tmp_path):
    # Without an explicit, unique ORDER BY, SQLite doesn't guarantee row
    # order is the same across separate LIMIT/OFFSET calls -- harmless for
    # a single call, but a real bug once a caller (admin_gui.py's Tables/
    # CSV export) pages through several calls expecting every row exactly
    # once. table_page always appends `rowid` as a tiebreaker precisely so
    # that holds even when order_by is None.
    conn = _fresh_conn(tmp_path)
    for i in range(12):
        db.create_account(conn, f"Acct {i}", 900, 25)

    seen = []
    offset = 0
    while True:
        rows, total = db.table_page(conn, "accounts", limit=5, offset=offset)
        if not rows:
            break
        seen.extend(r["id"] for r in rows)
        offset += 5
    assert len(seen) == len(set(seen)) == total == 12


def test_scans_are_append_only(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    db.create_manifest(conn, account_id, "M1", None)
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    db.insert_scan(
        conn,
        {
            "uuid": "u1",
            "session_id": session_id,
            "raw_payload": "RAW",
            "normalized": "RAW",
            "result": "ok",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    try:
        conn.execute("UPDATE scans SET result = 'reject' WHERE uuid = 'u1'")
        raise AssertionError("expected UPDATE to be blocked")
    except Exception as e:
        assert "append-only" in str(e)
    try:
        conn.execute("DELETE FROM scans WHERE uuid = 'u1'")
        raise AssertionError("expected DELETE to be blocked")
    except Exception as e:
        assert "append-only" in str(e)


def test_insert_scan_is_idempotent_on_uuid(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    scan = {
        "uuid": "replayed-uuid",
        "session_id": session_id,
        "raw_payload": "RAW",
        "normalized": "RAW",
        "result": "ok",
        "ts_client": "2026-07-25T00:00:00Z",
        "bundle_version": 0,
    }
    assert db.insert_scan(conn, scan) is True
    assert db.insert_scan(conn, scan) is False  # replay is a no-op
    assert len(db.query(conn, "SELECT * FROM scans WHERE uuid = ?", ("replayed-uuid",))) == 1


def test_billing_event_requires_confident_reject_or_confirmed_exception(tmp_path):
    # A 'reject' scan never has a manifest_line_id or matched_tier -- it's
    # a confident, unambiguous absence from the manifest (every tier came
    # back with zero candidates). It's billable on its own.
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")

    db.insert_scan(
        conn,
        {
            "uuid": "reject-scan",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "reject",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    db.insert_billing_event(conn, account_id, "reject-scan", "catch", 900)
    assert db.billing_event_exists_for_scan(conn, "reject-scan")


def test_billing_event_blocks_unresolved_ambiguous_scan_without_confirmation(tmp_path):
    # 'unresolved' means some tier hit the ambiguity guard (2+ candidates)
    # before falling through -- we are NOT confident this is a wrong item,
    # so it must never auto-bill. Only an owner-confirmed exception can
    # make it billable.
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")

    db.insert_scan(
        conn,
        {
            "uuid": "unresolved-scan",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "unresolved",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    try:
        db.insert_billing_event(conn, account_id, "unresolved-scan", "catch", 900)
        raise AssertionError("expected trigger to block this insert")
    except Exception as e:
        assert "confident reject" in str(e)

    exc_id = db.create_exception(conn, "unresolved-scan", "unresolved")
    db.resolve_exception(conn, exc_id, "owner@acme.test", "Confirmed by owner")
    db.insert_billing_event(conn, account_id, "unresolved-scan", "catch", 900)
    assert db.billing_event_exists_for_scan(conn, "unresolved-scan")


def test_billing_event_blocks_ok_and_duplicate_scans_even_with_certain_tier(tmp_path):
    # A correct scan (ok/duplicate) is never a billable catch, no matter
    # how confident the match was -- billing is for prevented mis-ships,
    # not for scanning the right item.
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S1", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "hash", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")

    for uuid_, result in [("ok-scan", "ok"), ("dup-scan", "duplicate")]:
        db.insert_scan(
            conn,
            {
                "uuid": uuid_,
                "session_id": session_id,
                "raw_payload": "X",
                "normalized": "X",
                "matched_tier": barcode.Tier.GTIN14,
                "result": result,
                "ts_client": "2026-07-25T00:00:00Z",
                "bundle_version": 0,
            },
        )
        try:
            db.insert_billing_event(conn, account_id, uuid_, "catch", 900)
            raise AssertionError(f"expected {result} scan to be blocked from billing")
        except Exception as e:
            assert "confident reject" in str(e)


def test_manifest_ingest_collision_analysis_and_match_index(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(
        conn, "Acme", 900, 25, loose_match_enabled=True, loose_suffix_len=8
    )
    account = db.get_account(conn, account_id)

    rows = [
        {"line_no": 1, "sku": "A", "description": "", "qty_expected": 1, "raw_barcode": "12345"},
        {"line_no": 2, "sku": "B", "description": "", "qty_expected": 1, "raw_barcode": "12345"},
        {"line_no": 3, "sku": "C", "description": "", "qty_expected": 1, "raw_barcode": "99999"},
    ]
    manifest_id, report = manifest_ingest.commit_manifest(
        conn, account_id, "M1", None, rows, loose_match_enabled=True, loose_suffix_len=8
    )
    assert report.total_lines_affected() == 2

    idx = manifest_ingest.build_match_index(conn, account, [manifest_id])
    assert idx.match("12345").resolution == barcode.Resolution.UNRESOLVED
    result = idx.match("99999")
    assert result.is_resolved


def test_seed_demo_account_is_idempotent(tmp_path):
    conn = _fresh_conn(tmp_path)
    info1 = seed.ensure_seeded(conn)
    assert info1 is not None
    info2 = seed.ensure_seeded(conn)
    assert info2 is None  # already seeded, no-op
    assert len(db.list_accounts(conn)) == 1


def test_seed_password_hash_roundtrip(tmp_path):
    conn = _fresh_conn(tmp_path)
    seed.ensure_seeded(conn)
    user = db.get_user_by_email(conn, seed.DEMO_EMAIL)
    assert seed.verify_password(seed.DEMO_PASSWORD, user["pw_hash"])
    assert not seed.verify_password("wrong-password", user["pw_hash"])


def test_seed_manifests_cover_upce_upca_ean13_gs1_and_alpha_skus(tmp_path):
    conn = _fresh_conn(tmp_path)
    info = seed.ensure_seeded(conn)
    manifest_ids = [mid for mid, _report in info["manifest_ids"]]
    all_lines = []
    for mid in manifest_ids:
        all_lines.extend(db.get_manifest_lines(conn, mid))
    raw_codes = [ln["raw_barcode"] for ln in all_lines]
    assert any(len(c) == 8 and c.isdigit() for c in raw_codes), "expected a UPC-E line"
    assert any(
        c.startswith("(01)") or "\x1d" in c or (len(c) >= 16 and "01" in c) for c in raw_codes
    ), "expected a GS1-128 line"
    assert any(not c.isdigit() for c in raw_codes), "expected an alphanumeric SKU line"

    collisions = [
        report for _mid, report in info["manifest_ids"] if report.total_lines_affected() > 0
    ]
    assert collisions, "expected at least one manifest with a 5-digit collision case"


def test_update_user_role_and_email(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    user_id = db.create_user(conn, account_id, "owner@acme.test", "hash")

    db.update_user_role(conn, user_id, "manager")
    assert db.get_user(conn, user_id)["role"] == "manager"

    db.update_user_email(conn, user_id, "New@Acme.test")
    assert db.get_user(conn, user_id)["email"] == "new@acme.test"  # lowercased, like create_user


def test_count_owners_for_account(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    db.create_user(conn, account_id, "owner@acme.test", "hash", role="owner")
    db.create_user(conn, account_id, "mgr@acme.test", "hash", role="manager")
    assert db.count_owners_for_account(conn, account_id) == 1

    db.create_user(conn, account_id, "owner2@acme.test", "hash", role="owner")
    assert db.count_owners_for_account(conn, account_id) == 2


def test_delete_user_refuses_to_remove_the_last_owner(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    owner_id = db.create_user(conn, account_id, "owner@acme.test", "hash", role="owner")
    manager_id = db.create_user(conn, account_id, "mgr@acme.test", "hash", role="manager")

    db.delete_user(conn, manager_id)  # not the last owner -- fine
    assert db.get_user(conn, manager_id) is None

    try:
        db.delete_user(conn, owner_id)
        raise AssertionError("expected ValueError deleting the account's last owner")
    except ValueError:
        pass
    assert db.get_user(conn, owner_id) is not None


def test_delete_user_cleans_up_dependent_rows(tmp_path):
    conn = _fresh_conn(tmp_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    db.create_user(conn, account_id, "owner@acme.test", "hash", role="owner")
    manager_id = db.create_user(conn, account_id, "mgr@acme.test", "hash", role="manager")

    db.create_web_session(conn, manager_id, account_id, "webtok", "2099-01-01T00:00:00Z")
    token = db.create_impersonation_token(conn, account_id, manager_id, "2099-01-01T00:00:00Z")

    db.delete_user(conn, manager_id)

    assert db.get_web_session(conn, "webtok") is None
    imp_row = db.query_one(
        conn, "SELECT user_id FROM impersonation_tokens WHERE token = ?", (token,)
    )
    assert (
        imp_row["user_id"] is None
    )  # detached, not deleted -- historical record of an operator action
