"""Regression tests for server-side verification of client-reported REJECT
scans before billing (see app.py's _bill_confirmed_reject).

Before this fix, /w/sync billed an account the instant a scan's `result`
field said "reject" -- a value the phone computed entirely client-side and
the server took on faith. A scripted client (or a buggy real one) could
claim "reject" for a barcode that genuinely matches the manifest, and the
account was billed with no human ever in the loop. These tests prove the
server now independently recomputes the match before billing, and that an
honest client's billing behavior is completely unchanged.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
import auth
import barcode
import db
import manifest_ingest


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _future():
    return (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture()
def rig(tmp_path):
    """An account with a two-line committed manifest, a shift, and a
    joined worker session -- everything needed to sync scans and inspect
    what gets billed vs. flagged."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    account_id = db.create_account(conn, "Acme", 900, 0)  # free_allowance=0: every catch bills

    rows = [
        {
            "line_no": 1,
            "sku": "WIDGET-A",
            "description": "Widget A",
            "qty_expected": 1,
            "raw_barcode": "0000012345",
        },
        {
            "line_no": 2,
            "sku": "WIDGET-B",
            "description": "Widget B",
            "qty_expected": 1,
            "raw_barcode": "0000067890",
        },
    ]
    manifest_id, _report = manifest_ingest.commit_manifest(
        conn, account_id, "REF", None, rows, False, 8
    )

    shift_id = db.create_shift(conn, account_id, "S", "2026-07-26", "tok", _future(), "h", 0)
    db.link_shift_manifest(conn, shift_id, manifest_id)

    client = flask_app.test_client()
    sid = (
        client.post("/w/join", data={"t": "tok", "name": "Ada"}, follow_redirects=False)
        .headers["Location"]
        .split("sid=")[1]
    )

    return {
        "app": flask_app,
        "conn": conn,
        "client": client,
        "account_id": account_id,
        "manifest_id": manifest_id,
        "shift_id": shift_id,
        "sid": sid,
        "real_barcode": rows[0]["raw_barcode"],
        "real_line_id": db.get_manifest_lines(conn, manifest_id)[0]["id"],
    }


def _sync_reject(rig, raw_payload, scan_uuid=None, bundle_version=0):
    scan_uuid = scan_uuid or str(uuid.uuid4())
    resp = rig["client"].post(
        "/w/sync",
        json={
            "sessionId": rig["sid"],
            "clientNow": _now(),
            "scans": [
                {
                    "uuid": scan_uuid,
                    "rawPayload": raw_payload,
                    "normalized": raw_payload,
                    "manifestLineId": None,
                    "matchedTier": None,
                    "result": "reject",
                    "decodeMs": 1,
                    "matchMs": 1,
                    "tsClient": _now(),
                    "bundleVersion": bundle_version,
                }
            ],
        },
    )
    return scan_uuid, resp


def test_fabricated_reject_for_a_real_barcode_is_not_billed(rig):
    """The core exploit: a scripted client (no real camera, no real
    matching JS involved at all) POSTs 'reject' directly for a barcode
    that IS on the manifest."""
    scan_uuid, resp = _sync_reject(rig, rig["real_barcode"])
    assert resp.status_code == 200
    assert scan_uuid in resp.json["accepted"]  # still recorded -- append-only

    assert (
        db.query_one(rig["conn"], "SELECT 1 FROM billing_events WHERE scan_uuid = ?", (scan_uuid,))
        is None
    )
    exc = db.query_one(rig["conn"], "SELECT * FROM exceptions WHERE scan_uuid = ?", (scan_uuid,))
    assert exc["kind"] == "manual_review"
    # The server's own match is attached, giving the owner a concrete lead.
    assert exc["manifest_line_id"] == rig["real_line_id"]


def test_genuine_reject_still_bills_exactly_as_before(rig):
    """A barcode that truly isn't on the manifest must still auto-bill --
    this fix must not turn every reject into a review-queue item."""
    scan_uuid, resp = _sync_reject(rig, "9999999999999-NOT-ON-MANIFEST")
    assert resp.status_code == 200
    billed = db.query_one(
        rig["conn"], "SELECT cents FROM billing_events WHERE scan_uuid = ?", (scan_uuid,)
    )
    assert billed is not None
    assert billed["cents"] == 900
    assert (
        db.query_one(rig["conn"], "SELECT 1 FROM exceptions WHERE scan_uuid = ?", (scan_uuid,))
        is None
    )


def test_reject_disagreement_does_not_stop_the_scan_from_being_recorded(rig):
    """A caught mismatch must not silently drop the scan -- the append-only
    record of what the phone reported still matters for audit even when
    the server declines to bill it."""
    scan_uuid, resp = _sync_reject(rig, rig["real_barcode"])
    scan = db.get_scan(rig["conn"], scan_uuid)
    assert scan is not None
    assert scan["result"] == "reject"  # the factual client report is preserved as-is


def test_owner_can_resolve_a_manual_review_exception_through_the_normal_flow(rig):
    """The new exception kind must be a first-class citizen of the
    existing review queue, not a dead end."""
    scan_uuid, _ = _sync_reject(rig, rig["real_barcode"])

    db.create_user(rig["conn"], rig["account_id"], "owner@acme.test", auth.hash_password("pw"))
    owner = rig["app"].test_client()
    owner.post("/login", data={"email": "owner@acme.test", "password": "pw"})

    page = owner.get("/exceptions").get_data(as_text=True)
    assert "Manual review" in page
    assert (
        'action="/exceptions/' in page and "/confirm-reject" in page
    )  # the billable-catch button is offered

    exc = db.query_one(rig["conn"], "SELECT id FROM exceptions WHERE scan_uuid = ?", (scan_uuid,))
    resp = owner.post(
        f"/exceptions/{exc['id']}/resolve", data={"manifest_line_id": str(rig["real_line_id"])}
    )
    assert resp.status_code == 302
    assert db.query_one(
        rig["conn"], "SELECT resolved_at FROM exceptions WHERE id = ?", (exc["id"],)
    )["resolved_at"]
    assert (
        db.query_one(rig["conn"], "SELECT 1 FROM billing_events WHERE scan_uuid = ?", (scan_uuid,))
        is None
    )


def test_billing_gate_treats_ambiguity_the_same_as_a_confident_match():
    """Unit-level test of the predicate app.py's _bill_confirmed_reject
    gates on (_server_confirms_reject). A genuine tier-level ambiguity
    (2+ candidates at some tier) is neither a confident match nor a
    confident absence, so it must not count as the server agreeing with
    a REJECT claim -- only a result where every tier came back
    completely empty does. (Real manifest data rarely reaches this
    branch, since commit-time collision analysis proactively disables
    colliding keys before they'd ever produce a live ambiguity in the
    built index -- this test exercises the boundary directly rather than
    trying to reverse-engineer real data that hits it.)
    """
    resolved = barcode.MatchResult(
        resolution=barcode.Resolution.RESOLVED,
        tier=barcode.Tier.RAW,
        manifest_line_id=1,
        candidates_by_tier={barcode.Tier.RAW: [1]},
    )
    ambiguous = barcode.MatchResult(
        resolution=barcode.Resolution.UNRESOLVED,
        tier=None,
        manifest_line_id=None,
        candidates_by_tier={barcode.Tier.RAW: [1, 2]},
    )
    true_reject = barcode.MatchResult(
        resolution=barcode.Resolution.UNRESOLVED,
        tier=None,
        manifest_line_id=None,
        candidates_by_tier={barcode.Tier.RAW: []},
    )
    assert app_module._server_confirms_reject(resolved) is False
    assert app_module._server_confirms_reject(ambiguous) is False
    assert app_module._server_confirms_reject(true_reject) is True


def test_cache_is_invalidated_when_the_manifest_changes(rig):
    """A barcode added to the manifest mid-shift must be honored on the
    very next sync, not served a stale pre-edit index."""
    new_barcode = "5551234567890"
    scan_uuid_1, _ = _sync_reject(rig, new_barcode)
    assert (
        db.query_one(
            rig["conn"], "SELECT cents FROM billing_events WHERE scan_uuid = ?", (scan_uuid_1,)
        )["cents"]
        == 900
    )

    account = db.get_account(rig["conn"], rig["account_id"])
    db.insert_manifest_lines(
        rig["conn"],
        rig["manifest_id"],
        [
            {
                "line_no": 99,
                "sku": "NEW",
                "description": "d",
                "qty_expected": 1,
                "raw_barcode": new_barcode,
            }
        ],
    )
    manifest_ingest.regenerate_keys(
        rig["conn"],
        rig["manifest_id"],
        bool(account["loose_match_enabled"]),
        account["loose_suffix_len"],
    )
    shift_now = db.get_shift(rig["conn"], rig["shift_id"])

    scan_uuid_2, resp = _sync_reject(rig, new_barcode, bundle_version=shift_now["bundle_version"])
    assert resp.status_code == 200
    assert (
        db.query_one(
            rig["conn"], "SELECT 1 FROM billing_events WHERE scan_uuid = ?", (scan_uuid_2,)
        )
        is None
    )
    exc = db.query_one(
        rig["conn"], "SELECT kind FROM exceptions WHERE scan_uuid = ?", (scan_uuid_2,)
    )
    assert exc["kind"] == "manual_review"


def test_stale_bundle_version_reject_is_neither_billed_nor_flagged(rig):
    """Existing behavior, unchanged by this fix: a scan made against an
    old bundle version isn't billed at all (the phone hasn't caught up
    yet), so it must not spuriously trigger a manual_review exception
    either -- that would defeat the point of waiting for a refresh."""
    scan_uuid, resp = _sync_reject(rig, rig["real_barcode"], bundle_version=-1)
    assert resp.status_code == 200
    assert (
        db.query_one(rig["conn"], "SELECT 1 FROM billing_events WHERE scan_uuid = ?", (scan_uuid,))
        is None
    )
    assert (
        db.query_one(rig["conn"], "SELECT 1 FROM exceptions WHERE scan_uuid = ?", (scan_uuid,))
        is None
    )


def test_cached_match_index_does_not_leak_across_different_databases(tmp_path):
    """The cache is process-level and keyed on (db file, shift_id), not
    just shift_id -- otherwise two different SQLite files (as in any two
    tests, both starting their id sequences at 1) could serve one
    database's match index for another's shift with the same id."""
    manifest_ingest._match_index_cache.clear()

    def _one_line_shift(dirpath, barcode_value):
        conn = db.init_db(dirpath / "t.db")
        account_id = db.create_account(conn, "Acme", 900, 0)
        rows = [
            {
                "line_no": 1,
                "sku": "X",
                "description": "d",
                "qty_expected": 1,
                "raw_barcode": barcode_value,
            }
        ]
        manifest_id, _ = manifest_ingest.commit_manifest(
            conn, account_id, "REF", None, rows, False, 8
        )
        shift_id = db.create_shift(conn, account_id, "S", "2026-07-26", "tok", _future(), "h", 0)
        db.link_shift_manifest(conn, shift_id, manifest_id)
        assert shift_id == 1  # both dbs really do start their sequence at 1
        return conn, db.get_account(conn, account_id), db.get_shift(conn, shift_id), [manifest_id]

    conn_a, account_a, shift_a, mids_a = _one_line_shift(tmp_path / "a", "AAAA")
    idx_a = manifest_ingest.cached_match_index(conn_a, account_a, shift_a, mids_a)
    assert idx_a.match("AAAA").is_resolved

    conn_b, account_b, shift_b, mids_b = _one_line_shift(tmp_path / "b", "BBBB")
    idx_b = manifest_ingest.cached_match_index(conn_b, account_b, shift_b, mids_b)
    # If the cache had collided on shift_id=1 alone, this would incorrectly
    # return db A's index, and "BBBB" would come back unresolved.
    assert idx_b.match("BBBB").is_resolved
    assert not idx_b.match("AAAA").is_resolved
