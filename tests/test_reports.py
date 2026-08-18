import billing
import db
import reports
import seed


def _seeded(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    info = seed.ensure_seeded(conn)
    return conn, info["account_id"]


def test_generate_savings_report_html_includes_account_and_savings(tmp_path):
    conn, account_id = _seeded(tmp_path)
    html = reports.generate_savings_report_html(conn, account_id)
    assert "Dockside Supply Co. (demo)" in html
    assert "Savings report" in html
    assert "$0.00" in html  # no catches yet -> $0 saved


def test_generate_savings_report_reflects_actual_catches_and_billing(tmp_path):
    conn, account_id = _seeded(tmp_path)
    db.update_account_fields(conn, account_id, free_allowance=0)
    worker_id = db.get_or_create_worker(conn, account_id, "Report Worker")
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    db.insert_scan(
        conn,
        {
            "uuid": "r1",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "reject",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    billing.process_scan_for_billing(conn, account_id, "r1")

    html = reports.generate_savings_report_html(conn, account_id)
    assert "$45.00" in html  # SAVINGS_PER_CATCH_CENTS default, one catch
    assert "$9.00" in html  # billed amount, one catch at full price
    assert "Report Worker" in html


def test_generate_savings_report_unknown_account_raises(tmp_path):
    conn, _account_id = _seeded(tmp_path)
    try:
        reports.generate_savings_report_html(conn, 99999)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_generate_savings_report_escapes_account_name(tmp_path):
    conn, _account_id = _seeded(tmp_path)
    evil_id = db.create_account(conn, "<script>alert(1)</script>", 900, 25)
    html = reports.generate_savings_report_html(conn, evil_id)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_write_savings_report_creates_file(tmp_path):
    conn, account_id = _seeded(tmp_path)
    dest = tmp_path / "reports" / "out.html"
    path = reports.write_savings_report(conn, account_id, dest)
    assert path == dest
    assert dest.exists()
    assert "Dockside Supply" in dest.read_text()


def test_write_savings_reports_for_all_accounts(tmp_path):
    conn, account_id = _seeded(tmp_path)
    db.create_account(conn, "Second Warehouse", 900, 25)
    dest_dir = tmp_path / "reports"

    paths = reports.write_savings_reports_for_all_accounts(conn, dest_dir)
    assert len(paths) == 2
    for p in paths:
        assert p.exists()
    names = {p.name for p in paths}
    assert any("Second_Warehouse" in n or "Second Warehouse" in n for n in names)


def test_default_report_filename_sanitizes_unsafe_characters(tmp_path):
    conn, _account_id = _seeded(tmp_path)
    account_id = db.create_account(conn, "A/B: Weird*Name?", 900, 25)
    account = db.get_account(conn, account_id)
    filename = reports.default_report_filename(account)
    assert filename.endswith(".html")
    # No path-traversal-relevant characters survive into the filename body.
    for bad_char in ("/", ":", "*", "?"):
        assert bad_char not in filename
