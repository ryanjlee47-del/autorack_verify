"""Regression tests for the findings in the Autorack Verify code review.

One test per fixed defect, named for the finding, with the docstring
recording what the bug actually did. The point is the review's own closing
observation: an invariant that spans two files needs a mechanism, not a
comment. These are the mechanisms for the server-side half; the engine half
is in tests/test_index_parity.py and tests/test_hash_parity.py.
"""

import io
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


def _future(hours=8):
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _line(no, sku, code, qty=1):
    return {
        "line_no": no,
        "sku": sku,
        "description": sku,
        "qty_expected": qty,
        "raw_barcode": code,
    }


@pytest.fixture()
def rig(tmp_path):
    """Two accounts, each with a committed manifest and a live shift."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)

    def account(name, email, token, rows):
        account_id = db.create_account(conn, name, 900, 0)
        db.create_user(conn, account_id, email, auth.hash_password("pw"))
        manifest_id, _ = manifest_ingest.commit_manifest(
            conn, account_id, f"{name}-REF", None, rows, False, 8
        )
        shift_id = db.create_shift(conn, account_id, "S", "2026-09-07", token, _future(), "h", 0)
        db.link_shift_manifest(conn, shift_id, manifest_id)
        return {
            "account_id": account_id,
            "manifest_id": manifest_id,
            "shift_id": shift_id,
            "token": token,
            "email": email,
            "lines": db.get_manifest_lines(conn, manifest_id),
        }

    victim = account(
        "VictimCo",
        "victim@v.test",
        "tok-victim",
        [_line(1, "SECRET-SKU-A", "0000012345"), _line(2, "SECRET-SKU-B", "0000067890")],
    )
    attacker = account(
        "AttackerCo", "attacker@a.test", "tok-attacker", [_line(1, "MINE", "9990001")]
    )
    return {"app": flask_app, "conn": conn, "victim": victim, "attacker": attacker}


def _login(flask_app, email):
    client = flask_app.test_client()
    assert client.post("/login", data={"email": email, "password": "pw"}).status_code == 302
    return client


def _join(flask_app, token, name="Ada"):
    client = flask_app.test_client()
    resp = client.post("/w/join", data={"t": token, "name": name}, follow_redirects=False)
    assert resp.status_code == 302, resp.data
    return client, resp.headers["Location"].split("sid=")[1]


def _sync(client, sid, scans):
    return client.post("/w/sync", json={"sessionId": sid, "clientNow": _now(), "scans": scans})


def _scan(result="ok", line_id=None, payload="0000012345", **extra):
    body = {
        "uuid": str(uuid.uuid4()),
        "rawPayload": payload,
        "normalized": payload,
        "manifestLineId": line_id,
        "matchedTier": 0,
        "result": result,
        "decodeMs": 1,
        "matchMs": 1,
        "tsClient": _now(),
        "bundleVersion": 0,
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# B1 -- cross-tenant manifest disclosure via /shifts/prepare
# ---------------------------------------------------------------------------


def test_shift_prepare_rejects_another_accounts_manifest(rig):
    """CRITICAL. /shifts/prepare took manifest_ids straight from the form
    with no ownership check, and db.get_manifest_lines is not
    account-scoped -- so any logged-in owner could POST another account's
    manifest id and receive that account's complete line list (sku,
    description, raw barcode) through /w/bundle.
    """
    client = _login(rig["app"], rig["attacker"]["email"])
    resp = client.post(
        "/shifts/prepare",
        data={
            "label": "Stolen",
            "date": "2026-09-07",
            "manifest_ids": [str(rig["victim"]["manifest_id"])],
        },
    )
    assert resp.status_code == 404

    # And nothing was created as a side effect before the check.
    shifts = db.list_shifts(rig["conn"], rig["attacker"]["account_id"])
    assert all(s["label"] != "Stolen" for s in shifts)


def test_shift_prepare_rejects_a_non_numeric_manifest_id(rig):
    """int(x) on raw form input raised ValueError, producing a 500."""
    client = _login(rig["app"], rig["attacker"]["email"])
    resp = client.post(
        "/shifts/prepare",
        data={"label": "S", "date": "2026-09-07", "manifest_ids": ["not-a-number"]},
    )
    assert resp.status_code == 400


def test_shift_prepare_still_accepts_your_own_manifest(rig):
    """The guard must not break the normal path."""
    client = _login(rig["app"], rig["attacker"]["email"])
    resp = client.post(
        "/shifts/prepare",
        data={
            "label": "Mine",
            "date": "2026-09-07",
            "manifest_ids": [str(rig["attacker"]["manifest_id"])],
        },
    )
    assert resp.status_code == 302
    assert any(
        s["label"] == "Mine" for s in db.list_shifts(rig["conn"], rig["attacker"]["account_id"])
    )


# ---------------------------------------------------------------------------
# B5 -- stored XSS via an unvalidated shift date
# ---------------------------------------------------------------------------


def test_shift_prepare_rejects_a_malformed_date(rig):
    """shift.date flowed from this form field to innerHTML on every worker
    phone that joined the shift (app.js's setHeaderChip). The date is now
    parsed server-side, and the client builds that chip from textContent."""
    client = _login(rig["app"], rig["attacker"]["email"])
    resp = client.post(
        "/shifts/prepare",
        data={
            "label": "S",
            "date": "<img src=x onerror=alert(1)>",
            "manifest_ids": [str(rig["attacker"]["manifest_id"])],
        },
        follow_redirects=False,
    )
    # Rejected back to the form rather than stored.
    assert resp.status_code == 302
    assert "/shifts/new" in resp.headers["Location"]
    rows = db.list_shifts(rig["conn"], rig["attacker"]["account_id"])
    assert all("<img" not in (s["date"] or "") for s in rows)


# ---------------------------------------------------------------------------
# B2 -- manifestLineId accepted unvalidated from the phone
# ---------------------------------------------------------------------------


def test_sync_rejects_a_nonexistent_manifest_line_id_without_500ing(rig):
    """A nonexistent id tripped the foreign key, raised inside the per-scan
    transaction, and 500ed the whole batch -- which the outbox then retried
    forever. It must come back as a named rejection instead, so the phone
    can drop exactly that scan and keep the rest."""
    client, sid = _join(rig["app"], rig["victim"]["token"])
    good = _scan(line_id=rig["victim"]["lines"][0]["id"])
    bad = _scan(line_id=999999)
    resp = _sync(client, sid, [good, bad])
    assert resp.status_code == 200
    assert good["uuid"] in resp.json["accepted"]
    assert bad["uuid"] in resp.json["rejected"]


def test_sync_rejects_another_accounts_manifest_line_id(rig):
    """A valid id from another account was stored and later rendered by the
    Live floor view, whose label lookup was not account-scoped either."""
    client, sid = _join(rig["app"], rig["victim"]["token"])
    foreign = _scan(line_id=rig["attacker"]["lines"][0]["id"])
    resp = _sync(client, sid, [foreign])
    assert resp.status_code == 200
    assert foreign["uuid"] in resp.json["rejected"]
    assert db.get_scan(rig["conn"], foreign["uuid"]) is None


def test_one_bad_scan_does_not_discard_the_rest_of_the_batch(rig):
    """The batch used to 500 as a whole, and the outbox only drops what the
    server names -- so every scan in the batch was retried forever and the
    phone's queue wedged permanently."""
    client, sid = _join(rig["app"], rig["victim"]["token"])
    line_id = rig["victim"]["lines"][0]["id"]
    scans = [_scan(line_id=line_id), _scan(line_id=999999), _scan(line_id=line_id, result="reject")]
    resp = _sync(client, sid, scans)
    assert resp.status_code == 200
    named = set(resp.json["accepted"]) | set(resp.json["rejected"])
    assert named == {s["uuid"] for s in scans}, "every scan must get a verdict"


# ---------------------------------------------------------------------------
# B3 -- billing gated on a client-supplied field
# ---------------------------------------------------------------------------


def test_client_cannot_suppress_billing_by_claiming_bundle_version_zero(rig):
    """The party holding the phones is the party being billed. The gate read
    scan.bundleVersion, so a modified client sending 0 recorded every catch
    and was charged for none of them. It now reads the version the server
    itself issued to that session at /w/bundle time."""
    conn = rig["conn"]
    client, sid = _join(rig["app"], rig["victim"]["token"])

    # Move the shift past version 0, so "am I current?" is a real question
    # rather than 0 >= 0. Then let the phone fetch the new bundle, which is
    # what records the server's own view of what this session holds.
    account = db.get_account(conn, rig["victim"]["account_id"])
    manifest_ingest.regenerate_keys(
        conn, rig["victim"]["manifest_id"], bool(account["loose_match_enabled"]), 8
    )
    assert db.get_shift(conn, rig["victim"]["shift_id"])["bundle_version"] > 0
    assert client.get(f"/w/bundle/{sid}").status_code == 200

    scan = _scan(result="reject", payload="NOT-ON-ANY-MANIFEST-9999", line_id=None)
    # The lie: a modified client claiming to be behind, so the old gate
    # ("scan.bundleVersion >= shift.bundle_version") would skip billing and
    # every catch this phone reported would be free.
    scan["bundleVersion"] = 0
    resp = _sync(client, sid, [scan])
    assert resp.status_code == 200
    assert db.billing_event_exists_for_scan(conn, scan["uuid"], "catch"), (
        "the client's bundleVersion must not be able to suppress billing"
    )


def test_a_genuinely_stale_session_is_still_not_billed(rig):
    """The other side of B3: the staleness rule itself must survive. A
    session that really has not refetched since the manifest changed is
    recorded for audit but not billed, per the README's "Bundle stale"
    tradeoff -- the server just establishes staleness from its own record
    instead of taking the phone's word for it."""
    conn = rig["conn"]
    client, sid = _join(rig["app"], rig["victim"]["token"])
    assert client.get(f"/w/bundle/{sid}").status_code == 200  # issued version 0

    account = db.get_account(conn, rig["victim"]["account_id"])
    manifest_ingest.regenerate_keys(
        conn, rig["victim"]["manifest_id"], bool(account["loose_match_enabled"]), 8
    )
    # The phone does NOT refetch. It even claims to be current.
    scan = _scan(result="reject", payload="NOT-ON-ANY-MANIFEST-9999", line_id=None)
    scan["bundleVersion"] = 99
    assert _sync(client, sid, [scan]).status_code == 200
    assert not db.billing_event_exists_for_scan(conn, scan["uuid"], "catch")
    assert db.get_scan(conn, scan["uuid"]) is not None  # recorded for audit


# ---------------------------------------------------------------------------
# B4 -- account suspension not enforced on the worker API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["bundle", "sync", "appeal"])
def test_suspended_account_is_refused_on_every_worker_route(rig, path):
    """/w/join checked account status; /w/bundle, /w/sync and /w/appeal did
    not -- so an already-joined session on a suspended account kept
    downloading bundles and generating billing events indefinitely."""
    client, sid = _join(rig["app"], rig["victim"]["token"])
    db.set_account_status(rig["conn"], rig["victim"]["account_id"], "suspended")

    if path == "bundle":
        resp = client.get(f"/w/bundle/{sid}")
    elif path == "sync":
        resp = _sync(client, sid, [_scan(line_id=rig["victim"]["lines"][0]["id"])])
    else:
        resp = client.post(
            "/w/appeal",
            data={
                "sessionId": sid,
                "scanUuid": str(uuid.uuid4()),
                "photo": (io.BytesIO(b"fake-jpeg-bytes"), "a.jpg"),
            },
            content_type="multipart/form-data",
        )
    assert resp.status_code == 403, f"/w/{path} served a suspended account"


# ---------------------------------------------------------------------------
# E1 / E2 -- cross-worker duplicate detection and qty_expected
# ---------------------------------------------------------------------------


def test_second_worker_scanning_the_same_line_gets_a_duplicate(rig):
    """The README promised this and only sessionScannedLineIds implemented
    it -- a plain object in app.js scoped to one page load on one phone. Two
    phones scanning the same line both got OK, and w_sync stored whatever
    the client asserted without ever recomputing."""
    line_id = rig["victim"]["lines"][0]["id"]
    client_a, sid_a = _join(rig["app"], rig["victim"]["token"], "Ada")
    client_b, sid_b = _join(rig["app"], rig["victim"]["token"], "Grace")

    first = _scan(line_id=line_id)
    second = _scan(line_id=line_id)  # a different phone, still claiming "ok"
    assert _sync(client_a, sid_a, [first]).status_code == 200
    assert _sync(client_b, sid_b, [second]).status_code == 200

    assert db.get_scan(rig["conn"], first["uuid"])["result"] == "ok"
    assert db.get_scan(rig["conn"], second["uuid"])["result"] == "duplicate", (
        "the second worker to reach the server must get the duplicate"
    )


def test_qty_expected_allows_that_many_units_before_duplicate(tmp_path):
    """qty_expected was detected at ingest, stored, shipped to the phone and
    never read: a line with qty_expected 4 reported DUPLICATE on unit two.
    That is a wrong answer on a screen whose whole promise is three
    unmistakable ones."""
    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    account_id = db.create_account(conn, "Acme", 900, 0)
    manifest_id, _ = manifest_ingest.commit_manifest(
        conn, account_id, "REF", None, [_line(1, "BULK", "0000012345", qty=3)], False, 8
    )
    shift_id = db.create_shift(conn, account_id, "S", "2026-09-07", "tok", _future(), "h", 0)
    db.link_shift_manifest(conn, shift_id, manifest_id)
    line_id = db.get_manifest_lines(conn, manifest_id)[0]["id"]

    client, sid = _join(flask_app, "tok")
    scans = [_scan(line_id=line_id) for _ in range(4)]
    assert _sync(client, sid, scans).status_code == 200

    results = [db.get_scan(conn, s["uuid"])["result"] for s in scans]
    assert results == ["ok", "ok", "ok", "duplicate"], results


# ---------------------------------------------------------------------------
# E3 -- logging out did not end the session
# ---------------------------------------------------------------------------


def test_logout_ends_the_session_but_still_drains_queued_scans(rig):
    """logOut() flushed the outbox, rendered a summary and navigated away.
    There was no server call at all, so the token stayed valid on shared
    phones and in browser history.

    Ending a session must not destroy queued data, though: outbox.js tags
    every scan with the session it was taken under precisely because a
    worker can log out with scans still pending. So sync keeps working while
    the scan page and the bundle do not.
    """
    client, sid = _join(rig["app"], rig["victim"]["token"])
    assert client.post("/w/logout", json={"sessionId": sid}).status_code == 200

    assert client.get(f"/w/bundle/{sid}").status_code == 403
    assert client.get(f"/w/scan?sid={sid}").status_code == 302  # back to join

    leftover = _scan(line_id=rig["victim"]["lines"][0]["id"])
    resp = _sync(client, sid, [leftover])
    assert resp.status_code == 200
    assert leftover["uuid"] in resp.json["accepted"], "queued scans must still drain after logout"


# ---------------------------------------------------------------------------
# C1 -- learned aliases invalidated nothing
# ---------------------------------------------------------------------------


def test_add_alias_bumps_every_shift_carrying_that_sku(rig):
    """add_alias bumped no bundle version, so phones never re-downloaded the
    alias and the server's cached_match_index -- keyed on bundle_version --
    kept serving the pre-alias index. The next identical scan was still
    classified REJECT, still confirmed against the stale index, and still
    billed. The alias did nothing until some unrelated event bumped the
    version."""
    conn = rig["conn"]
    before = db.get_shift(conn, rig["victim"]["shift_id"])["bundle_version"]
    db.add_alias(conn, rig["victim"]["account_id"], "WEIRD-LABEL-1", "SECRET-SKU-A", "owner@v.test")
    after = db.get_shift(conn, rig["victim"]["shift_id"])["bundle_version"]
    assert after > before, "the alias must invalidate the bundle and the server's index cache"

    # A shift that does not carry the sku is untouched.
    other = db.get_shift(conn, rig["attacker"]["shift_id"])["bundle_version"]
    db.add_alias(conn, rig["victim"]["account_id"], "ANOTHER-LABEL", "SECRET-SKU-B", "o@v.test")
    assert db.get_shift(conn, rig["attacker"]["shift_id"])["bundle_version"] == other


def test_alias_actually_resolves_after_being_learned(rig):
    """The end of the same story: once the version moves, the rebuilt index
    contains the alias and the scan resolves instead of rejecting."""
    conn = rig["conn"]
    account = db.get_account(conn, rig["victim"]["account_id"])
    manifest_ids = [rig["victim"]["manifest_id"]]

    shift = db.get_shift(conn, rig["victim"]["shift_id"])
    assert (
        not manifest_ingest.cached_match_index(conn, account, shift, manifest_ids)
        .match("WEIRD-LABEL-1")
        .is_resolved
    )

    db.add_alias(conn, rig["victim"]["account_id"], "WEIRD-LABEL-1", "SECRET-SKU-A", "o@v.test")

    shift = db.get_shift(conn, rig["victim"]["shift_id"])  # version has moved
    result = manifest_ingest.cached_match_index(conn, account, shift, manifest_ids).match(
        "WEIRD-LABEL-1"
    )
    assert result.is_resolved
    assert result.tier == barcode.Tier.ALIAS


# ---------------------------------------------------------------------------
# C2 -- single-line manifest edits were not atomic
# ---------------------------------------------------------------------------


def test_a_failed_key_regeneration_rolls_the_line_back(rig, monkeypatch):
    """db.add_manifest_line inserts a line with no line_keys rows; the route
    then regenerated keys as a separate, uncommitted step. A failure between
    them left a manifest line that matches nothing -- so every scan of that
    physical item is a confident REJECT, and confident REJECTs are billed.
    """
    client = _login(rig["app"], rig["victim"]["email"])
    manifest_id = rig["victim"]["manifest_id"]
    before = len(db.get_manifest_lines(rig["conn"], manifest_id))

    def boom(*args, **kwargs):
        raise RuntimeError("key regeneration failed")

    monkeypatch.setattr(manifest_ingest, "regenerate_keys", boom)
    resp = client.post(
        f"/manifests/{manifest_id}/lines/add",
        data={"raw_barcode": "5550001", "sku": "NEW", "qty_expected": "1"},
    )
    assert resp.status_code == 500  # the failure surfaces rather than being swallowed

    after = db.get_manifest_lines(rig["conn"], manifest_id)
    assert len(after) == before, "the line must not survive a failed key regeneration"
    assert all(row["raw_barcode"] != "5550001" for row in after)


# ---------------------------------------------------------------------------
# C6 -- credit beyond the balance evaporated
# ---------------------------------------------------------------------------


def test_credit_larger_than_the_balance_is_not_discarded(tmp_path):
    """net_amount_owed_cents returned max(total, 0), so a goodwill credit
    larger than the current balance was silently destroyed instead of
    carrying forward."""
    import billing

    conn = db.init_db(tmp_path / "t.db")
    account_id = db.create_account(conn, "Acme", 900, 0)
    db.grant_credit(conn, account_id, 5000, "Goodwill", "operator")
    assert billing.net_amount_owed_cents(conn, account_id) == -5000


# ---------------------------------------------------------------------------
# G -- shifts.bundle_hash was computed, written, and read by nothing
# ---------------------------------------------------------------------------


def test_bundle_hash_is_stable_across_manifest_id_ordering(rig):
    """It was worse than unread: it could not have matched if anything HAD
    read it.

    shift_prepare hashed a payload whose shift.id is None (the row does not
    exist yet) while /w/bundle hashed one carrying the real id, and the two
    also see the manifest ids in different orders -- the owner's form order
    versus shift_manifests. So the recorded hash and the served one could
    never be equal. The hash now covers matching content only, and treats
    lines/keys as the unordered collections they become on the phone.
    """
    conn = rig["conn"]
    account = db.get_account(conn, rig["victim"]["account_id"])
    manifest_ids = [rig["victim"]["manifest_id"]]
    shift_row = db.get_shift(conn, rig["victim"]["shift_id"])

    at_prepare = manifest_ingest.bundle_content_hash(
        manifest_ingest.bundle_payload(
            conn,
            account,
            {"id": None, "label": "S", "date": "2026-09-07", "bundle_version": 0},
            manifest_ids,
        )
    )
    at_serve = manifest_ingest.bundle_content_hash(
        manifest_ingest.bundle_payload(conn, account, shift_row, manifest_ids)
    )
    assert at_prepare == at_serve, "shift identity must not affect the content hash"

    # And reversing the manifest order must not change it either.
    payload = manifest_ingest.bundle_payload(conn, account, shift_row, manifest_ids)
    shuffled = dict(payload)
    shuffled["lines"] = list(reversed(payload["lines"]))
    shuffled["keys"] = list(reversed(payload["keys"]))
    assert manifest_ingest.bundle_content_hash(shuffled) == at_serve


def test_bundle_hash_detects_content_moving_without_a_version_bump(rig, caplog):
    """The corruption case the column was presumably meant to catch: the
    matching content changed while bundle_version stayed put. A legitimate
    manifest edit always bumps the version, so this is specifically the
    illegitimate one."""
    conn = rig["conn"]
    client = _login(rig["app"], rig["victim"]["email"])
    resp = client.post(
        "/shifts/prepare",
        data={
            "label": "Hashed",
            "date": "2026-09-07",
            "manifest_ids": [str(rig["victim"]["manifest_id"])],
        },
    )
    assert resp.status_code == 302
    shift = next(
        s for s in db.list_shifts(conn, rig["victim"]["account_id"]) if s["label"] == "Hashed"
    )

    worker, sid = _join(rig["app"], shift["token"])
    with caplog.at_level("WARNING"):
        caplog.clear()
        assert worker.get(f"/w/bundle/{sid}").status_code == 200
        assert not caplog.records, "a normal shift must not warn"

        conn.execute("DELETE FROM line_keys WHERE rowid = (SELECT MIN(rowid) FROM line_keys)")
        conn.commit()
        assert worker.get(f"/w/bundle/{sid}").status_code == 200
        assert any("bundle content differs" in r.getMessage() for r in caplog.records)
