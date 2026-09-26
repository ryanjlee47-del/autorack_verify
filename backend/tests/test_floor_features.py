"""Short picks, shipping-label scans, and problem photos."""

from __future__ import annotations

import uuid

from conftest import JPEG, add_worker, make_order, scan, scan_event, signup, sync, worker_on_phone

UPC_A = "012345678905"
UPC_B = "036000291452"
UPS = "1Z999AA10123456784"


def short_event(phone, order_id, line_id, qty, reason="out_of_stock", **extra):
    return {
        **scan_event(phone, order_id, ""),
        "kind": "short",
        "scanned_barcode": None,
        "line_item_id": line_id,
        "quantity": qty,
        "short_reason": reason,
        **extra,
    }


def ship_event(phone, order_id, tracking):
    return {**scan_event(phone, order_id, ""), "kind": "ship", "scanned_barcode": None, "tracking_number": tracking}


def detail(client, owner, oid):
    return client.get(f"/api/orders/{oid}", headers=owner.h).json()


def upload(client, phone, flag_id, data=JPEG, ctype="image/jpeg", photo_id=None):
    return client.post(
        "/api/worker/photos",
        params={"id": photo_id or str(uuid.uuid4()), "flag_id": flag_id},
        content=data,
        headers={"X-Device-Token": phone.device_token, "Content-Type": ctype},
    )


# ---------------------------------------------------------------------------
# Short picks
# ---------------------------------------------------------------------------


def test_short_pick_flags_order_and_accepting_ships_it_short(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [(UPC_A, 3), (UPC_B, 1)])
    oid, line_a = order["id"], order["lines"][0]["id"]
    scan(client, phone, oid, UPC_A)
    scan(client, phone, oid, UPC_B)
    ev = short_event(phone, oid, line_a, 2, note="Bin empty")
    out = sync(client, phone, ev)
    assert out["events"][0]["status"] == "applied"
    assert out["orders"][oid]["short"] == {line_a: 2}
    d = detail(client, owner, oid)
    assert d["status"] == "flagged"
    assert d["units_short"] == 2
    flag = d["flags"][0]
    assert flag["reason"] == "short_pick" and flag["short_quantity"] == 2 and flag["short_reason"] == "out_of_stock"

    # Replay is a no-op.
    again = sync(client, phone, ev)
    assert again["events"][0]["status"] == "duplicate"
    assert detail(client, owner, oid)["lines"][0]["short_quantity"] == 2

    r = client.post(f"/api/orders/{oid}/flags/{flag['id']}/resolve", json={"action": "accept"}, headers=owner.h)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "completed"
    assert d["flags"][0]["resolution"] == "accepted"


def test_reopening_a_short_pick_puts_units_back_on_the_list(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [(UPC_A, 2)])
    oid, line = order["id"], order["lines"][0]["id"]
    sync(client, phone, short_event(phone, oid, line, 2))
    flag_id = detail(client, owner, oid)["flags"][0]["id"]
    d = client.post(f"/api/orders/{oid}/flags/{flag_id}/resolve", json={"action": "reopen"}, headers=owner.h).json()
    assert d["lines"][0]["short_quantity"] == 0
    assert d["status"] == "in_progress"
    assert d["flags"][0]["resolution"] == "reopened"
    # Now the units scan normally again.
    assert scan(client, phone, oid, UPC_A)["result"] == "match"


def test_short_quantity_is_capped_and_blocks_over_scanning(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [(UPC_A, 3)])
    oid, line = order["id"], order["lines"][0]["id"]
    scan(client, phone, oid, UPC_A)
    sync(client, phone, short_event(phone, oid, line, 99))
    assert detail(client, owner, oid)["lines"][0]["short_quantity"] == 2  # only 2 were left
    # The line is accounted for: another unit is an over-pick, not a match.
    assert scan(client, phone, oid, UPC_A)["result"] == "over_pick"
    out = sync(client, phone, short_event(phone, oid, line, 1))
    assert out["events"][0]["error"]["code"] == "line_complete"


def test_plain_flags_cannot_be_accepted_as_short(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    flag = {**scan_event(phone, oid, ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    sync(client, phone, flag)
    r = client.post(f"/api/orders/{oid}/flags/{flag['id']}/resolve", json={"action": "accept"}, headers=owner.h)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Pack and ship
# ---------------------------------------------------------------------------


def complete(client, owner, phone):
    order = make_order(client, owner, [(UPC_A, 1)])
    scan(client, phone, order["id"], UPC_A)
    return order["id"]


def test_shipping_label_ties_tracking_to_order(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = complete(client, owner, phone)
    out = sync(client, phone, ship_event(phone, oid, "1z 999 aa1 0123456784"))
    assert out["events"][0]["result"] == "shipped"
    assert out["orders"][oid]["tracking_number"] == UPS
    d = detail(client, owner, oid)
    assert d["status"] == "shipped" and d["carrier"] == "UPS" and d["shipped_by"] == "Maria"
    # Same label again (a retried sync) is a duplicate, not an error.
    again = sync(client, phone, ship_event(phone, oid, UPS))
    assert again["events"][0]["status"] == "duplicate"
    # Shipped is final: no more scans, edits, cancel, or reopening from the list.
    assert scan(client, phone, oid, UPC_A)["error"]["code"] == "order_shipped"
    assert client.post(f"/api/orders/{oid}/cancel", headers=owner.h).status_code == 409
    assert client.post(f"/api/orders/{oid}/lines", json={"barcode": "9"}, headers=owner.h).status_code == 409
    r = client.get("/api/worker/orders/lookup", params={"code": d["external_order_number"]}, headers=phone.h)
    assert r.json()["detail"]["code"] == "order_shipped"
    summary = client.get("/api/dashboard/summary", headers=owner.h).json()
    assert summary["orders"]["shipped_today"] == 1
    assert summary["orders"]["completed_today"] == 1


def test_ship_rejections(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    unfinished = make_order(client, owner, [(UPC_A, 2)])["id"]
    scan(client, phone, unfinished, UPC_A)
    codes = lambda out: out["events"][0]["error"]["code"]  # noqa: E731
    assert codes(sync(client, phone, ship_event(phone, unfinished, UPS))) == "order_incomplete"

    oid = complete(client, owner, phone)
    assert codes(sync(client, phone, ship_event(phone, oid, UPC_A))) == "tracking_is_product"
    assert codes(sync(client, phone, ship_event(phone, oid, "123"))) == "tracking_invalid"
    sync(client, phone, ship_event(phone, oid, UPS))
    other = complete(client, owner, phone)
    assert codes(sync(client, phone, ship_event(phone, other, UPS))) == "tracking_used"

    flagged = complete(client, owner, phone)
    sync(
        client, phone, {**scan_event(phone, flagged, ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    )
    assert codes(sync(client, phone, ship_event(phone, flagged, "9400111899223334445566"))) == "order_flagged"


def test_ship_queue_and_proof(client):
    owner = signup(client)
    client.patch("/api/warehouse", json={"require_ship_scan": True}, headers=owner.h)
    phone = worker_on_phone(client, owner)
    oid = complete(client, owner, phone)
    listing = client.get("/api/worker/orders", headers=phone.h).json()
    assert listing["require_ship_scan"] is True
    assert [o["id"] for o in listing["to_ship"]] == [oid]
    assert client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()["require_ship_scan"] is True
    sync(client, phone, ship_event(phone, oid, UPS))
    assert client.get("/api/worker/orders", headers=phone.h).json()["to_ship"] == []
    proof = client.get(f"/api/orders/{oid}/proof", headers=owner.h).json()
    assert proof["tracking_number"] == UPS
    assert [p["scanned_barcode"] for p in proof["picks"]] == [UPC_A]
    assert proof["picks"][0]["worker"] == "Maria"
    assert "qr_svg" not in proof


def test_offline_batch_scan_then_ship_in_one_sync(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [(UPC_A, 1)])["id"]
    out = sync(client, phone, scan_event(phone, oid, UPC_A), ship_event(phone, oid, UPS))
    assert [e["status"] for e in out["events"]] == ["applied", "applied"]
    assert detail(client, owner, oid)["status"] == "shipped"


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------


def test_photo_upload_waits_for_flag_and_is_viewable(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    flag = {**scan_event(phone, oid, ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    pid = str(uuid.uuid4())
    assert upload(client, phone, flag["id"], photo_id=pid).status_code == 409  # flag not synced yet
    sync(client, phone, flag)
    r = upload(client, phone, flag["id"], photo_id=pid)
    assert r.status_code == 201 and r.json()["status"] == "applied"
    assert upload(client, phone, flag["id"], photo_id=pid).json()["status"] == "duplicate"

    d = detail(client, owner, oid)
    assert d["flags"][0]["photos"] == [pid]
    img = client.get(f"/api/photos/{pid}", headers=owner.h)
    assert img.status_code == 200 and img.content == JPEG
    assert img.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/photos/{pid}").status_code == 401
    gallery = client.get("/api/photos", headers=owner.h).json()
    assert gallery[0]["id"] == pid and gallery[0]["worker"] == "Maria"
    live = client.get("/api/dashboard/live", headers=owner.h).json()
    assert live["flags"][0]["photos"] == [pid]


def test_photo_validation(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    order = client.get(f"/api/orders/{oid}", headers=owner.h).json()
    ev = short_event(phone, oid, order["lines"][0]["id"], 1)
    sync(client, phone, ev)
    assert upload(client, phone, ev["id"], ctype="image/gif").status_code == 415
    assert upload(client, phone, ev["id"], data=b"not an image").status_code == 400
    assert upload(client, phone, ev["id"], data=JPEG + b"\0" * 1_600_000).status_code == 413
    for _ in range(4):
        assert upload(client, phone, ev["id"]).status_code == 201
    assert upload(client, phone, ev["id"]).json()["detail"]["code"] == "photo_limit"


def test_worker_summary_counts_shipped_orders(client):
    owner = signup(client)
    w = add_worker(client, owner, "Devon")
    assert w["pin"]
    phone = worker_on_phone(client, owner, "Sam")
    oid = complete(client, owner, phone)
    sync(client, phone, ship_event(phone, oid, UPS))
    s = client.get("/api/worker/summary", headers=phone.h).json()
    assert s["orders_completed"] == 1
