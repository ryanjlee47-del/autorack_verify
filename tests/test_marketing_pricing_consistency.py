"""Pricing constants are a single source of truth (pricing.py). Marketing
copy that contradicts billing reality is a defect: this test renders the
actual landing page and fails if a displayed dollar figure or catch count
doesn't trace back to pricing.py -- not a hardcoded number in the template.
"""

import app as app_module
import billing
import db
import pricing
import seed


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    db.init_db(db_path)
    return flask_app.test_client()


def test_marketing_page_free_allowance_matches_pricing_module(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f"{pricing.DEFAULT_FREE_ALLOWANCE} free" in body
    assert f"{pricing.DEFAULT_FREE_ALLOWANCE} catches on us" in body


def test_marketing_page_price_per_catch_matches_pricing_module(tmp_path):
    client = _client(tmp_path)
    body = client.get("/").data.decode()
    expected = pricing.format_cents_as_dollars(pricing.DEFAULT_PRICE_PER_CATCH_CENTS)
    assert expected in body


def test_marketing_page_savings_example_is_computed_not_hardcoded(tmp_path):
    client = _client(tmp_path)
    body = client.get("/").data.decode()
    assert pricing.format_cents_as_dollars(pricing.SAVINGS_PER_CATCH_CENTS) in body
    assert pricing.format_cents_as_dollars(pricing.SAVINGS_PER_CATCH_CENTS * 10) in body


def test_signup_page_matches_pricing_module(tmp_path):
    client = _client(tmp_path)
    body = client.get("/signup").data.decode()
    assert f"{pricing.DEFAULT_FREE_ALLOWANCE} prevented mis-ships free" in body
    assert pricing.format_cents_as_dollars(pricing.DEFAULT_PRICE_PER_CATCH_CENTS) in body


def test_signup_creates_account_with_pricing_module_defaults(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    client = flask_app.test_client()
    resp = client.post(
        "/signup",
        data={
            "business_name": "New Warehouse",
            "email": "owner@newwarehouse.test",
            "password": "longenough1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    account = db.query_one(conn, "SELECT * FROM accounts WHERE name = ?", ("New Warehouse",))
    assert account["price_per_catch_cents"] == pricing.DEFAULT_PRICE_PER_CATCH_CENTS
    assert account["free_allowance"] == pricing.DEFAULT_FREE_ALLOWANCE


def test_signup_rejects_duplicate_email(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    db.init_db(db_path)
    client = flask_app.test_client()
    client.post(
        "/signup", data={"business_name": "A", "email": "dup@test.com", "password": "longenough1"}
    )
    resp = client.post(
        "/signup", data={"business_name": "B", "email": "dup@test.com", "password": "longenough1"}
    )
    assert resp.status_code == 400
    assert b"already exists" in resp.data


def test_billing_dashboard_shows_savings_and_net_owed(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    account_id = info["account_id"]

    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    db.insert_scan(
        conn,
        {
            "uuid": "billing-dash-1",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "reject",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )
    billing.process_scan_for_billing(conn, account_id, "billing-dash-1")

    client = flask_app.test_client()
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    resp = client.get("/billing")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert pricing.format_cents_as_dollars(pricing.SAVINGS_PER_CATCH_CENTS) in body
    assert "billing-dash-1"[:8] in body
