"""Regression tests for the hardening required before public exposure.

Covers: database-backed rate limiting (login + signup) and email format
validation. The common thread is that none of this mattered while
accounts were provisioned by hand for one warehouse -- all of it matters
the moment a stranger can reach /signup.

(An email-verification round trip also used to live here. It was removed
along with the feature -- see migrations/0008_remove_email_verification.sql
for why.)
"""

from datetime import UTC, datetime

import pytest

import app as app_module
import auth
import db
import seed


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.fixture()
def rig(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    return {"app": flask_app, "conn": conn, "db_path": db_path}


def _signup(client, email, business="Acme", password="longenough1"):
    return client.post(
        "/signup",
        data={
            "business_name": business,
            "email": email,
            "password": password,
            "timezone": "UTC",
        },
    )


# ---------------------------------------------------------------------------
# Login throttle: now database-backed
# ---------------------------------------------------------------------------


def test_login_lockout_survives_a_process_restart(tmp_path):
    """The throttle used to live in a per-process dict, so restarting the
    app -- or simply being routed to a different gunicorn worker --
    handed an attacker a fresh budget. It has to be shared state."""
    db_path = tmp_path / "t.db"
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)

    app_a = app_module.create_app(db_path=db_path)
    client_a = app_a.test_client()
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        client_a.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"})
    assert (
        client_a.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"}).status_code
        == 429
    )

    # A brand-new app object is what both a restart and a sibling worker
    # look like from the throttle's point of view.
    app_b = app_module.create_app(db_path=db_path)
    client_b = app_b.test_client()
    assert (
        client_b.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"}).status_code
        == 429
    )
    assert (
        client_b.post(
            "/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD}
        ).status_code
        == 429
    )


def test_successful_login_clears_the_failure_budget(tmp_path):
    db_path = tmp_path / "t.db"
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)
    client = app_module.create_app(db_path=db_path).test_client()

    for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
        client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"})
    assert (
        client.post(
            "/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD}
        ).status_code
        == 302
    )
    assert (
        db.count_rate_limit_events(
            conn, app_module.RATE_BUCKET_LOGIN, f"{seed.DEMO_EMAIL.lower()}|", 3600
        )
        == 0
    )


def test_login_lockout_is_scoped_per_email_not_global(tmp_path):
    """Keyed on email+IP so one attacker hammering one account can't lock
    every other user out of the whole app."""
    db_path = tmp_path / "t.db"
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    db.create_user(conn, info["account_id"], "other@acme.test", auth.hash_password("otherpw"))
    client = app_module.create_app(db_path=db_path).test_client()

    for _ in range(app_module.LOGIN_MAX_ATTEMPTS + 1):
        client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"})
    assert (
        client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "wrong"}).status_code
        == 429
    )
    # A different account from the same IP is unaffected.
    assert (
        client.post("/login", data={"email": "other@acme.test", "password": "otherpw"}).status_code
        == 302
    )


def test_rate_limit_window_expiry_is_respected(tmp_path):
    """Old events must age out of the window rather than counting forever."""
    conn = db.init_db(tmp_path / "t.db")
    for _ in range(10):
        db.record_rate_limit_event(conn, "login", "k")
    assert db.count_rate_limit_events(conn, "login", "k", 3600) == 10
    # Backdate everything past the window.
    conn.execute("UPDATE rate_limit_events SET created_at = '2020-01-01T00:00:00.000Z'")
    assert db.count_rate_limit_events(conn, "login", "k", 3600) == 0
    assert db.purge_expired_rate_limit_events(conn, 3600) == 10


def test_rate_limit_buckets_are_independent(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    for _ in range(5):
        db.record_rate_limit_event(conn, "login", "same-key")
    assert db.count_rate_limit_events(conn, "signup", "same-key", 3600) == 0


# ---------------------------------------------------------------------------
# Signup rate limiting
# ---------------------------------------------------------------------------


def test_signup_is_rate_limited_per_ip(rig):
    """Unlimited self-service account creation is the front door standing
    open once /signup is publicly reachable."""
    client = rig["app"].test_client()
    for i in range(app_module.SIGNUP_MAX_PER_IP):
        assert _signup(client, f"user{i}@example.com").status_code == 302
    assert _signup(client, "toomany@example.com").status_code == 429
    assert (
        db.query_one(rig["conn"], "SELECT COUNT(*) n FROM users")["n"]
        == app_module.SIGNUP_MAX_PER_IP
    )


def test_invalid_signup_attempts_still_consume_the_budget(rig):
    """Otherwise a script can hammer the endpoint indefinitely with junk
    input for free -- each attempt still costs a request and a DB round
    trip."""
    client = rig["app"].test_client()
    for _ in range(app_module.SIGNUP_MAX_PER_IP):
        assert _signup(client, "not-an-email", business="").status_code == 400
    assert _signup(client, "legit@example.com").status_code == 429


def test_signup_rate_limit_is_audited(rig):
    client = rig["app"].test_client()
    for i in range(app_module.SIGNUP_MAX_PER_IP):
        _signup(client, f"u{i}@example.com")
    _signup(client, "blocked@example.com")
    assert (
        db.query_one(
            rig["conn"], "SELECT 1 FROM audit_log WHERE action='account.signup_rate_limited'"
        )
        is not None
    )


def test_signup_works_while_holding_a_stale_session_cookie(rig):
    """CSRF_EXEMPT_ENDPOINTS lists Flask *endpoint* names: /signup GET is
    `signup`, but the POST is `signup_submit`. Naming only the GET
    exempted nothing, so a signup POST from anyone still carrying a
    session cookie failed with a confusing CSRF 400.

    Uses client.open() rather than client.post() deliberately:
    conftest.py's auto_csrf_token fixture patches .post() to attach the
    caller's token, which is right for owner-app forms but would mask
    this bug entirely -- templates/signup.html renders no csrf_token
    field, so a real browser posts this form without one.
    """
    client = rig["app"].test_client()
    assert _signup(client, "first@example.com").status_code == 302  # now logged in

    form = {
        "business_name": "Acme",
        "email": "second@example.com",
        "password": "longenough1",
        "timezone": "UTC",
    }
    resp = client.open("/signup", method="POST", data=form)
    assert resp.status_code == 302
    assert db.get_user_by_email(rig["conn"], "second@example.com") is not None


# ---------------------------------------------------------------------------
# Email format validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_email",
    [
        "notanemail",
        "no@tld",
        "@nothing.com",
        "spaces in@email.com",
        "trailing@dot.",
        "a@b@c.com",
        "",
    ],
)
def test_signup_rejects_malformed_emails(rig, bad_email):
    assert _signup(rig["app"].test_client(), bad_email).status_code == 400
    assert db.query_one(rig["conn"], "SELECT COUNT(*) n FROM users")["n"] == 0


@pytest.mark.parametrize(
    "good_email",
    ["a@b.co", "first.last@example.com", "user+tag@sub.example.co.uk"],
)
def test_signup_accepts_reasonable_emails(tmp_path, good_email):
    flask_app = app_module.create_app(db_path=tmp_path / f"{abs(hash(good_email))}.db")
    assert _signup(flask_app.test_client(), good_email).status_code == 302
