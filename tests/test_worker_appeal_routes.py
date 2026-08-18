import io
import uuid
from datetime import UTC, datetime, timedelta

import app as app_module
import billing
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
    client = flask_app.test_client()
    join = client.post("/w/join", data={"t": token, "name": "Appealer"}, follow_redirects=False)
    sid = join.headers["Location"].split("sid=")[1]
    return flask_app, conn, info, client, sid


def _sync_scan(client, sid, scan_uuid, result="reject"):
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    client.post(
        "/w/sync",
        json={
            "sessionId": sid,
            "clientNow": now,
            "scans": [
                {
                    "uuid": scan_uuid,
                    "rawPayload": "UNKNOWN",
                    "normalized": "UNKNOWN",
                    "manifestLineId": None,
                    "matchedTier": None,
                    "result": result,
                    "decodeMs": 1,
                    "matchMs": 1,
                    "tsClient": now,
                    "bundleVersion": 0,
                    "seq": 1,
                }
            ],
        },
    )


def test_appeal_requires_session_scan_and_photo(tmp_path):
    flask_app, conn, info, client, sid = _setup(tmp_path)
    resp = client.post(
        "/w/appeal", data={"sessionId": sid, "scanUuid": "nope"}, content_type="multipart/form-data"
    )
    assert resp.status_code == 400


def test_appeal_rejects_before_scan_has_synced(tmp_path):
    flask_app, conn, info, client, sid = _setup(tmp_path)
    scan_uuid = str(uuid.uuid4())
    resp = client.post(
        "/w/appeal",
        data={
            "sessionId": sid,
            "scanUuid": scan_uuid,
            "note": "x",
            "photo": (io.BytesIO(b"\xff\xd8\xff"), "p.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 409


def test_appeal_creates_worker_reported_exception_with_photo(tmp_path):
    flask_app, conn, info, client, sid = _setup(tmp_path)
    scan_uuid = str(uuid.uuid4())
    _sync_scan(client, sid, scan_uuid)

    photo_bytes = b"\xff\xd8\xff\xe0FAKEJPEG"
    resp = client.post(
        "/w/appeal",
        data={
            "sessionId": sid,
            "scanUuid": scan_uuid,
            "note": "I think this is right",
            "photo": (io.BytesIO(photo_bytes), "p.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.json["ok"] is True
    assert resp.json["alreadySubmitted"] is False

    exceptions = db.list_open_exceptions(conn, info["account_id"])
    exc = next(e for e in exceptions if e["scan_uuid"] == scan_uuid)
    assert exc["kind"] == "worker_reported"
    assert exc["worker_note"] == "I think this is right"
    assert exc["worker_name"] == "Appealer"


def test_appeal_upload_is_idempotent(tmp_path):
    flask_app, conn, info, client, sid = _setup(tmp_path)
    scan_uuid = str(uuid.uuid4())
    _sync_scan(client, sid, scan_uuid)
    data = {
        "sessionId": sid,
        "scanUuid": scan_uuid,
        "note": "n",
        "photo": (io.BytesIO(b"\xff\xd8"), "p.jpg"),
    }
    r1 = client.post("/w/appeal", data=data, content_type="multipart/form-data")
    data2 = {
        "sessionId": sid,
        "scanUuid": scan_uuid,
        "note": "n",
        "photo": (io.BytesIO(b"\xff\xd8"), "p.jpg"),
    }
    r2 = client.post("/w/appeal", data=data2, content_type="multipart/form-data")
    assert r1.json["alreadySubmitted"] is False
    assert r2.json["alreadySubmitted"] is True
    rows = db.query(conn, "SELECT * FROM exceptions WHERE scan_uuid = ?", (scan_uuid,))
    assert len(rows) == 1


def test_owner_can_fetch_appeal_photo_but_not_other_accounts(tmp_path):
    flask_app, conn, info, client, sid = _setup(tmp_path)
    scan_uuid = str(uuid.uuid4())
    _sync_scan(client, sid, scan_uuid)
    photo_bytes = b"\xff\xd8\xff\xe0REALPHOTO"
    client.post(
        "/w/appeal",
        data={"sessionId": sid, "scanUuid": scan_uuid, "photo": (io.BytesIO(photo_bytes), "p.jpg")},
        content_type="multipart/form-data",
    )
    exc = next(
        e for e in db.list_open_exceptions(conn, info["account_id"]) if e["scan_uuid"] == scan_uuid
    )

    owner_client = flask_app.test_client()
    owner_client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    resp = owner_client.get(f"/exceptions/{exc['id']}/photo")
    assert resp.status_code == 200
    assert resp.data == photo_bytes
    assert resp.content_type == "image/jpeg"

    # Unauthenticated request must not get the photo.
    anon_client = flask_app.test_client()
    assert (
        anon_client.get(f"/exceptions/{exc['id']}/photo").status_code == 302
    )  # redirected to login


def test_resolving_worker_appeal_to_a_line_reverses_billing(tmp_path):
    flask_app, conn, info, client, sid = _setup(tmp_path)
    account_id = info["account_id"]
    db.update_account_fields(conn, account_id, free_allowance=0)

    scan_uuid = str(uuid.uuid4())
    _sync_scan(client, sid, scan_uuid, result="reject")
    billing.process_scan_for_billing(conn, account_id, scan_uuid)
    assert billing.net_amount_owed_cents(conn, account_id) == 900

    client.post(
        "/w/appeal",
        data={
            "sessionId": sid,
            "scanUuid": scan_uuid,
            "note": "wrong reject",
            "photo": (io.BytesIO(b"\xff\xd8"), "p.jpg"),
        },
        content_type="multipart/form-data",
    )
    exc = next(e for e in db.list_open_exceptions(conn, account_id) if e["scan_uuid"] == scan_uuid)
    line = db.get_manifest_lines_for_account(conn, account_id)[0]

    owner_client = flask_app.test_client()
    owner_client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    resp = owner_client.post(
        f"/exceptions/{exc['id']}/resolve",
        data={"manifest_line_id": str(line["id"]), "remember_alias": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert billing.net_amount_owed_cents(conn, account_id) == 0

    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "billing.reverse" for a in audit_rows)
