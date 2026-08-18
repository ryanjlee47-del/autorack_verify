import uuid
from datetime import UTC, datetime, timedelta

import app as app_module
import db
import seed


def _setup(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    manifests = db.list_manifests(conn, info["account_id"])
    token = "tok"
    expires = (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    shift_id = db.create_shift(conn, info["account_id"], "S", "2026-07-25", token, expires, "h", 0)
    for m in manifests:
        db.link_shift_manifest(conn, shift_id, m["id"])
    return flask_app, conn, info, token


def _join_and_scan(client, token, name, results):
    join = client.post("/w/join", data={"t": token, "name": name}, follow_redirects=False)
    sid = join.headers["Location"].split("sid=")[1]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    scans = [
        {
            "uuid": str(uuid.uuid4()),
            "rawPayload": f"X{i}",
            "normalized": f"X{i}",
            "manifestLineId": None,
            "matchedTier": 1 if r in ("ok", "duplicate") else None,
            "result": r,
            "decodeMs": 1,
            "matchMs": 1,
            "tsClient": now,
            "bundleVersion": 0,
            "seq": i,
        }
        for i, r in enumerate(results)
    ]
    client.post("/w/sync", json={"sessionId": sid, "clientNow": now, "scans": scans})
    return sid


def test_workers_stats_requires_login(tmp_path):
    flask_app, conn, info, token = _setup(tmp_path)
    resp = flask_app.test_client().get("/workers", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_workers_stats_shows_reject_rate_and_billed_amount(tmp_path):
    flask_app, conn, info, token = _setup(tmp_path)
    db.update_account_fields(conn, info["account_id"], free_allowance=0)
    client = flask_app.test_client()
    _join_and_scan(client, token, "Sloppy Sam", ["ok", "reject", "reject", "reject", "duplicate"])

    import billing

    for row in db.query(conn, "SELECT uuid FROM scans WHERE result = 'reject'"):
        billing.process_scan_for_billing(conn, info["account_id"], row["uuid"])

    owner = flask_app.test_client()
    owner.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    resp = owner.get("/workers")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Sloppy Sam" in body
    assert "60%" in body  # 3 of 5 scans were rejects
    assert "$27.00" in body  # 3 rejects * $9 each, no free allowance


def test_workers_stats_reject_rate_dashes_for_no_scans(tmp_path):
    flask_app, conn, info, token = _setup(tmp_path)
    db.get_or_create_worker(conn, info["account_id"], "Never Scanned")
    owner = flask_app.test_client()
    owner.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    resp = owner.get("/workers")
    body = resp.data.decode()
    assert "Never Scanned" in body
    assert "--" in body


def test_workers_stats_billed_amount_nets_out_reversed_catches(tmp_path):
    flask_app, conn, info, token = _setup(tmp_path)
    db.update_account_fields(conn, info["account_id"], free_allowance=0)
    client = flask_app.test_client()
    _join_and_scan(client, token, "Worker A", ["reject"])

    import billing

    scan_uuid = db.query(conn, "SELECT uuid FROM scans WHERE result = 'reject'")[0]["uuid"]
    billing.process_scan_for_billing(conn, info["account_id"], scan_uuid)
    billing.reverse_if_billed(conn, scan_uuid, "was actually fine", "owner@test.com")

    owner = flask_app.test_client()
    owner.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    body = owner.get("/workers").data.decode()
    assert "Worker A" in body
    assert "$0.00" in body
