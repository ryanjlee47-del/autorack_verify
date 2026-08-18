import re
from datetime import UTC

import app as app_module
import db
import seed


def _logged_in_client(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    client = flask_app.test_client()
    resp = client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    assert resp.status_code == 302
    return flask_app, conn, client, info


def test_impersonation_link_is_single_use_and_logged(tmp_path):
    from datetime import datetime, timedelta

    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    expires = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    token = db.create_impersonation_token(conn, info["account_id"], None, expires)

    client = flask_app.test_client()
    resp = client.get(f"/impersonate/{token}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dashboard")
    assert client.get("/dashboard").status_code == 200

    second_client = flask_app.test_client()
    resp2 = second_client.get(f"/impersonate/{token}", follow_redirects=False)
    assert resp2.headers["Location"].endswith("/login")

    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "impersonate.redeem" for a in audit_rows)


def test_login_wrong_password_rejected(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)
    client = flask_app.test_client()
    resp = client.post("/login", data={"email": seed.DEMO_EMAIL, "password": "nope"})
    assert resp.status_code == 400
    assert b"Wrong email or password" in resp.data


def test_login_success_sets_cookie_and_dashboard_loads(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data


def test_dashboard_requires_login(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    db.init_db(db_path)
    client = flask_app.test_client()
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_manifest_upload_preview_and_commit_paste_mode(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    resp = client.post(
        "/manifests/upload", data={"ref": "TEST-PASTE", "paste": "012345678905\nALT-SKU-1\n"}
    )
    assert resp.status_code == 200
    assert b"012345678905" in resp.data
    assert b"ALT-SKU-1" in resp.data

    match = re.search(rb'name="raw_text"[^>]*>([\s\S]*?)</textarea>', resp.data)
    assert match

    resp2 = client.post(
        "/manifests/commit",
        data={
            "ref": "TEST-PASTE",
            "source_filename": "",
            "mode": "paste",
            "raw_text": "012345678905\nALT-SKU-1\n",
        },
        follow_redirects=False,
    )
    assert resp2.status_code == 302
    manifests = db.list_manifests(conn, info["account_id"])
    assert any(m["ref"] == "TEST-PASTE" and m["status"] == "committed" for m in manifests)


def test_manifest_upload_csv_with_column_detection(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    csv_text = "SKU,Barcode,Description,Qty\nA1,111111,Widget,2\nA2,222222,Gadget,1\n"
    data = {"ref": "TEST-CSV", "file": (io_bytes(csv_text), "manifest.csv")}
    resp = client.post("/manifests/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert b"111111" in resp.data
    assert b"Widget" in resp.data

    resp2 = client.post(
        "/manifests/commit",
        data={
            "ref": "TEST-CSV",
            "source_filename": "manifest.csv",
            "mode": "delimited",
            "raw_text": csv_text,
            "col_raw_barcode": "Barcode",
            "col_sku": "SKU",
            "col_description": "Description",
            "col_qty_expected": "Qty",
        },
        follow_redirects=False,
    )
    assert resp2.status_code == 302
    manifest = next(
        m for m in db.list_manifests(conn, info["account_id"]) if m["ref"] == "TEST-CSV"
    )
    lines = db.get_manifest_lines(conn, manifest["id"])
    assert len(lines) == 2
    assert lines[0]["sku"] == "A1"
    assert lines[0]["raw_barcode"] == "111111"


def io_bytes(text):
    import io as _io

    return _io.BytesIO(text.encode())


def test_shift_prepare_qr_and_revoke(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    manifests = db.list_committed_manifests(conn, info["account_id"])
    manifest_id = manifests[0]["id"]

    resp = client.post(
        "/shifts/prepare",
        data={"label": "Morning", "date": "2026-07-25", "manifest_ids": [str(manifest_id)]},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    shift_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])

    detail = client.get(f"/shifts/{shift_id}")
    assert detail.status_code == 200
    assert b"Morning" in detail.data

    qr_resp = client.get(f"/shifts/{shift_id}/qr.png")
    assert qr_resp.status_code == 200
    assert qr_resp.content_type == "image/png"
    assert qr_resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    shift = db.get_shift(conn, shift_id)
    assert shift["revoked_at"] is None

    revoke_resp = client.post(f"/shifts/{shift_id}/revoke", follow_redirects=False)
    assert revoke_resp.status_code == 302
    shift_after = db.get_shift(conn, shift_id)
    assert shift_after["revoked_at"] is not None

    # Audit log must record the revoke with a captured before-state.
    audit_rows = db.list_audit_log(conn)
    revoke_entries = [a for a in audit_rows if a["action"] == "shift.revoke"]
    assert revoke_entries
    assert revoke_entries[0]["before_json"] is not None


def test_floor_data_reflects_synced_scans(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    manifests = db.list_committed_manifests(conn, info["account_id"])
    manifest_id = manifests[0]["id"]

    resp = client.post(
        "/shifts/prepare",
        data={"label": "Floor test", "date": "2026-07-25", "manifest_ids": [str(manifest_id)]},
        follow_redirects=False,
    )
    shift_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    shift = db.get_shift(conn, shift_id)

    worker_client = flask_app.test_client()
    join_resp = worker_client.post(
        "/w/join", data={"t": shift["token"], "name": "Floor Worker"}, follow_redirects=False
    )
    sid = join_resp.headers["Location"].split("sid=")[1]

    import uuid
    from datetime import datetime

    worker_client.post(
        "/w/sync",
        json={
            "sessionId": sid,
            "clientNow": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "scans": [
                {
                    "uuid": str(uuid.uuid4()),
                    "rawPayload": "FLOOR-TEST-RAW",
                    "normalized": "FLOOR-TEST-RAW",
                    "manifestLineId": None,
                    "matchedTier": None,
                    "result": "reject",
                    "decodeMs": 1.0,
                    "matchMs": 0.1,
                    "tsClient": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "bundleVersion": 0,
                    "seq": 1,
                }
            ],
        },
    )

    data_resp = client.get("/floor/data")
    assert data_resp.status_code == 200
    payloads = [s["raw_payload"] for s in data_resp.json["scans"]]
    assert "FLOOR-TEST-RAW" in payloads


def test_exception_resolve_creates_alias_and_audit_entry(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    account_id = info["account_id"]
    manifests = db.list_committed_manifests(conn, account_id)
    manifest_id = manifests[0]["id"]
    line = db.get_manifest_lines(conn, manifest_id)[0]

    resp = client.post(
        "/shifts/prepare",
        data={"label": "Exc test", "date": "2026-07-25", "manifest_ids": [str(manifest_id)]},
        follow_redirects=False,
    )
    shift_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    shift = db.get_shift(conn, shift_id)

    worker_client = flask_app.test_client()
    join_resp = worker_client.post(
        "/w/join", data={"t": shift["token"], "name": "W"}, follow_redirects=False
    )
    sid = join_resp.headers["Location"].split("sid=")[1]

    import uuid
    from datetime import datetime

    scan_uuid = str(uuid.uuid4())
    worker_client.post(
        "/w/sync",
        json={
            "sessionId": sid,
            "clientNow": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "scans": [
                {
                    "uuid": scan_uuid,
                    "rawPayload": "UNKNOWN-VENDOR-CODE",
                    "normalized": "UNKNOWN-VENDOR-CODE",
                    "manifestLineId": None,
                    "matchedTier": None,
                    "result": "unresolved",
                    "decodeMs": 1.0,
                    "matchMs": 0.1,
                    "tsClient": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "bundleVersion": 0,
                    "seq": 1,
                }
            ],
        },
    )

    exceptions = db.list_open_exceptions(conn, account_id)
    exc = next(e for e in exceptions if e["scan_uuid"] == scan_uuid)

    resolve_resp = client.post(
        f"/exceptions/{exc['id']}/resolve",
        data={"manifest_line_id": str(line["id"]), "remember_alias": "1"},
        follow_redirects=False,
    )
    assert resolve_resp.status_code == 302

    remaining = db.list_open_exceptions(conn, account_id)
    assert all(e["scan_uuid"] != scan_uuid for e in remaining)

    if line["sku"]:
        aliases = db.list_aliases(conn, account_id)
        assert any(
            a["normalized_key"] == "UNKNOWN-VENDOR-CODE" and a["sku"] == line["sku"]
            for a in aliases
        )

    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "exception.resolve" for a in audit_rows)


def test_exception_confirm_reject_creates_billable_catch(tmp_path):
    flask_app, conn, client, info = _logged_in_client(tmp_path)
    account_id = info["account_id"]
    manifests = db.list_committed_manifests(conn, account_id)
    manifest_id = manifests[0]["id"]

    resp = client.post(
        "/shifts/prepare",
        data={"label": "Confirm test", "date": "2026-07-25", "manifest_ids": [str(manifest_id)]},
        follow_redirects=False,
    )
    shift_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    shift = db.get_shift(conn, shift_id)

    worker_client = flask_app.test_client()
    join_resp = worker_client.post(
        "/w/join", data={"t": shift["token"], "name": "W"}, follow_redirects=False
    )
    sid = join_resp.headers["Location"].split("sid=")[1]

    import uuid
    from datetime import datetime

    scan_uuid = str(uuid.uuid4())
    worker_client.post(
        "/w/sync",
        json={
            "sessionId": sid,
            "clientNow": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "scans": [
                {
                    "uuid": scan_uuid,
                    "rawPayload": "AMBIGUOUS-CODE",
                    "normalized": "AMBIGUOUS-CODE",
                    "manifestLineId": None,
                    "matchedTier": None,
                    "result": "unresolved",
                    "decodeMs": 1.0,
                    "matchMs": 0.1,
                    "tsClient": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "bundleVersion": 0,
                    "seq": 1,
                }
            ],
        },
    )

    exceptions = db.list_open_exceptions(conn, account_id)
    exc = next(e for e in exceptions if e["scan_uuid"] == scan_uuid)
    assert not db.billing_event_exists_for_scan(conn, scan_uuid)

    resp = client.post(f"/exceptions/{exc['id']}/confirm-reject", follow_redirects=False)
    assert resp.status_code == 302
    assert db.billing_event_exists_for_scan(conn, scan_uuid)
    remaining = db.list_open_exceptions(conn, account_id)
    assert all(e["scan_uuid"] != scan_uuid for e in remaining)
    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "exception.confirm_reject" for a in audit_rows)
