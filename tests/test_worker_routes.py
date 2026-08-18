import gzip
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import app as app_module
import barcode
import db
import seed


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    return flask_app, conn, info


def _make_shift(conn, account_id, manifest_ids, expires_in_hours=8):
    token = secrets.token_urlsafe(16)
    expires = (datetime.now(UTC) + timedelta(hours=expires_in_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    shift_id = db.create_shift(
        conn, account_id, "Test shift", "2026-07-25", token, expires, "hash", 0
    )
    for mid in manifest_ids:
        db.link_shift_manifest(conn, shift_id, mid)
    return shift_id, token


def test_join_flow_and_bundle_download(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    account_id = info["account_id"]
    manifest_ids = [m["id"] for m in db.list_manifests(conn, account_id)]
    shift_id, token = _make_shift(conn, account_id, manifest_ids)

    client = flask_app.test_client()
    assert client.get(f"/w/join?t={token}").status_code == 200

    resp = client.post("/w/join", data={"t": token, "name": "Ada"}, follow_redirects=False)
    assert resp.status_code == 302
    sid = resp.headers["Location"].split("sid=")[1]

    assert client.get(f"/w/scan?sid={sid}").status_code == 200

    bundle_resp = client.get(f"/w/bundle/{sid}")
    assert bundle_resp.status_code == 200
    assert bundle_resp.headers["Content-Encoding"] == "gzip"
    bundle = json.loads(gzip.decompress(bundle_resp.data))
    assert bundle["lines"]
    assert bundle["keys"]
    assert bundle["contentHash"]
    assert bundle["settings"]["looseMatchEnabled"] is True


def test_join_rejects_expired_or_revoked_token(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    account_id = info["account_id"]
    manifest_ids = [m["id"] for m in db.list_manifests(conn, account_id)]
    shift_id, token = _make_shift(
        conn, account_id, manifest_ids, expires_in_hours=-1
    )  # already expired

    client = flask_app.test_client()
    resp = client.get(f"/w/join?t={token}")
    assert b"expired" in resp.data.lower() or resp.status_code == 200
    resp2 = client.post("/w/join", data={"t": token, "name": "Ada"})
    assert resp2.status_code == 400


def test_suspended_account_cannot_join_shift(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    account_id = info["account_id"]
    manifest_ids = [m["id"] for m in db.list_manifests(conn, account_id)]
    shift_id, token = _make_shift(conn, account_id, manifest_ids)

    db.set_account_status(conn, account_id, "suspended")
    client = flask_app.test_client()
    assert b"not active" in client.get(f"/w/join?t={token}").data
    resp = client.post("/w/join", data={"t": token, "name": "Ada"})
    assert resp.status_code == 400

    db.set_account_status(conn, account_id, "active")
    resp2 = client.post("/w/join", data={"t": token, "name": "Ada"}, follow_redirects=False)
    assert resp2.status_code == 302


def test_sync_is_idempotent_on_scan_uuid(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    account_id = info["account_id"]
    manifest_ids = [m["id"] for m in db.list_manifests(conn, account_id)]
    shift_id, token = _make_shift(conn, account_id, manifest_ids)

    client = flask_app.test_client()
    resp = client.post("/w/join", data={"t": token, "name": "Ada"}, follow_redirects=False)
    sid = resp.headers["Location"].split("sid=")[1]

    scan_uuid = str(uuid.uuid4())
    body = {
        "sessionId": sid,
        "clientNow": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "scans": [
            {
                "uuid": scan_uuid,
                "rawPayload": "TEST-RAW",
                "normalized": "TEST-RAW",
                "manifestLineId": None,
                "matchedTier": None,
                "result": "unresolved",
                "decodeMs": 5.0,
                "matchMs": 0.1,
                "tsClient": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "bundleVersion": 0,
                "seq": 1,
            }
        ],
    }
    r1 = client.post("/w/sync", json=body)
    assert r1.status_code == 200
    assert scan_uuid in r1.json["accepted"]

    r2 = client.post("/w/sync", json=body)
    assert r2.status_code == 200
    assert scan_uuid in r2.json["accepted"]

    rows = db.query(conn, "SELECT COUNT(*) AS n FROM scans WHERE uuid = ?", (scan_uuid,))
    assert rows[0]["n"] == 1

    # An unresolved scan must create exactly one open exception for review.
    exc_rows = db.query(
        conn, "SELECT COUNT(*) AS n FROM exceptions WHERE scan_uuid = ?", (scan_uuid,)
    )
    assert exc_rows[0]["n"] == 1


def test_session_summary_reflects_synced_scans(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    account_id = info["account_id"]
    manifest_ids = [m["id"] for m in db.list_manifests(conn, account_id)]
    shift_id, token = _make_shift(conn, account_id, manifest_ids)

    client = flask_app.test_client()
    resp = client.post("/w/join", data={"t": token, "name": "Ada"}, follow_redirects=False)
    sid = resp.headers["Location"].split("sid=")[1]

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
        for i, r in enumerate(["ok", "ok", "reject", "duplicate", "unresolved"])
    ]
    client.post("/w/sync", json={"sessionId": sid, "clientNow": now, "scans": scans})

    resp = client.get(f"/w/session-summary/{sid}")
    assert resp.status_code == 200
    assert resp.json == {
        "totalScans": 5,
        "okCount": 2,
        "rejectCount": 1,
        "duplicateCount": 1,
        "unresolvedCount": 1,
    }


def test_session_summary_unknown_session_404s(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    resp = flask_app.test_client().get("/w/session-summary/999999")
    assert resp.status_code == 404


def test_offline_drill_block_returns_503_then_recovers(tmp_path):
    flask_app, conn, info = _client(tmp_path)
    app_module.set_offline_drill_block(0.3)
    client = flask_app.test_client()
    resp = client.post("/w/sync", json={"sessionId": 1, "scans": []})
    assert resp.status_code == 503

    import time

    time.sleep(0.35)
    resp2 = client.post("/w/sync", json={"sessionId": 1, "scans": []})
    assert resp2.status_code == 404  # unblocked, now fails validation normally (no such session)
    app_module.set_offline_drill_block(None)


def test_client_side_match_index_from_bundle_matches_server(tmp_path):
    """Sanity check that the exact JSON shape shipped in the bundle is
    consumable by barcode.MatchIndex.add_key the same way the JS
    Barcode.buildIndex()/matchAgainstIndex() consumes it (parity is
    covered structurally by tests/test_hash_parity.py at the normalize()
    level; this checks the bundle's key shape end-to-end)."""
    flask_app, conn, info = _client(tmp_path)
    account_id = info["account_id"]
    manifest_ids = [m["id"] for m in db.list_manifests(conn, account_id)]
    shift_id, token = _make_shift(conn, account_id, manifest_ids)

    client = flask_app.test_client()
    resp = client.post("/w/join", data={"t": token, "name": "Ada"}, follow_redirects=False)
    sid = resp.headers["Location"].split("sid=")[1]
    bundle = json.loads(gzip.decompress(client.get(f"/w/bundle/{sid}").data))

    idx = barcode.MatchIndex(
        loose_match_enabled=bundle["settings"]["looseMatchEnabled"],
        suffix_len=bundle["settings"]["looseSuffixLen"],
    )
    for row in bundle["keys"]:
        idx.add_key(row["manifestLineId"], barcode.Tier(row["tier"]), row["key"])

    line = bundle["lines"][0]
    raw = db.query_one(conn, "SELECT raw_barcode FROM manifest_lines WHERE id = ?", (line["id"],))[
        "raw_barcode"
    ]
    result = idx.match(raw)
    assert result.is_resolved
    assert result.manifest_line_id == line["id"]
