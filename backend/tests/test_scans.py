"""The scan itself: matching, quantities, order status, offline sync."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from conftest import (
    add_worker,
    link_phone,
    login,
    make_order,
    scan,
    scan_event,
    signup,
    sync,
    worker_on_phone,
)
from fastapi.testclient import TestClient


def lines_by_barcode(client, owner, order_id):
    return {li["expected_barcode"]: li for li in client.get(f"/api/orders/{order_id}", headers=owner.h).json()["lines"]}


def test_match_increments_and_completes_order(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [("012345678905", 2), ("036000291452", 1)])
    oid = order["id"]

    r = scan(client, phone, oid, "012345678905")
    assert r["status"] == "applied" and r["result"] == "match"
    assert r["order"]["status"] == "in_progress"

    scan(client, phone, oid, "012345678905")
    r = scan(client, phone, oid, "036000291452")
    assert r["result"] == "match"
    assert r["order"]["status"] == "completed"
    detail = client.get(f"/api/orders/{oid}", headers=owner.h).json()
    assert detail["status"] == "completed" and detail["completed_at"]
    assert detail["units_scanned"] == detail["units_expected"] == 3


def test_quantity_is_per_unit_and_extra_unit_is_over_pick(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("012345678905", 1), ("036000291452", 1)])["id"]
    assert scan(client, phone, oid, "012345678905")["result"] == "match"
    r = scan(client, phone, oid, "012345678905")
    assert r["result"] == "over_pick"
    assert lines_by_barcode(client, owner, oid)["012345678905"]["scanned_quantity"] == 1


def test_wrong_item_is_mismatch_and_logged(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner)
    oid = order["id"]
    intended = order["lines"][0]["id"]
    r = scan(client, phone, oid, "9780306406157", intended_line_item_id=intended, client_result="mismatch")
    assert r["result"] == "mismatch" and "line_item_id" not in r
    scans = client.get(f"/api/orders/{oid}/scans", headers=owner.h).json()
    assert scans[0]["result"] == "mismatch" and scans[0]["intended_line_item_id"] == intended
    assert scans[0]["client_result"] == "mismatch"
    summary = client.get("/api/dashboard/summary", headers=owner.h).json()
    assert summary["today"]["mismatches"] == 1 and summary["all_time"]["errors_caught"] == 1


def test_equivalent_barcode_formats_match(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("025300000208", 1), ("012345678905", 1)])["id"]
    r = scan(client, phone, oid, "02532038")  # UPC-E form of the first line
    assert r["result"] == "match" and r["match_tier"] == 2
    r = scan(client, phone, oid, "(01)00012345678905(10)LOT7")
    assert r["result"] == "match"
    assert r["order"]["status"] == "completed"


def test_ambiguous_scan_goes_to_review_not_counted(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    # Same product twice in different formats: GS1 scan of it is ambiguous.
    oid = make_order(client, owner, [("025300000208", 1), ("02532038", 1)])["id"]
    r = scan(client, phone, oid, "(01)00025300000208")
    assert r["result"] == "review"
    assert all(li["scanned_quantity"] == 0 for li in lines_by_barcode(client, owner, oid).values())


def test_low_confidence_suffix_match_goes_to_review(client):
    owner = signup(client)
    client.patch("/api/warehouse", json={"loose_match_enabled": True}, headers=owner.h)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("4006381333931", 1)])["id"]
    r = scan(client, phone, oid, "99994006381333931")
    assert r["result"] == "review" and r["match_tier"] == 6


def test_learned_alias_turns_mismatch_into_match(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("VND-88213", 2)])["id"]
    assert scan(client, phone, oid, "INTERNAL-5521")["result"] == "mismatch"
    version_before = client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()["version"]
    r = client.post(
        "/api/aliases", json={"scanned_barcode": "internal-5521", "target_barcode": "VND-88213"}, headers=owner.h
    )
    assert r.status_code == 201
    payload = client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()
    assert payload["version"] > version_before  # phones know to refresh
    assert any(row["tier"] == 5 and row["key"] == "INTERNAL-5521" for row in payload["match"]["index"])
    r = scan(client, phone, oid, "INTERNAL-5521")
    assert r["result"] == "match" and r["match_tier"] == 5


def test_void_undoes_a_match(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("012345678905", 1)])["id"]
    ev = scan_event(phone, oid, "012345678905")
    out = sync(client, phone, ev)
    assert out["orders"][oid]["status"] == "completed"
    void = {**scan_event(phone, oid, "x"), "kind": "void", "target_scan_id": ev["id"], "scanned_barcode": None}
    out = sync(client, phone, void)
    assert out["events"][0]["status"] == "applied" and out["events"][0]["result"] == "void"
    assert out["orders"][oid]["status"] == "in_progress"
    # A second undo of the same scan is refused.
    again = {**void, "id": str(uuid.uuid4())}
    assert sync(client, phone, again)["events"][0]["error"]["code"] == "void_already"


def test_void_only_applies_to_matches(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    ev = scan_event(phone, oid, "nope")
    sync(client, phone, ev)
    void = {**scan_event(phone, oid, "x"), "kind": "void", "target_scan_id": ev["id"]}
    assert sync(client, phone, void)["events"][0]["error"]["code"] == "void_not_match"


def test_flag_marks_order_flagged_until_resolved(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [("012345678905", 1)])
    oid = order["id"]
    flag = {
        **scan_event(phone, oid, ""),
        "kind": "flag",
        "reason": "out_of_stock",
        "note": "Bin empty",
        "line_item_id": order["lines"][0]["id"],
        "scanned_barcode": None,
    }
    out = sync(client, phone, flag)
    assert out["events"][0]["result"] == "flagged"
    assert out["orders"][oid]["status"] == "flagged"
    # Completing the pick does not clear a flag: a human must.
    assert scan(client, phone, oid, "012345678905")["order"]["status"] == "flagged"
    detail = client.get(f"/api/orders/{oid}", headers=owner.h).json()
    fid = detail["flags"][0]["id"]
    assert detail["flags"][0]["note"] == "Bin empty"
    r = client.post(f"/api/orders/{oid}/flags/{fid}/resolve", json={"note": "Restocked"}, headers=owner.h)
    assert r.json()["status"] == "completed"


def test_sync_is_idempotent(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("012345678905", 3)])["id"]
    ev = scan_event(phone, oid, "012345678905")
    first = sync(client, phone, ev)
    second = sync(client, phone, ev)  # response lost, phone re-sends
    assert first["events"][0]["status"] == "applied"
    assert second["events"][0]["status"] == "duplicate"
    assert second["events"][0]["result"] == "match"
    assert lines_by_barcode(client, owner, oid)["012345678905"]["scanned_quantity"] == 1


def test_offline_batch_applied_in_scan_order(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("012345678905", 1), ("036000291452", 1)])["id"]
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    later = scan_event(
        phone, oid, "012345678905", client_scanned_at=(t0 + timedelta(seconds=5)).isoformat(), offline=True
    )
    earlier = scan_event(phone, oid, "012345678905", client_scanned_at=t0.isoformat(), offline=True)
    out = sync(client, phone, later, earlier)  # arrive out of order
    by_id = {e["id"]: e for e in out["events"]}
    assert by_id[earlier["id"]]["result"] == "match"
    assert by_id[later["id"]]["result"] == "over_pick"
    assert [e["id"] for e in out["events"]] == [later["id"], earlier["id"]]  # response keeps request order


def test_offline_scans_keep_their_worker_after_logout_and_handover(client):
    owner = signup(client)
    maria = add_worker(client, owner, "Maria")
    devon = add_worker(client, owner, "Devon")
    phone = login(client, link_phone(client, owner), maria["pin"])
    oid = make_order(client, owner)["id"]
    queued = scan_event(phone, oid, "012345678905", offline=True)  # made offline by Maria
    maria_session = phone.session_id
    client.post("/api/worker/logout", headers=phone.h)
    login(client, phone, devon["pin"])  # Devon picks up the same phone
    assert phone.session_id != maria_session
    out = sync(client, phone, queued)  # Maria's queued scan finally syncs
    assert out["events"][0]["status"] == "applied"
    scans = client.get(f"/api/orders/{oid}/scans", headers=owner.h).json()
    assert scans[0]["worker"] == "Maria" and scans[0]["was_offline"]


def test_sync_rejects_sessions_from_another_device(client):
    owner = signup(client)
    phone_a = worker_on_phone(client, owner, "A")
    phone_b = worker_on_phone(client, owner, "B")
    oid = make_order(client, owner)["id"]
    forged = scan_event(phone_a, oid, "012345678905")
    out = sync(client, phone_b, forged)  # B's device, A's session id
    assert out["events"][0]["error"]["code"] == "session_unknown"


def test_future_timestamps_are_clamped(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    future = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    sync(client, phone, scan_event(phone, oid, "012345678905", client_scanned_at=future))
    at = datetime.fromisoformat(client.get(f"/api/orders/{oid}/scans", headers=owner.h).json()[0]["at"])
    assert at <= datetime.now(UTC) + timedelta(seconds=5)


def test_cancelled_order_rejects_scans(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    client.post(f"/api/orders/{oid}/cancel", headers=owner.h)
    r = scan(client, phone, oid, "012345678905")
    assert r["status"] == "error" and r["error"]["code"] == "order_cancelled"


def test_unknown_order_is_per_event_error(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    good = scan_event(phone, oid, "012345678905")
    bad = scan_event(phone, str(uuid.uuid4()), "012345678905")
    out = sync(client, phone, good, bad)
    assert out["events"][0]["status"] == "applied"
    assert out["events"][1]["error"]["code"] == "order_not_found"


def test_malformed_events_rejected(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    ev = scan_event(phone, oid, "")
    ev["scanned_barcode"] = None
    sync(client, phone, ev, expect=422)
    naive = scan_event(phone, oid, "012345678905", client_scanned_at="2026-01-01T10:00:00")
    sync(client, phone, naive, expect=422)


def test_two_phones_race_for_the_last_unit(app):
    """Row locking: exactly one of two simultaneous scans gets the last unit."""
    c = TestClient(app)
    owner = signup(c)
    p1 = worker_on_phone(c, owner, "One")
    p2 = worker_on_phone(c, owner, "Two")
    oid = make_order(c, owner, [("012345678905", 1)])["id"]
    results: list[str] = []
    barrier = threading.Barrier(2)

    def go(phone):
        client = TestClient(app)
        barrier.wait()
        results.append(scan(client, phone, oid, "012345678905")["result"])

    threads = [threading.Thread(target=go, args=(p,)) for p in (p1, p2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["match", "over_pick"]


def test_worker_order_list_prioritizes_assignments(client):
    owner = signup(client)
    maria = add_worker(client, owner, "Maria")
    devon = add_worker(client, owner, "Devon")
    phone = login(client, link_phone(client, owner), maria["pin"])
    make_order(client, owner, number="UNASSIGNED")
    mine = make_order(client, owner, number="MINE")
    theirs = make_order(client, owner, number="THEIRS")
    client.patch(f"/api/orders/{mine['id']}", json={"assigned_worker_id": maria["id"]}, headers=owner.h)
    client.patch(f"/api/orders/{theirs['id']}", json={"assigned_worker_id": devon["id"]}, headers=owner.h)
    numbers = [o["external_order_number"] for o in client.get("/api/worker/orders", headers=phone.h).json()["orders"]]
    assert numbers == ["MINE", "UNASSIGNED"]


def test_order_lookup_by_qr_and_by_wms_number(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, number="WMS-7781")
    r = client.get("/api/worker/orders/lookup", params={"code": f"AUTORACK:ORDER:{order['id']}"}, headers=phone.h)
    assert r.json()["order_id"] == order["id"]
    r = client.get("/api/worker/orders/lookup", params={"code": " wms-7781 "}, headers=phone.h)
    assert r.json()["order_id"] == order["id"]
    assert client.get("/api/worker/orders/lookup", params={"code": "nope"}, headers=phone.h).status_code == 404


def test_offline_payload_contains_match_index(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("025300000208", 1)])["id"]
    payload = client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()
    keys = {(row["tier"], row["key"]) for row in payload["match"]["index"]}
    assert (2, "00025300000208") in keys and (0, "025300000208") in keys
    assert payload["match"]["loose_match_enabled"] is False
    assert payload["lines"][0]["expected_quantity"] == 1


def test_shift_summary_counts(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [("012345678905", 1)])["id"]
    scan(client, phone, oid, "bad-item")
    scan(client, phone, oid, "012345678905")
    s = client.get("/api/worker/summary", headers=phone.h).json()
    assert s["units_picked"] == 1 and s["errors_caught"] == 1 and s["orders_completed"] == 1
