"""Regression tests for:
- manager role restriction on /billing
- the searchable manifest-line picker endpoint
- the manifest edit-history view
- the audit-log entries added for manifest commit, login/logout, worker
  appeals, and server-flagged manual_review scans
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
import auth
import db
import manifest_ingest
import seed


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _future():
    return (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _csrf_token(client, path="/dashboard"):
    page = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    return m.group(1) if m else None


@pytest.fixture()
def rig(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    manager_id = db.create_user(
        conn, info["account_id"], "manager@acme.test", auth.hash_password("mgrpw"), role="manager"
    )
    owner = flask_app.test_client()
    owner.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    manager = flask_app.test_client()
    manager.post("/login", data={"email": "manager@acme.test", "password": "mgrpw"})
    return {
        "app": flask_app,
        "conn": conn,
        "info": info,
        "owner": owner,
        "manager": manager,
        "manager_id": manager_id,
    }


# ---------------------------------------------------------------------------
# Manager role restriction
# ---------------------------------------------------------------------------


def test_manager_cannot_view_billing(rig):
    assert rig["owner"].get("/billing").status_code == 200
    assert rig["manager"].get("/billing").status_code == 403


def test_manager_billing_nav_link_is_hidden(rig):
    owner_page = rig["owner"].get("/dashboard").get_data(as_text=True)
    manager_page = rig["manager"].get("/dashboard").get_data(as_text=True)
    assert 'href="/billing"' in owner_page
    assert 'href="/billing"' not in manager_page


def test_manager_can_still_use_the_rest_of_the_app(rig):
    """The restriction must be scoped to billing, not a blanket lockout."""
    for path in ("/dashboard", "/manifests", "/shifts", "/floor", "/exceptions", "/workers"):
        assert rig["manager"].get(path).status_code == 200, path


# ---------------------------------------------------------------------------
# Searchable manifest-line picker
# ---------------------------------------------------------------------------


def test_search_endpoint_requires_minimum_query_length(rig):
    assert rig["owner"].get("/manifests/lines/search?q=").json == {"lines": []}
    assert rig["owner"].get("/manifests/lines/search?q=a").json == {"lines": []}


def test_search_endpoint_matches_sku_description_and_barcode(rig):
    manifests = db.list_manifests(rig["conn"], rig["info"]["account_id"])
    line = db.get_manifest_lines(rig["conn"], manifests[0]["id"])[0]

    by_sku = rig["owner"].get(f"/manifests/lines/search?q={line['sku'][:5]}").json["lines"]
    assert any(ln["id"] == line["id"] for ln in by_sku)

    by_barcode = rig["owner"].get(f"/manifests/lines/search?q={line['raw_barcode']}").json["lines"]
    assert any(ln["id"] == line["id"] for ln in by_barcode)


def test_search_treats_underscore_and_percent_as_literal_characters(tmp_path):
    """LIKE wildcards in the raw search term must be escaped -- otherwise
    a SKU containing '_' or '%' is matched far too broadly (SQLite LIKE
    treats '_' as 'any one character' and '%' as 'any run of characters')."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    account_id = db.create_account(conn, "Acme", 900, 25)
    db.create_user(conn, account_id, "o@a.test", auth.hash_password("pw"))
    rows = [
        {
            "line_no": 1,
            "sku": "WIDGET_A",
            "description": "d",
            "qty_expected": 1,
            "raw_barcode": "111",
        },
        {
            "line_no": 2,
            "sku": "WIDGETXA",
            "description": "d",
            "qty_expected": 1,
            "raw_barcode": "222",
        },
    ]
    manifest_ingest.commit_manifest(conn, account_id, "REF", None, rows, False, 8)

    client = flask_app.test_client()
    client.post("/login", data={"email": "o@a.test", "password": "pw"})
    results = client.get("/manifests/lines/search?q=WIDGET_A").json["lines"]
    assert [r["sku"] for r in results] == ["WIDGET_A"]  # not WIDGETXA too


def test_search_is_scoped_to_the_caller_account(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    a1 = db.create_account(conn, "A", 900, 25)
    a2 = db.create_account(conn, "B", 900, 25)
    db.create_user(conn, a1, "a@a.test", auth.hash_password("pw"))
    rows = [
        {
            "line_no": 1,
            "sku": "SECRET-B-SKU",
            "description": "d",
            "qty_expected": 1,
            "raw_barcode": "999",
        }
    ]
    manifest_ingest.commit_manifest(conn, a2, "REF", None, rows, False, 8)

    client = flask_app.test_client()
    client.post("/login", data={"email": "a@a.test", "password": "pw"})
    assert client.get("/manifests/lines/search?q=SECRET").json["lines"] == []


def test_manual_review_exception_carries_a_suggested_line(rig):
    """The server's own match from _bill_confirmed_reject should show up
    as a one-click suggestion in the exceptions list, not force the owner
    to search for something the system already found."""
    manifests = db.list_manifests(rig["conn"], rig["info"]["account_id"])
    line = db.get_manifest_lines(rig["conn"], manifests[0]["id"])[0]

    shift_id = db.create_shift(
        rig["conn"], rig["info"]["account_id"], "S", "2026-07-26", "tok2", _future(), "h", 0
    )
    db.link_shift_manifest(rig["conn"], shift_id, manifests[0]["id"])
    worker_id = db.get_or_create_worker(rig["conn"], rig["info"]["account_id"], "W")
    session_id = db.create_session(rig["conn"], shift_id, worker_id, "ua")
    import uuid

    scan_uuid = str(uuid.uuid4())
    db.insert_scan(
        rig["conn"],
        {
            "uuid": scan_uuid,
            "session_id": session_id,
            "raw_payload": line["raw_barcode"],
            "normalized": line["raw_barcode"],
            "result": "reject",
            "ts_client": _now(),
            "bundle_version": 0,
        },
    )
    db.create_exception(rig["conn"], scan_uuid, "manual_review", manifest_line_id=line["id"])

    html = rig["owner"].get("/exceptions").get_data(as_text=True)
    assert "line-suggested" in html
    assert f'data-line-id="{line["id"]}"' in html


# ---------------------------------------------------------------------------
# Manifest edit-history view
# ---------------------------------------------------------------------------


def test_manifest_history_shows_commit_and_line_events(rig):
    token = _csrf_token(rig["owner"], "/manifests/upload")
    resp = rig["owner"].post(
        "/manifests/commit",
        data={
            "ref": "History Test",
            "mode": "paste",
            "raw_text": "0000012345\n0000067890",
            "csrf_token": token,
        },
    )
    manifest_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])

    rig["owner"].post(
        f"/manifests/{manifest_id}/lines/add",
        data={"raw_barcode": "0000099999", "sku": "NEW", "description": "d", "csrf_token": token},
    )
    lines = db.get_manifest_lines(rig["conn"], manifest_id)
    rig["owner"].post(
        f"/manifests/{manifest_id}/lines/{lines[0]['id']}/edit",
        data={
            "raw_barcode": lines[0]["raw_barcode"],
            "sku": "RENAMED",
            "description": "d2",
            "qty_expected": "2",
            "csrf_token": token,
        },
    )
    rig["owner"].post(
        f"/manifests/{manifest_id}/lines/{lines[1]['id']}/delete", data={"csrf_token": token}
    )

    html = rig["owner"].get(f"/manifests/{manifest_id}/history").get_data(as_text=True)
    assert "Manifest committed" in html
    assert "Line added" in html
    assert "Line edited" in html
    assert "Line deleted" in html
    assert "History Test" in html
    assert "RENAMED" in html


def test_manifest_history_is_scoped_to_the_owning_account(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    a1 = db.create_account(conn, "A", 900, 25)
    a2 = db.create_account(conn, "B", 900, 25)
    db.create_user(conn, a1, "a@a.test", auth.hash_password("pw"))
    rows = [{"line_no": 1, "sku": "X", "description": "d", "qty_expected": 1, "raw_barcode": "111"}]
    manifest_id, _ = manifest_ingest.commit_manifest(conn, a2, "SECRET", None, rows, False, 8)

    client = flask_app.test_client()
    client.post("/login", data={"email": "a@a.test", "password": "pw"})
    assert client.get(f"/manifests/{manifest_id}/history").status_code == 404


def test_manager_can_view_manifest_history_too(rig):
    """History is informational, not a billing/account-settings surface --
    no reason to restrict it to owners."""
    manifests = db.list_manifests(rig["conn"], rig["info"]["account_id"])
    assert rig["manager"].get(f"/manifests/{manifests[0]['id']}/history").status_code == 200


# ---------------------------------------------------------------------------
# New audit_log coverage
# ---------------------------------------------------------------------------


def test_manifest_commit_is_audited(rig):
    token = _csrf_token(rig["owner"], "/manifests/upload")
    resp = rig["owner"].post(
        "/manifests/commit",
        data={
            "ref": "Audited Commit",
            "mode": "paste",
            "raw_text": "0000012345",
            "csrf_token": token,
        },
    )
    manifest_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    row = db.query_one(
        rig["conn"],
        "SELECT * FROM audit_log WHERE action='manifest.commit' AND target_id=?",
        (str(manifest_id),),
    )
    assert row is not None
    assert row["actor"] == seed.DEMO_EMAIL


def test_login_success_and_failure_are_both_audited(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)
    client = flask_app.test_client()

    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"})
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})

    actions = [r["action"] for r in db.query(conn, "SELECT action FROM audit_log ORDER BY id")]
    assert "user.login_failed" in actions
    assert "user.login" in actions


def test_login_lockout_is_audited(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)
    client = flask_app.test_client()
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"})
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"})  # now locked out
    assert (
        db.query_one(conn, "SELECT 1 FROM audit_log WHERE action='user.login_locked_out'")
        is not None
    )


def test_logout_is_audited(rig):
    token = _csrf_token(rig["owner"])
    rig["owner"].post("/logout", data={"csrf_token": token})
    row = db.query_one(rig["conn"], "SELECT * FROM audit_log WHERE action='user.logout'")
    assert row is not None
    assert row["actor"] == seed.DEMO_EMAIL


def test_audit_row_for_a_resolved_exception_survives_a_downstream_failure(rig, monkeypatch):
    """Regression for the ordering bug this session found: record_audit
    must commit durably even if the mutation it describes later fails --
    it must not be nested inside the same all-or-nothing transaction."""
    import billing as billing_mod

    manifests = db.list_manifests(rig["conn"], rig["info"]["account_id"])
    line = db.get_manifest_lines(rig["conn"], manifests[0]["id"])[0]
    shift_id = db.create_shift(
        rig["conn"], rig["info"]["account_id"], "S", "2026-07-26", "tok3", _future(), "h", 0
    )
    db.link_shift_manifest(rig["conn"], shift_id, manifests[0]["id"])
    worker_id = db.get_or_create_worker(rig["conn"], rig["info"]["account_id"], "W")
    session_id = db.create_session(rig["conn"], shift_id, worker_id, "ua")
    import uuid

    scan_uuid = str(uuid.uuid4())
    db.insert_scan(
        rig["conn"],
        {
            "uuid": scan_uuid,
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "result": "unresolved",
            "ts_client": _now(),
            "bundle_version": 0,
        },
    )
    exc_id = db.create_exception(rig["conn"], scan_uuid, "unresolved")

    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(billing_mod, "reverse_if_billed", boom)

    token = _csrf_token(rig["owner"])
    resp = rig["owner"].post(
        f"/exceptions/{exc_id}/resolve",
        data={"manifest_line_id": str(line["id"]), "csrf_token": token},
    )
    assert (
        resp.status_code == 500
    )  # Flask's test client returns the error response rather than propagating it

    audit_row = db.query_one(
        rig["conn"],
        "SELECT 1 FROM audit_log WHERE action='exception.resolve' AND target_id=?",
        (str(exc_id),),
    )
    assert audit_row is not None  # durable despite the downstream failure
    exc_row = db.query_one(rig["conn"], "SELECT resolved_at FROM exceptions WHERE id=?", (exc_id,))
    assert exc_row["resolved_at"] is None  # and the mutation itself correctly rolled back
