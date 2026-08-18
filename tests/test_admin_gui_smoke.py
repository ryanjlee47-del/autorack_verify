"""Smoke tests for admin_gui.py.

These construct the real Tkinter app (no mocks) against a seeded database
and pump the event loop long enough for the background-thread/queue/after()
plumbing to complete real DB round trips, then assert on actual widget
state. Skipped automatically if no display is available (e.g. a bare CI
container with no Xvfb) rather than failing the whole suite.
"""

import gc
import time

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import messagebox, scrolledtext, ttk  # noqa: E402

import auth  # noqa: E402
import backup  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402

try:
    _probe = tk.Tk()
    _probe.destroy()
    _HAS_DISPLAY = True
except tk.TclError:
    _HAS_DISPLAY = False

pytestmark = pytest.mark.skipif(not _HAS_DISPLAY, reason="no display available for Tkinter")


@pytest.fixture(autouse=True)
def _no_real_dialogs(monkeypatch):
    # tkinter.messagebox.showinfo/showerror/askyesno are REAL modal
    # dialogs -- under a headless/scripted run nothing ever clicks "OK",
    # so any code path that hits one unpatched hangs the whole process
    # forever, not just that test. Every admin_gui test exercises real
    # button handlers (not mocks), and it's easy to add a new one that
    # hits a messagebox call without realizing it (that's exactly what
    # happened here: AccountsTab._save()'s success path calls
    # messagebox.showinfo() unconditionally). Patch these globally so
    # that's a silent no-op by default; a test can still override
    # askyesno's return value with its own monkeypatch.setattr() as needed.
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)


def _pump(app, rounds=40, delay=0.03):
    for _ in range(rounds):
        app.update()
        time.sleep(delay)


def _pump_until(app, condition, max_rounds=400, delay=0.03):
    """Poll until condition() is true instead of a fixed round count --
    a fixed count is a race under system load (many Tkinter/thread-heavy
    tests running back to back in the same process), since it can't tell
    "still running" from "already timed out". Still bounded, so a truly
    broken condition fails fast rather than hanging."""
    for _ in range(max_rounds):
        app.update()
        if condition():
            return
        time.sleep(delay)
    raise AssertionError(f"condition not met after {max_rounds} rounds")


def _find_button(widget, text):
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button) and child.cget("text") == text:
            return child
        found = _find_button(child, text)
        if found is not None:
            return found
    return None


def _latest_toplevel(app):
    tops = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
    assert tops, "expected a Toplevel window to be open"
    return tops[-1]


@pytest.fixture()
def seeded_db(tmp_path):
    db_path = tmp_path / "gui_test.db"
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    conn.close()
    return db_path, info


@pytest.fixture()
def app(seeded_db):
    import admin_gui

    db_path, _info = seeded_db
    application = admin_gui.AdminApp(db_path=db_path)
    _pump(application, rounds=20)
    yield application
    # Drain in-flight background threads before tearing the app down --
    # AsyncRunner.join_all() exists precisely for this and documents why
    # (a daemon thread outliving its AdminApp can race a GC pass against
    # a C-level call from an unrelated thread).
    application.runner.join_all()
    application.destroy()
    # Then collect this app's discarded tkinter.Variable objects here, on
    # the main thread, instead of leaving them for whichever thread
    # happens to trigger the next GC. Variable.__del__ calls into Tcl,
    # and Tcl is not thread-safe: left pending, these finalizers get run
    # inside a later test's AsyncRunner worker thread, where the Tcl call
    # blocks forever. That is what hung the forensic-panel test at
    # "Loading..." in a full-suite run (confirmed by thread dump:
    # tkinter/__init__.py __del__ on top of barcode.MatchIndex.__init__),
    # and what the "main thread is not in main loop" warnings were.
    gc.collect()


def test_all_nine_tabs_constructed(app):
    expected = {
        "Overview",
        "Tables",
        "Live feed",
        "Normalizer playground",
        "Accounts",
        "Manifests & shifts",
        "Billing",
        "SQL console",
        "Server control",
    }
    assert set(app.tabs.keys()) == expected


def test_overview_tab_loads_real_stats(app):
    _pump(app, rounds=20)
    overview = app.tabs["Overview"]
    assert overview.conn_label.cget("text") == "Connected"
    assert overview.stat_labels["Accounts"].cget("text") == "1"


def test_tables_tab_browses_accounts_table(app):
    tables = app.tabs["Tables"]
    _pump(tables, rounds=10)
    assert tables.table == "accounts"
    assert len(tables.tree.get_children()) == 1


def test_accounts_tab_edit_and_save_writes_audit_log(app, seeded_db):
    db_path, info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    tab.detail_vars["free_allowance"].set("77")
    tab._save()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    account = db.get_account(conn, info["account_id"])
    assert account["free_allowance"] == 77
    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "account.update" for a in audit_rows)


def test_normalizer_playground_shows_all_tiers_and_gtin_info(app):
    tab = app.tabs["Normalizer playground"]
    _pump_until(app, lambda: bool(tab.account_combo["values"]))
    tab.account_combo.current(0)
    tab.input_entry.insert(0, "02532038")
    tab._run()
    _pump_until(app, lambda: "GTIN-14:" in tab.output.get("1.0", "end"))
    output = tab.output.get("1.0", "end")
    assert "GTIN-14:" in output
    assert "00025300000208" in output
    assert "tier 6 (SUFFIX)" in output


def test_normalizer_diff_mode_finds_divergent_tier(app):
    tab = app.tabs["Normalizer playground"]
    tab.diff_a.insert(0, "025300000208")
    tab.diff_b.insert(0, "025300000209")
    tab._diff()
    output = tab.output.get("1.0", "end")
    assert "DIFFERENT" in output


def test_sql_console_blocks_write_in_readonly_mode(app, monkeypatch):
    import tkinter.messagebox as messagebox

    calls = []
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: calls.append(a))
    tab = app.tabs["SQL console"]
    tab.sql_text.insert("1.0", "DELETE FROM accounts")
    tab._run()
    _pump(app, rounds=10)
    assert calls  # showerror was called -- the write was refused
    assert tab.status_label.cget("text") == "Read-only mode."


def test_sql_console_unsafe_mode_writes_and_logs_verbatim(app, seeded_db):
    db_path, info = seeded_db
    tab = app.tabs["SQL console"]
    tab.unsafe_var.set(True)
    tab.sql_text.insert("1.0", "UPDATE accounts SET plan='premium' WHERE id=1")
    tab._run()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    assert db.get_account(conn, info["account_id"])["plan"] == "premium"
    audit_rows = db.list_audit_log(conn)
    sql_audit = [a for a in audit_rows if a["action"] == "sql.unsafe_execute"]
    assert sql_audit
    assert "UPDATE accounts" in sql_audit[0]["after_json"]


def test_sql_console_unsafe_mode_runs_multiple_semicolon_separated_statements(app, seeded_db):
    """Regression for the gap analysis's 'SQL console runs one statement
    at a time' item: a semicolon-separated paste used to silently execute
    (or, on this sqlite3 version, outright reject) only the first
    statement. Unsafe mode should now run all of them via executescript()."""
    db_path, info = seeded_db
    tab = app.tabs["SQL console"]
    tab.unsafe_var.set(True)
    tab.sql_text.insert(
        "1.0",
        "UPDATE accounts SET plan='premium' WHERE id=1; UPDATE accounts SET free_allowance=999 WHERE id=1;",
    )
    tab._run()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    account = db.get_account(conn, info["account_id"])
    assert account["plan"] == "premium"
    assert account["free_allowance"] == 999
    assert "Multiple statements executed" in tab.status_label.cget("text")


def test_overview_backup_now_creates_file_in_backup_data(app, seeded_db, monkeypatch, tmp_path):
    db_path, _info = seeded_db
    backup_dir = tmp_path / "backup_data"
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", backup_dir)

    overview = app.tabs["Overview"]
    overview._backup_now()
    _pump(app, rounds=25)

    assert "Last backup:" in overview.backup_status_label.cget("text")
    files = list(backup_dir.glob(f"{db_path.stem}-*.db"))
    assert len(files) == 1
    rows = [overview.backup_list.item(i, "values") for i in overview.backup_list.get_children()]
    assert rows and rows[0][0] == "Database" and rows[0][1] == files[0].name


def test_overview_backup_includes_appeal_photos_when_present(app, seeded_db, monkeypatch, tmp_path):
    db_path, _info = seeded_db
    backup_dir = tmp_path / "backup_data"
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", backup_dir)
    photos_dir = db_path.parent / "appeal_photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    (photos_dir / "sample.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")

    overview = app.tabs["Overview"]
    overview._backup_now()
    _pump(app, rounds=25)

    assert "Last backup:" in overview.backup_status_label.cget("text")
    assert "+" in overview.backup_status_label.cget("text")  # "db.db + photos.zip"
    zips = list(backup_dir.glob("appeal_photos-*.zip"))
    assert len(zips) == 1
    kinds = {overview.backup_list.item(i, "values")[0] for i in overview.backup_list.get_children()}
    assert kinds == {"Database", "Photos"}


def test_forensic_panel_shows_raw_bytes_and_candidate_tiers(app, seeded_db):
    import uuid as uuid_mod

    import admin_gui

    db_path, info = seeded_db
    conn = db.connect(db_path)
    account_id = info["account_id"]
    worker_id = db.get_or_create_worker(conn, account_id, "Forensic Tester")
    shift_id = db.create_shift(
        conn,
        account_id,
        "Forensic Shift",
        "2026-07-25",
        "forensictok",
        "2099-01-01T00:00:00Z",
        "h",
        0,
    )
    manifest_id = db.list_manifests(conn, account_id)[0]["id"]
    db.link_shift_manifest(conn, shift_id, manifest_id)
    session_id = db.create_session(conn, shift_id, worker_id, "test-ua")
    scan_uuid = str(uuid_mod.uuid4())
    db.insert_scan(
        conn,
        {
            "uuid": scan_uuid,
            "session_id": session_id,
            "raw_payload": "0000099999",
            "normalized": "0000099999",
            "matched_tier": None,
            "result": "reject",
            "decode_ms": 12.5,
            "match_ms": 0.3,
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    conn.close()

    admin_gui.open_forensic_panel(app, scan_uuid)

    def find_scrolledtext(widget):
        if isinstance(widget, scrolledtext.ScrolledText):
            return widget
        for child in widget.winfo_children():
            found = find_scrolledtext(child)
            if found:
                return found
        return None

    def panel_text():
        # Search every Toplevel, not just the first: other windows (a
        # dialog, a previously opened panel) can occupy index 0, and
        # keying off position made this test hang until its timeout.
        for top in (w for w in app.winfo_children() if isinstance(w, tk.Toplevel)):
            widget = find_scrolledtext(top)
            if widget is not None:
                text = widget.get("1.0", "end")
                if text.strip():
                    return text
        return ""

    # The panel fills itself from a background DB thread, so wait for the
    # content rather than a fixed pump count -- under full-suite load 25
    # rounds isn't always enough and the test reads "Loading..." instead.
    _pump_until(app, lambda: "Loading" not in panel_text() and panel_text().strip())

    content = panel_text()
    assert "30 30 30 30 30 39 39 39 39 39" in content  # hex bytes
    assert "tier 6 (SUFFIX)" in content
    assert "ambiguity guard" in content


def test_accounts_tab_lists_users_for_selected_account(app, seeded_db):
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set("1")
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)
    rows = [tab.users_tree.item(i, "values") for i in tab.users_tree.get_children()]
    assert rows == [(seed.DEMO_EMAIL, "owner")]


def test_accounts_tab_edits_timezone(app, seeded_db):
    db_path, info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    assert tab.timezone_var.get() == "UTC"
    tab.timezone_var.set("America/Chicago")
    tab._save()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    assert db.get_account(conn, info["account_id"])["timezone"] == "America/Chicago"


def test_accounts_tab_reset_password_generates_working_new_password(app, seeded_db, monkeypatch):
    import tkinter.messagebox as messagebox

    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    db_path, info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    user_id = tab.users_tree.get_children()[0]
    tab.users_tree.selection_set(user_id)
    tab._reset_password()
    _pump(app, rounds=20)

    tops = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
    assert tops
    entry = next(
        w for w in tops[0].winfo_children() if isinstance(w, __import__("tkinter").ttk.Entry)
    )
    new_password = entry.get()
    assert new_password

    conn = db.connect(db_path)
    user = db.get_user(conn, int(user_id))
    assert auth.verify_password(new_password, user["pw_hash"])
    assert not auth.verify_password(seed.DEMO_PASSWORD, user["pw_hash"])
    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "user.password_reset" for a in audit_rows)


def test_accounts_tab_new_account_creates_account_and_owner(app, seeded_db):
    db_path, _info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)

    tab._new_account()
    win = _latest_toplevel(app)
    win.field_vars["name"].set("New Warehouse Co")
    win.field_vars["email"].set("owner@newwarehouse.test")
    _find_button(win, "Create").invoke()
    _pump(app, rounds=20)

    conn = db.connect(db_path)
    user = db.get_user_by_email(conn, "owner@newwarehouse.test")
    assert user is not None
    assert user["role"] == "owner"
    account = db.get_account(conn, user["account_id"])
    assert account["name"] == "New Warehouse Co"
    audit_rows = db.list_audit_log(conn)
    assert any(
        a["action"] == "account.create" and a["target_id"] == str(account["id"]) for a in audit_rows
    )

    # The generated password is shown in a follow-up credential window.
    cred_win = _latest_toplevel(app)
    entry = next(w for w in cred_win.winfo_children() if isinstance(w, ttk.Entry))
    assert auth.verify_password(entry.get(), user["pw_hash"])


def test_accounts_tab_new_account_rejects_duplicate_email(app, seeded_db):
    db_path, _info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)

    tab._new_account()
    win = _latest_toplevel(app)
    win.field_vars["name"].set("Dup Co")
    win.field_vars["email"].set(seed.DEMO_EMAIL)  # already exists
    _find_button(win, "Create").invoke()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    assert len(db.list_accounts(conn)) == 1  # no second account was created
    assert not any(a["action"] == "account.create" for a in db.list_audit_log(conn))
    # No credential window was opened for a failed creation -- the only
    # Toplevel still open is the dialog itself.
    tops = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
    assert tops == [win]


def test_accounts_tab_add_user_creates_manager_for_selected_account(app, seeded_db):
    db_path, info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    tab._add_user()
    win = _latest_toplevel(app)
    win.email_var.set("manager@acme.test")
    win.role_var.set("manager")
    _find_button(win, "Add").invoke()
    _pump(app, rounds=20)

    conn = db.connect(db_path)
    user = db.get_user_by_email(conn, "manager@acme.test")
    assert user is not None
    assert user["role"] == "manager"
    assert user["account_id"] == info["account_id"]
    audit_rows = db.list_audit_log(conn)
    assert any(
        a["action"] == "user.create" and a["target_id"] == str(user["id"]) for a in audit_rows
    )
    rows = [tab.users_tree.item(i, "values") for i in tab.users_tree.get_children()]
    assert ("manager@acme.test", "manager") in rows


def test_accounts_tab_edit_user_changes_email_and_role(app, seeded_db):
    db_path, info = seeded_db
    conn = db.connect(db_path)
    manager_id = db.create_user(conn, info["account_id"], "mgr@acme.test", "hash", role="manager")
    conn.close()

    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    tab.users_tree.selection_set(str(manager_id))
    tab._edit_user()
    win = _latest_toplevel(app)
    win.email_var.set("promoted@acme.test")
    win.role_var.set("owner")
    _find_button(win, "Save").invoke()
    _pump(app, rounds=20)

    conn = db.connect(db_path)
    user = db.get_user(conn, manager_id)
    assert user["email"] == "promoted@acme.test"
    assert user["role"] == "owner"
    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "user.update_email" for a in audit_rows)
    assert any(a["action"] == "user.update_role" for a in audit_rows)


def test_accounts_tab_edit_user_refuses_to_demote_the_last_owner(app, seeded_db):
    db_path, info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    owner_row_id = tab.users_tree.get_children()[0]  # the seeded demo owner
    tab.users_tree.selection_set(owner_row_id)
    tab._edit_user()
    win = _latest_toplevel(app)
    win.role_var.set("manager")
    _find_button(win, "Save").invoke()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    assert db.get_user(conn, int(owner_row_id))["role"] == "owner"  # unchanged
    assert not any(a["action"] == "user.update_role" for a in db.list_audit_log(conn))


def test_accounts_tab_delete_user_removes_a_manager(app, seeded_db, monkeypatch):
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    db_path, info = seeded_db
    conn = db.connect(db_path)
    manager_id = db.create_user(conn, info["account_id"], "mgr@acme.test", "hash", role="manager")
    conn.close()

    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    tab.users_tree.selection_set(str(manager_id))
    tab._delete_user()
    _pump(app, rounds=20)

    conn = db.connect(db_path)
    assert db.get_user(conn, manager_id) is None
    audit_rows = db.list_audit_log(conn)
    assert any(
        a["action"] == "user.delete" and a["target_id"] == str(manager_id) for a in audit_rows
    )
    rows = [tab.users_tree.item(i, "values") for i in tab.users_tree.get_children()]
    assert ("mgr@acme.test", "manager") not in rows


def test_accounts_tab_delete_user_refuses_to_remove_the_last_owner(app, seeded_db, monkeypatch):
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    db_path, info = seeded_db
    tab = app.tabs["Accounts"]
    _pump(app, rounds=10)
    tab.tree.selection_set(str(info["account_id"]))
    tab.tree.event_generate("<<TreeviewSelect>>")
    _pump(app, rounds=15)

    owner_row_id = tab.users_tree.get_children()[0]
    tab.users_tree.selection_set(owner_row_id)
    tab._delete_user()
    _pump(app, rounds=15)

    conn = db.connect(db_path)
    assert db.get_user(conn, int(owner_row_id)) is not None  # refused, still there
    # The audit row is written BEFORE db.delete_user runs (the standing
    # audit-before-mutation rule) and db.delete_user raises before
    # touching anything -- so the attempt is durably recorded even though
    # nothing was actually deleted. That's intentional, not a bug: see
    # db.delete_user's docstring.
    assert any(
        a["action"] == "user.delete" and a["target_id"] == owner_row_id
        for a in db.list_audit_log(conn)
    )


def test_billing_tab_generates_savings_report_for_selected_account(
    app, seeded_db, monkeypatch, tmp_path
):
    import tkinter.filedialog as filedialog
    import tkinter.messagebox as messagebox

    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    dest = tmp_path / "report.html"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: str(dest))

    billing_tab = app.tabs["Billing"]
    _pump(app, rounds=15)
    billing_tab._generate_report()
    _pump(app, rounds=20)

    assert dest.exists()
    assert "Dockside Supply Co. (demo)" in dest.read_text()
    assert "Savings report" in dest.read_text()


def test_billing_tab_generates_reports_for_all_accounts(app, seeded_db, monkeypatch, tmp_path):
    import tkinter.filedialog as filedialog
    import tkinter.messagebox as messagebox

    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    dest_dir = tmp_path / "all_reports"
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: str(dest_dir))

    billing_tab = app.tabs["Billing"]
    _pump(app, rounds=15)
    billing_tab._generate_all_reports()
    _pump(app, rounds=20)

    files = list(dest_dir.glob("*.html"))
    assert len(files) == 1
    assert "Dockside Supply Co. (demo)" in files[0].read_text()


def test_remote_mode_disables_auto_backup_and_backs_off_polling():
    # Port 1 refuses connections immediately (no server there), so
    # ping_remote() fails fast and cleanly during __init__ instead of
    # hanging or timing out -- the failure itself is caught and shown via
    # the mocked messagebox.showerror, tab construction proceeds
    # regardless. A local SQLite query is free; the same poll over HTTP
    # against someone else's host every few seconds, or a full DB+photos
    # backup shipped unattended every 30 minutes, is real load against
    # that host -- see admin_gui.AdminApp.__init__ and OverviewTab/
    # LiveFeedTab's REMOTE_* constants.
    import admin_gui

    app = admin_gui.AdminApp(remote_url="http://127.0.0.1:1", token="dummy-token")
    try:
        assert app._backup_job is None
        assert app.tabs["Live feed"].REMOTE_POLL_MS > app.tabs["Live feed"].LOCAL_POLL_MS
        assert app.tabs["Overview"].REMOTE_REFRESH_MS > app.tabs["Overview"].LOCAL_REFRESH_MS
    finally:
        app.runner.join_all()
        app.destroy()
