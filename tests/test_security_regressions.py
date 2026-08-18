"""Regression tests for specific security defects.

Each test here corresponds to a concrete bug that existed and was fixed;
the docstrings say what the attack was so a future change that reopens
one fails with an explanation rather than a bare assertion.
"""

import io
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
import auth
import billing
import db
import seed


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _future():
    return (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_account(conn, name, email, token, free_allowance=0):
    account_id = db.create_account(conn, name, 900, free_allowance)
    db.create_user(conn, account_id, email, auth.hash_password("pw"))
    shift_id = db.create_shift(conn, account_id, "S", "2026-07-26", token, _future(), "h", 0)
    worker_id = db.get_or_create_worker(conn, account_id, "W")
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    return {
        "account_id": account_id,
        "shift_id": shift_id,
        "session_id": session_id,
        "session_token": db.get_session(conn, session_id)["token"],
        "email": email,
    }


def _reject_scan(conn, session_id, scan_uuid, result="unresolved"):
    db.insert_scan(
        conn,
        {
            "uuid": scan_uuid,
            "session_id": session_id,
            "raw_payload": "SECRET-PART-9931",
            "normalized": "SECRET-PART-9931",
            "result": result,
            "ts_client": _now(),
            "bundle_version": 0,
        },
    )


@pytest.fixture()
def two_accounts(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    victim = _make_account(conn, "VictimCo", "victim@v.test", "tok-victim")
    attacker = _make_account(conn, "AttackerCo", "attacker@a.test", "tok-attacker")
    return flask_app, conn, victim, attacker


def _login(flask_app, email):
    client = flask_app.test_client()
    resp = client.post("/login", data={"email": email, "password": "pw"})
    assert resp.status_code == 302
    return client


# ---------------------------------------------------------------------------
# Cross-tenant exception handling
# ---------------------------------------------------------------------------


def test_cannot_confirm_another_accounts_exception(two_accounts):
    """An owner could POST another account's exception id to
    /confirm-reject: it closed the victim's review queue entry and wrote a
    billing_events row pairing the attacker's account_id with the victim's
    scan_uuid."""
    flask_app, conn, victim, attacker = two_accounts
    scan_uuid = str(uuid.uuid4())
    _reject_scan(conn, victim["session_id"], scan_uuid)
    exception_id = db.create_exception(conn, scan_uuid, "unresolved")

    client = _login(flask_app, attacker["email"])
    assert client.post(f"/exceptions/{exception_id}/confirm-reject").status_code == 404

    row = db.query_one(conn, "SELECT * FROM exceptions WHERE id = ?", (exception_id,))
    assert row["resolved_at"] is None
    assert not db.billing_event_exists_for_scan(conn, scan_uuid, "catch")
    assert billing.net_amount_owed_cents(conn, attacker["account_id"]) == 0


def test_cannot_resolve_another_accounts_exception(two_accounts):
    """The same hole on /resolve additionally called reverse_if_billed on
    the victim's scan, wiping out a legitimately billed catch."""
    flask_app, conn, victim, attacker = two_accounts
    scan_uuid = str(uuid.uuid4())
    _reject_scan(conn, victim["session_id"], scan_uuid, result="reject")
    exception_id = db.create_exception(conn, scan_uuid, "unresolved")
    billing.process_scan_for_billing(conn, victim["account_id"], scan_uuid)
    owed_before = billing.net_amount_owed_cents(conn, victim["account_id"])
    assert owed_before == 900

    client = _login(flask_app, attacker["email"])
    resp = client.post(f"/exceptions/{exception_id}/resolve", data={"manifest_line_id": "1"})
    assert resp.status_code == 404

    assert (
        db.query_one(conn, "SELECT * FROM exceptions WHERE id = ?", (exception_id,))["resolved_at"]
        is None
    )
    assert billing.net_amount_owed_cents(conn, victim["account_id"]) == owed_before


def test_cannot_resolve_to_another_accounts_manifest_line(tmp_path):
    """manifest_line_id was never ownership-checked, so an exception could
    be resolved (and aliased) against another account's manifest data."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    other = _make_account(conn, "OtherCo", "other@o.test", "tok-other")

    # An exception belonging to the seeded demo account...
    shift_id = db.create_shift(
        conn, info["account_id"], "S", "2026-07-26", "tok-demo", _future(), "h", 0
    )
    worker_id = db.get_or_create_worker(conn, info["account_id"], "W")
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    scan_uuid = str(uuid.uuid4())
    _reject_scan(conn, session_id, scan_uuid)
    exception_id = db.create_exception(conn, scan_uuid, "unresolved")

    # ...resolved to a line owned by nobody in that account.
    other_manifest = db.create_manifest(conn, other["account_id"], "OTHER", None)
    other_line = db.query_one(
        conn,
        "SELECT id FROM manifest_lines WHERE id = ?",
        (
            db.insert_manifest_lines(
                conn,
                other_manifest,
                [
                    {
                        "line_no": 1,
                        "sku": "X",
                        "description": "d",
                        "qty_expected": 1,
                        "raw_barcode": "0000012345",
                    }
                ],
            )[0],
        ),
    )

    client = flask_app.test_client()
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    resp = client.post(
        f"/exceptions/{exception_id}/resolve",
        data={"manifest_line_id": str(other_line["id"]), "remember_alias": "1"},
    )
    assert resp.status_code == 404
    assert (
        db.query_one(conn, "SELECT * FROM exceptions WHERE id = ?", (exception_id,))["resolved_at"]
        is None
    )


# ---------------------------------------------------------------------------
# Worker session scoping
# ---------------------------------------------------------------------------


def test_worker_session_id_is_not_a_guessable_integer(tmp_path):
    """Sessions were addressed by sequential integer, so incrementing sid
    gave access to another worker's bundle, sync and appeals."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    shift_id = db.create_shift(
        conn, info["account_id"], "S", "2026-07-26", "tok", _future(), "h", 0
    )
    for m in db.list_manifests(conn, info["account_id"]):
        db.link_shift_manifest(conn, shift_id, m["id"])

    client = flask_app.test_client()
    location = client.post(
        "/w/join", data={"t": "tok", "name": "Ada"}, follow_redirects=False
    ).headers["Location"]
    sid = location.split("sid=")[1]
    assert sid != "1" and not sid.isdigit()

    assert client.get("/w/bundle/1").status_code == 404
    assert client.get("/w/session-summary/1").status_code == 404
    assert client.post("/w/sync", json={"sessionId": 1, "scans": []}).status_code == 404
    assert client.get(f"/w/bundle/{sid}").status_code == 200


def test_appeal_rejects_a_scan_from_another_shift(two_accounts):
    """Any valid session could file a worker_reported exception against
    any scan in the database, including another account's."""
    flask_app, conn, victim, attacker = two_accounts
    victim_scan = str(uuid.uuid4())
    _reject_scan(conn, victim["session_id"], victim_scan, result="reject")

    client = flask_app.test_client()
    resp = client.post(
        "/w/appeal",
        data={
            "sessionId": attacker["session_token"],
            "scanUuid": victim_scan,
            "photo": (io.BytesIO(b"\xff\xd8"), "p.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 403
    assert db.get_exception_for_scan(conn, victim_scan) is None


def test_appeal_rejects_path_traversal_in_scan_uuid(tmp_path):
    """scanUuid was interpolated straight into a filesystem path, so an
    absolute or ../-laden value wrote the uploaded file outside
    appeal_photos/."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    acct = _make_account(conn, "Acme", "a@a.test", "tok")

    outside = tmp_path / "pwned.jpg"
    for evil in ("../../pwned", str(tmp_path / "pwned"), "../" * 8 + "pwned"):
        resp = flask_app.test_client().post(
            "/w/appeal",
            data={
                "sessionId": acct["session_token"],
                "scanUuid": evil,
                "photo": (io.BytesIO(b"\xff\xd8"), "p.jpg"),
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400, evil
    assert not outside.exists()


def test_sync_rejects_non_uuid_scan_ids_without_stranding_them(tmp_path):
    """A non-UUID scan id is what made the traversal above reachable. It
    must be reported in `rejected` rather than silently skipped, or the
    client outbox retries it forever."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    acct = _make_account(conn, "Acme", "a@a.test", "tok")

    good = str(uuid.uuid4())
    body = {
        "sessionId": acct["session_token"],
        "clientNow": _now(),
        "scans": [
            {
                "uuid": good,
                "rawPayload": "X",
                "normalized": "X",
                "result": "ok",
                "tsClient": _now(),
                "bundleVersion": 0,
            },
            {
                "uuid": "../../etc/passwd",
                "rawPayload": "X",
                "normalized": "X",
                "result": "ok",
                "tsClient": _now(),
                "bundleVersion": 0,
            },
            {
                "uuid": str(uuid.uuid4()),
                "rawPayload": "X",
                "normalized": "X",
                "result": "not-a-valid-result",
                "tsClient": _now(),
                "bundleVersion": 0,
            },
        ],
    }
    resp = flask_app.test_client().post("/w/sync", json=body)
    assert resp.status_code == 200
    assert resp.json["accepted"] == [good]
    assert len(resp.json["rejected"]) == 2
    assert db.query_one(conn, "SELECT COUNT(*) n FROM scans")["n"] == 1


# ---------------------------------------------------------------------------
# Shift revocation
# ---------------------------------------------------------------------------


def test_revoked_shift_blocks_bundle_and_sync(tmp_path):
    """Revoke only gated /w/join, so an already-joined session kept
    downloading manifests and syncing billable scans -- despite the UI
    promising workers 'can no longer join or sync with it'."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    shift_id = db.create_shift(
        conn, info["account_id"], "S", "2026-07-26", "tok", _future(), "h", 0
    )
    for m in db.list_manifests(conn, info["account_id"]):
        db.link_shift_manifest(conn, shift_id, m["id"])

    client = flask_app.test_client()
    sid = (
        client.post("/w/join", data={"t": "tok", "name": "Ada"}, follow_redirects=False)
        .headers["Location"]
        .split("sid=")[1]
    )
    assert client.get(f"/w/bundle/{sid}").status_code == 200

    db.revoke_shift(conn, shift_id)

    assert client.get(f"/w/bundle/{sid}").status_code == 403
    blocked = str(uuid.uuid4())
    resp = client.post(
        "/w/sync",
        json={
            "sessionId": sid,
            "clientNow": _now(),
            "scans": [
                {
                    "uuid": blocked,
                    "rawPayload": "X",
                    "normalized": "X",
                    "result": "reject",
                    "tsClient": _now(),
                    "bundleVersion": 0,
                }
            ],
        },
    )
    assert resp.status_code == 403
    assert db.get_scan(conn, blocked) is None


def test_expired_shift_still_accepts_a_late_offline_sync(tmp_path):
    """The counterpart to the test above: natural expiry must NOT block
    sync, or a phone that was offline past the end of the shift loses the
    scans in its outbox -- breaking the offline-first guarantee."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    shift_id = db.create_shift(
        conn, info["account_id"], "S", "2026-07-26", "tok", _future(), "h", 0
    )
    for m in db.list_manifests(conn, info["account_id"]):
        db.link_shift_manifest(conn, shift_id, m["id"])

    client = flask_app.test_client()
    sid = (
        client.post("/w/join", data={"t": "tok", "name": "Ada"}, follow_redirects=False)
        .headers["Location"]
        .split("sid=")[1]
    )

    past = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute("UPDATE shifts SET token_expires_at = ? WHERE id = ?", (past, shift_id))

    late = str(uuid.uuid4())
    resp = client.post(
        "/w/sync",
        json={
            "sessionId": sid,
            "clientNow": _now(),
            "scans": [
                {
                    "uuid": late,
                    "rawPayload": "X",
                    "normalized": "X",
                    "result": "ok",
                    "tsClient": _now(),
                    "bundleVersion": 0,
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert late in resp.json["accepted"]
    assert db.get_scan(conn, late) is not None


# ---------------------------------------------------------------------------
# SQL console read-only mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "WITH d AS (SELECT id FROM users) DELETE FROM users WHERE id IN (SELECT id FROM d)",
        "WITH d AS (SELECT 1) UPDATE accounts SET name = 'pwned'",
        "DELETE FROM users",
        "DROP TABLE users",
        "PRAGMA writable_schema = ON",
        "ATTACH DATABASE '/tmp/evil.db' AS evil",
    ],
)
def test_read_only_mode_denies_writes(tmp_path, statement):
    """The console gated on a string prefix, and SQLite lets a CTE prefix
    DML -- so `WITH ... DELETE` read as a 'with' query, ran, and skipped
    the audit logging that only unsafe mode performs."""
    conn = db.init_db(tmp_path / "t.db")
    account_id = db.create_account(conn, "Acme", 900, 25)
    db.create_user(conn, account_id, "o@a.test", auth.hash_password("pw"))
    conn.close()

    conn = db.connect(tmp_path / "t.db")
    db.set_read_only(conn)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(statement).fetchall()
    conn.close()

    verify = db.connect(tmp_path / "t.db")
    assert verify.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert verify.execute("SELECT name FROM accounts").fetchone()[0] == "Acme"
    verify.close()


def test_read_only_mode_still_allows_introspection(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    db.create_account(conn, "Acme", 900, 25)
    conn.close()

    conn = db.connect(tmp_path / "t.db")
    db.set_read_only(conn)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert conn.execute("WITH x AS (SELECT 1 AS n) SELECT n FROM x").fetchone()[0] == 1
    assert conn.execute("PRAGMA table_info(accounts)").fetchall()
    db.set_read_only(conn, False)
    conn.execute("UPDATE accounts SET name = 'writable again'")
    conn.close()


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def _csrf_client(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    client = flask_app.test_client()
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    return flask_app, conn, client, info


@pytest.mark.parametrize("bad_token", ["", "not-the-right-token", "a" * 64])
def test_state_changing_post_requires_a_valid_csrf_token(tmp_path, bad_token):
    flask_app, conn, client, info = _csrf_client(tmp_path)
    worker_id = db.get_or_create_worker(conn, info["account_id"], "Bob")

    resp = client.post(
        f"/workers/{worker_id}/rename", data={"display_name": "Forged", "csrf_token": bad_token}
    )
    assert resp.status_code == 400
    assert db.get_worker(conn, worker_id)["display_name"] == "Bob"


def test_valid_csrf_token_is_accepted(tmp_path):
    flask_app, conn, client, info = _csrf_client(tmp_path)
    worker_id = db.get_or_create_worker(conn, info["account_id"], "Bob")

    # conftest injects the caller's real token when none is supplied.
    resp = client.post(f"/workers/{worker_id}/rename", data={"display_name": "Renamed"})
    assert resp.status_code == 302
    assert db.get_worker(conn, worker_id)["display_name"] == "Renamed"


def test_csrf_token_is_bound_to_the_session(tmp_path):
    """A token from one login must not authorise writes on another."""
    flask_app, conn, client_a, info = _csrf_client(tmp_path)
    page = client_a.get("/workers").get_data(as_text=True)
    assert 'name="csrf_token"' in page

    client_b = flask_app.test_client()
    client_b.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    import re

    token_a = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    page_b = client_b.get("/workers").get_data(as_text=True)
    token_b = re.search(r'name="csrf_token" value="([^"]+)"', page_b).group(1)
    assert token_a != token_b

    worker_id = db.get_or_create_worker(conn, info["account_id"], "Bob")
    resp = client_b.post(
        f"/workers/{worker_id}/rename", data={"display_name": "X", "csrf_token": token_a}
    )
    assert resp.status_code == 400


def test_worker_pwa_endpoints_stay_csrf_exempt(tmp_path):
    """The PWA has no session cookie to ride, and enforcing CSRF there
    would break offline sync."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    acct = _make_account(conn, "Acme", "a@a.test", "tok")
    resp = flask_app.test_client().post(
        "/w/sync", json={"sessionId": acct["session_token"], "clientNow": _now(), "scans": []}
    )
    assert resp.status_code == 200


def test_session_cookie_is_secure_and_httponly(tmp_path):
    flask_app, conn, client, info = _csrf_client(tmp_path)
    cookie = client.get_cookie(auth.SESSION_COOKIE_NAME)
    assert cookie is not None
    assert cookie.secure is True
    assert cookie.http_only is True


def test_logout_rejects_get(tmp_path):
    """/logout was a bare GET, so any <img src="/logout"> logged a user
    out."""
    flask_app, conn, client, info = _csrf_client(tmp_path)
    assert client.get("/logout").status_code == 405
    assert client.get("/dashboard").status_code == 200


def test_login_is_rate_limited(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)
    client = flask_app.test_client()

    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        assert (
            client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"}).status_code
            == 400
        )
    # Locked out now -- and the correct password does not get through either.
    assert (
        client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"}).status_code
        == 429
    )
    assert (
        client.post(
            "/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD}
        ).status_code
        == 429
    )
