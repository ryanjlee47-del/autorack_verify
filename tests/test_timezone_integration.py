import app as app_module
import db
import seed


def test_signup_stores_selected_timezone(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    client = flask_app.test_client()
    resp = client.post(
        "/signup",
        data={
            "business_name": "Denver Co",
            "email": "d@test.com",
            "password": "longenough1",
            "timezone": "America/Denver",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    account = db.query_one(conn, "SELECT * FROM accounts WHERE name = ?", ("Denver Co",))
    assert account["timezone"] == "America/Denver"


def test_signup_rejects_bogus_timezone_by_falling_back_to_utc(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    db.init_db(db_path)
    client = flask_app.test_client()
    client.post(
        "/signup",
        data={
            "business_name": "Bogus TZ Co",
            "email": "bogus@test.com",
            "password": "longenough1",
            "timezone": "Not/AZone",
        },
    )
    conn = db.connect(db_path)
    account = db.query_one(conn, "SELECT * FROM accounts WHERE name = ?", ("Bogus TZ Co",))
    assert account["timezone"] == "UTC"


def test_dashboard_shows_exception_time_in_account_timezone(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    db.update_account_fields(conn, info["account_id"], timezone="America/Denver")

    worker_id = db.get_or_create_worker(conn, info["account_id"], "W")
    shift_id = db.create_shift(
        conn, info["account_id"], "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    db.insert_scan(
        conn,
        {
            "uuid": "tz1",
            "session_id": session_id,
            "raw_payload": "X",
            "normalized": "X",
            "matched_tier": None,
            "result": "unresolved",
            "ts_client": "2026-07-25T20:30:00.000Z",
            "bundle_version": 0,
        },
    )
    db.create_exception(conn, "tz1", "unresolved")

    client = flask_app.test_client()
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    body = client.get("/dashboard").data.decode()
    assert "MDT" in body or "MST" in body  # Denver offset label, DST-dependent
    assert "2026-07-25T20:30:00" not in body  # raw UTC string shouldn't leak through


def test_floor_data_converts_timestamps_per_account(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    db.update_account_fields(conn, info["account_id"], timezone="Asia/Tokyo")

    worker_id = db.get_or_create_worker(conn, info["account_id"], "W")
    shift_id = db.create_shift(
        conn, info["account_id"], "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    session_id = db.create_session(conn, shift_id, worker_id, "ua")
    db.insert_scan(
        conn,
        {
            "uuid": "t1",
            "session_id": session_id,
            "raw_payload": "JP-TEST",
            "normalized": "JP-TEST",
            "matched_tier": None,
            "result": "reject",
            "ts_client": "2026-07-25T00:00:00Z",
            "bundle_version": 0,
        },
    )

    client = flask_app.test_client()
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    scans = client.get("/floor/data").json["scans"]
    assert "JST" in scans[0]["ts_server"]


def test_two_accounts_see_different_local_times_for_the_same_instant(tmp_path):
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)

    client_a = flask_app.test_client()
    client_a.post(
        "/signup",
        data={
            "business_name": "A Co",
            "email": "a@test.com",
            "password": "longenough1",
            "timezone": "America/Los_Angeles",
        },
    )
    client_b = flask_app.test_client()
    client_b.post(
        "/signup",
        data={
            "business_name": "B Co",
            "email": "b@test.com",
            "password": "longenough1",
            "timezone": "Europe/Paris",
        },
    )

    account_a = db.query_one(conn, "SELECT * FROM accounts WHERE name = ?", ("A Co",))["id"]
    account_b = db.query_one(conn, "SELECT * FROM accounts WHERE name = ?", ("B Co",))["id"]
    for account_id, _client in [(account_a, client_a), (account_b, client_b)]:
        worker_id = db.get_or_create_worker(conn, account_id, "W")
        shift_id = db.create_shift(
            conn, account_id, "S", "2026-07-25", f"tok{account_id}", "2099-01-01T00:00:00Z", "h", 0
        )
        session_id = db.create_session(conn, shift_id, worker_id, "ua")
        db.insert_scan(
            conn,
            {
                "uuid": f"scan{account_id}",
                "session_id": session_id,
                "raw_payload": "X",
                "normalized": "X",
                "matched_tier": None,
                "result": "reject",
                "ts_client": "2026-07-25T00:00:00Z",
                "bundle_version": 0,
            },
        )

    ts_a = client_a.get("/floor/data").json["scans"][0]["ts_server"]
    ts_b = client_b.get("/floor/data").json["scans"][0]["ts_server"]
    assert (
        ts_a != ts_b
    )  # same instant, different account timezones -> different displayed local time
