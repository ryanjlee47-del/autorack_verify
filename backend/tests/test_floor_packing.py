"""Items with no barcode, the photo of the packed box, and batch picking."""

from __future__ import annotations

import uuid

from conftest import IS_JPEG, JPEG, make_order, scan, scan_event, signup, sync, worker_on_phone

UPC = "012345678905"
TAPE = "036000291452"


def confirm_event(phone, order_id, line_id, quantity=1):
    return {
        **scan_event(phone, order_id, ""),
        "kind": "confirm",
        "scanned_barcode": None,
        "line_item_id": line_id,
        "quantity": quantity,
    }


def test_items_without_a_barcode_are_confirmed_by_tap(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    client.post("/api/products", json={"name": "Gift card", "sku": "GIFT", "no_barcode": True}, headers=owner.h).json()
    o = client.post(
        "/api/orders",
        json={"lines": [{"barcode": UPC, "quantity": 1}, {"barcode": "GIFT", "quantity": 2}]},
        headers=owner.h,
    )
    assert o.status_code == 201, o.text
    o = o.json()
    lines = {li["description"] or li["expected_barcode"]: li for li in o["lines"]}
    gift = lines["Gift card"]
    assert gift["confirm_without_scan"]
    # A barcoded line can't be tapped through.
    out = sync(client, phone, confirm_event(phone, o["id"], lines[UPC]["id"]))
    assert out["events"][0]["error"]["code"] == "scan_required"
    ev = confirm_event(phone, o["id"], gift["id"], 5)
    out = sync(client, phone, ev)
    assert (out["events"][0]["result"], out["events"][0]["quantity"]) == ("match", 2)  # capped at what's left
    assert sync(client, phone, ev)["events"][0]["status"] == "duplicate"
    assert scan(client, phone, o["id"], UPC)["order"]["status"] == "completed"
    hist = client.get(f"/api/orders/{o['id']}/scans", headers=owner.h).json()
    assert [s["confirmed"] for s in hist if s["result"] == "match"].count(True) == 1
    # Undo works like any pick.
    undo = {**scan_event(phone, o["id"], ""), "kind": "void", "scanned_barcode": None, "target_scan_id": ev["id"]}
    assert sync(client, phone, undo)["orders"][o["id"]]["lines"][gift["id"]] == 0
    # The proof says it was confirmed by hand, not scanned.
    sync(client, phone, confirm_event(phone, o["id"], gift["id"], 2))
    proof = client.get(f"/api/orders/{o['id']}/proof", headers=owner.h).json()
    assert any(p["confirmed"] for p in proof["picks"])


def upload_pack_photo(client, phone, order_id, photo_id=None):
    return client.post(
        "/api/worker/photos",
        params={"id": photo_id or str(uuid.uuid4()), "order_id": order_id, "kind": "pack"},
        content=JPEG,
        headers={**phone.h, "Content-Type": "image/jpeg"},
    )


def test_packed_box_photo_shows_on_order_and_shared_proof(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    r = client.patch("/api/warehouse", json={"require_pack_photo": True}, headers=owner.h)
    assert r.json()["require_pack_photo"] is True
    o = make_order(client, owner, [(UPC, 1)])
    assert client.get(f"/api/worker/orders/{o['id']}", headers=phone.h).json()["require_pack_photo"] is True
    scan(client, phone, o["id"], UPC)
    pid = str(uuid.uuid4())
    r = upload_pack_photo(client, phone, o["id"], pid)
    assert r.status_code == 201, r.text
    assert upload_pack_photo(client, phone, o["id"], pid).json()["status"] == "duplicate"
    assert client.get(f"/api/worker/orders/{o['id']}", headers=phone.h).json()["pack_photos"] == 1
    detail = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    assert detail["pack_photos"] == [pid]
    assert client.get(f"/api/photos/{pid}", headers=owner.h).content.startswith(IS_JPEG)
    # Not a problem photo: the problem-photo list doesn't show it.
    assert client.get("/api/photos", headers=owner.h).json() == []
    token = client.post(f"/api/orders/{o['id']}/share", headers=owner.h).json()["url"].split("#t=")[1]
    public = client.post("/api/public/proof", json={"token": token}).json()
    assert public["pack_photos"] == [pid]
    link = public["photo_links"][pid]
    assert token not in link  # the share token never goes into a URL
    img = client.get(link)
    assert img.status_code == 200 and img.content.startswith(IS_JPEG)
    # A signed link names one photo: swapping in another id doesn't work.
    other = make_order(client, owner, [(UPC, 1)])
    other_pid = str(uuid.uuid4())
    upload_pack_photo(client, phone, other["id"], other_pid)
    assert client.get(link.replace(pid, other_pid)).status_code == 404
    assert client.get(link[:-4] + "0000").status_code == 404  # tampered signature
    # Six at most per order.
    for _ in range(5):
        upload_pack_photo(client, phone, o["id"])
    assert upload_pack_photo(client, phone, o["id"]).json()["detail"]["code"] == "photo_limit"


def test_pack_photos_need_an_order_in_this_warehouse(client):
    a = signup(client, "A")
    b = signup(client, "B")
    a_phone = worker_on_phone(client, a)
    b_order = make_order(client, b)
    assert upload_pack_photo(client, a_phone, b_order["id"]).status_code == 404
    r = client.post(
        "/api/worker/photos",
        params={"id": str(uuid.uuid4()), "kind": "pack"},
        content=JPEG,
        headers={**a_phone.h, "Content-Type": "image/jpeg"},
    )
    assert r.json()["detail"]["code"] == "order_required"


def test_batch_picking_puts_each_order_in_its_tote(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    one = make_order(client, owner, [(UPC, 1)], number="SO-1")
    two = make_order(client, owner, [(UPC, 1), (TAPE, 1)], number="SO-2")
    solo = make_order(client, owner, [(TAPE, 1)], number="SO-3")
    r = client.post("/api/batches", json={"order_ids": [one["id"], two["id"]]}, headers=owner.h)
    assert r.status_code == 201, r.text
    batch = r.json()
    assert [(x["external_order_number"], x["tote"]) for x in batch["orders"]] == [("SO-1", "A"), ("SO-2", "B")]
    assert batch["number"] == "B0001" and batch["orders_left"] == 2
    # The phone lists the batch instead of its orders.
    listed = client.get("/api/worker/orders", headers=phone.h).json()
    assert [o["id"] for o in listed["orders"]] == [solo["id"]]
    assert [b["id"] for b in listed["batches"]] == [batch["id"]]
    payload = client.get(f"/api/worker/batches/{batch['id']}", headers=phone.h).json()
    assert [(o["tote"], len(o["lines"])) for o in payload["orders"]] == [("A", 1), ("B", 2)]
    # An order can be in one open batch only, and only while it waits to be picked.
    r = client.post("/api/batches", json={"order_ids": [one["id"], solo["id"]]}, headers=owner.h)
    assert r.json()["detail"]["code"] == "batch_already"
    assert client.post("/api/batches", json={"order_ids": [solo["id"]]}, headers=owner.h).status_code == 400
    # Scans land on the individual orders; the batch closes when they're done.
    for oid, code in ((one["id"], UPC), (two["id"], UPC), (two["id"], TAPE)):
        assert scan(client, phone, oid, code)["result"] == "match"
    assert client.get(f"/api/batches/{batch['id']}", headers=owner.h).json()["closed_at"]
    assert client.get("/api/worker/orders", headers=phone.h).json()["batches"] == []
    assert client.get(f"/api/orders/{two['id']}", headers=owner.h).json()["batch"]["tote"] == "B"


def test_releasing_a_batch_returns_its_orders(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    one = make_order(client, owner, [(UPC, 1)])
    two = make_order(client, owner, [(TAPE, 1)])
    bid = client.post("/api/batches", json={"order_ids": [one["id"], two["id"]]}, headers=owner.h).json()["id"]
    assert client.delete(f"/api/batches/{bid}", headers=owner.h).status_code == 204
    listed = client.get("/api/worker/orders", headers=phone.h).json()
    assert {o["id"] for o in listed["orders"]} == {one["id"], two["id"]} and listed["batches"] == []
    assert client.get(f"/api/orders/{one['id']}", headers=owner.h).json()["tote"] is None
