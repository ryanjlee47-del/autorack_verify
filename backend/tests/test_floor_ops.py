"""Rush orders and cutoffs, pack inserts, multi-box shipments, restock tasks,
and the time clock."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from conftest import JPEG, make_order, scan, scan_event, signup, sync, worker_on_phone

from autorack.db import get_sessionmaker
from autorack.models import Order, Shift
from autorack.services import floor

UPC = "012345678905"
TAPE = "036000291452"


def ev(phone, order_id, kind, **extra):
    return {**scan_event(phone, order_id, ""), "kind": kind, "scanned_barcode": None, **extra}


def test_rush_and_ship_by_order_the_phone_list(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    plain = make_order(client, owner, [(UPC, 1)], number="SO-PLAIN")
    rush = client.post(
        "/api/orders",
        json={"external_order_number": "SO-RUSH", "rush": True, "lines": [{"barcode": UPC}]},
        headers=owner.h,
    ).json()
    late = client.post(
        "/api/orders",
        json={"external_order_number": "SO-LATE", "ship_by": "2020-01-02", "lines": [{"barcode": UPC}]},
        headers=owner.h,
    ).json()
    assert rush["rush"] and late["late"] and late["ship_by"].startswith("2020-01-02")
    rows = client.get("/api/worker/orders", headers=phone.h).json()["orders"]
    assert [r["external_order_number"] for r in rows] == ["SO-RUSH", "SO-LATE", "SO-PLAIN"]
    assert rows[1]["late"] is True

    def by_due(d):
        rows = client.get(f"/api/orders?due={d}", headers=owner.h).json()["orders"]
        return [o["external_order_number"] for o in rows]

    assert by_due("rush") == ["SO-RUSH"]
    assert by_due("late") == ["SO-LATE"]
    assert set(by_due("today")) == {"SO-RUSH", "SO-LATE"}
    summary = client.get("/api/dashboard/summary", headers=owner.h).json()["orders"]
    assert (summary["rush"], summary["late"]) == (1, 1)
    # Un-rush, move the date.
    r = client.patch(f"/api/orders/{rush['id']}", json={"rush": False, "ship_by": "2999-01-01"}, headers=owner.h)
    assert r.json()["rush"] is False and r.json()["late"] is False
    assert plain["id"]


def test_daily_cutoff_sets_due_times(client):
    owner = signup(client)
    client.patch("/api/warehouse", json={"ship_cutoff": "00:00"}, headers=owner.h)
    o = make_order(client, owner)
    detail = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    # Created after midnight's cutoff: due at tomorrow's.
    assert detail["due_at"] and not detail["late"]
    assert client.patch("/api/warehouse", json={"ship_cutoff": "25:00"}, headers=owner.h).status_code == 422
    assert client.patch("/api/warehouse", json={"ship_cutoff": ""}, headers=owner.h).json()["ship_cutoff"] is None


def test_csv_import_reads_rush_ship_by_and_client(client):
    owner = signup(client)
    client.post("/api/clients", json={"name": "Glow Skincare", "code": "GLOW"}, headers=owner.h)
    csv = (
        b"order,barcode,qty,priority,ship by,brand\n"
        b"SO-1,012345678905,1,Rush,2030-05-01,GLOW\n"
        b"SO-2,012345678905,1,,05/02/2030,Unknown brand\n"
    )
    r = client.post("/api/orders/import", files={"file": ("o.csv", csv, "text/csv")}, headers=owner.h)
    assert r.status_code == 201, r.text
    rows = {o["external_order_number"]: o for o in client.get("/api/orders", headers=owner.h).json()["orders"]}
    assert rows["SO-1"]["rush"] and rows["SO-1"]["ship_by"].startswith("2030-05-01")
    assert rows["SO-1"]["client"] == "Glow Skincare" and rows["SO-2"]["client"] is None
    assert not rows["SO-2"]["rush"] and rows["SO-2"]["ship_by"].startswith("2030-05-02")


def test_inserts_must_be_in_the_box_before_the_label(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    flyer = client.post("/api/inserts", json={"name": "Spring flyer"}, headers=owner.h).json()
    card = client.post(
        "/api/inserts", json={"name": "Thank-you card", "barcode": "CARD-01", "scan_required": True}, headers=owner.h
    ).json()
    assert client.post("/api/inserts", json={"name": "x", "scan_required": True}, headers=owner.h).status_code == 400
    o = make_order(client, owner, [(UPC, 1)])
    pl = client.get(f"/api/worker/orders/{o['id']}", headers=phone.h).json()
    assert [i["name"] for i in pl["inserts"]] == ["Spring flyer", "Thank-you card"]
    scan(client, phone, o["id"], UPC)
    out = sync(client, phone, ev(phone, o["id"], "ship", tracking_number="1Z999AA10123456784"))
    assert out["events"][0]["error"]["code"] == "inserts_missing"
    out = sync(
        client,
        phone,
        ev(phone, o["id"], "insert", insert_id=flyer["id"]),
        ev(phone, o["id"], "insert", insert_id=card["id"]),
        ev(phone, o["id"], "insert", insert_id=card["id"], scanned_barcode="WRONG"),
        ev(phone, o["id"], "insert", insert_id=card["id"], scanned_barcode="card-01"),
    )
    codes = [e.get("error", {}).get("code") or e["result"] for e in out["events"]]
    assert codes == ["checked", "insert_scan_required", "insert_wrong", "checked"]
    assert sorted(out["orders"][o["id"]]["inserts_done"]) == sorted([flyer["id"], card["id"]])
    out = sync(client, phone, ev(phone, o["id"], "ship", tracking_number="1Z999AA10123456784"))
    assert out["events"][0]["result"] == "shipped"
    detail = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    assert all(i["done"] for i in detail["inserts"])


def test_inserts_scoped_to_client_or_product(client):
    owner = signup(client)
    glow = client.post("/api/clients", json={"name": "Glow"}, headers=owner.h).json()
    p = client.post("/api/products", json={"name": "Serum", "sku": "SER", "barcode": TAPE}, headers=owner.h).json()
    client.post("/api/inserts", json={"name": "Glow card", "client_id": glow["id"]}, headers=owner.h)
    client.post("/api/inserts", json={"name": "Serum leaflet", "product_id": p["id"]}, headers=owner.h)
    phone = worker_on_phone(client, owner)
    plain = make_order(client, owner, [(UPC, 1)])
    glow_order = client.post(
        "/api/orders", json={"client_id": glow["id"], "lines": [{"barcode": "SER"}]}, headers=owner.h
    ).json()
    names = lambda oid: [i["name"] for i in client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()["inserts"]]  # noqa: E731
    assert names(plain["id"]) == []
    assert names(glow_order["id"]) == ["Glow card", "Serum leaflet"]


def test_an_order_can_ship_in_several_boxes(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    o = make_order(client, owner, [(UPC, 2)])
    scan(client, phone, o["id"], UPC)
    scan(client, phone, o["id"], UPC)
    first = ev(phone, o["id"], "ship", tracking_number="1Z999AA10123456784", final=False)
    out = sync(client, phone, first)
    assert out["events"][0]["result"] == "boxed" and out["orders"][o["id"]]["boxes"] == 1
    assert sync(client, phone, first)["events"][0]["status"] == "duplicate"
    # A box photo for box 2.
    r = client.post(
        "/api/worker/photos",
        params={"id": str(uuid.uuid4()), "order_id": o["id"], "kind": "pack", "box": 2},
        content=JPEG,
        headers={**phone.h, "Content-Type": "image/jpeg"},
    )
    assert r.status_code == 201
    same = sync(client, phone, ev(phone, o["id"], "ship", tracking_number="1Z999AA10123456784", final=False))
    assert same["events"][0]["error"]["code"] == "tracking_same_box"
    sync(client, phone, ev(phone, o["id"], "ship", tracking_number="9400111899223197428490", final=False))
    out = sync(client, phone, ev(phone, o["id"], "ship"))  # "that's all the boxes"
    assert out["events"][0]["result"] == "shipped"
    detail = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    assert [(b["box"], b["carrier"]) for b in detail["packages"]] == [(1, "UPS"), (2, "USPS")]
    assert len(detail["packages"][1]["photos"]) == 1
    assert detail["tracking_number"] == "1Z999AA10123456784"
    # Box 2's label can't go on another order, and finds this one for a return.
    other = make_order(client, owner, [(UPC, 1)])
    scan(client, phone, other["id"], UPC)
    clash = sync(client, phone, ev(phone, other["id"], "ship", tracking_number="9400111899223197428490"))
    assert clash["events"][0]["error"]["code"] == "tracking_used"
    ret = client.post("/api/worker/returns", json={"code": "9400111899223197428490"}, headers=phone.h)
    assert ret.status_code == 201, ret.text
    token = client.post(f"/api/orders/{o['id']}/share", headers=owner.h).json()["url"].split("#t=")[1]
    assert len(client.get(f"/api/public/proof/{token}").json()["boxes"]) == 2


def test_empty_bins_become_restock_tasks(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    o = client.post(
        "/api/orders", json={"lines": [{"barcode": UPC, "quantity": 3, "location": "A-01"}]}, headers=owner.h
    ).json()
    line = o["lines"][0]["id"]
    first = ev(phone, o["id"], "restock", line_item_id=line)
    out = sync(client, phone, first, ev(phone, o["id"], "restock", line_item_id=line))
    assert [e["result"] for e in out["events"]] == ["reported", "already_reported"]
    assert sync(client, phone, first)["events"][0]["status"] == "duplicate"
    tasks = client.get("/api/worker/restock", headers=phone.h).json()["tasks"]
    assert [(t["location"], t["barcode"]) for t in tasks] == [("A-01", UPC)]
    done = client.post(f"/api/worker/restock/{tasks[0]['id']}/done", headers=phone.h).json()
    assert done["status"] == "done" and done["done_by"] == "Maria"
    # A short pick "out of stock" opens one by itself.
    short = ev(phone, o["id"], "short", line_item_id=line, quantity=1, short_reason="out_of_stock")
    sync(client, phone, short)
    listed = client.get("/api/restock", headers=owner.h).json()["tasks"]
    assert [t["source"] for t in listed] == ["short_pick"]
    assert client.get("/api/dashboard/summary", headers=owner.h).json()["restock_open"] == 1
    assert client.post(f"/api/restock/{listed[0]['id']}/cancel", headers=owner.h).json()["status"] == "cancelled"


def test_time_clock_and_units_per_hour(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    assert client.post("/api/worker/clock-in", headers=phone.h).json()["detail"]["code"] == "time_clock_off"
    client.patch("/api/warehouse", json={"time_clock_enabled": True}, headers=owner.h)
    state = client.post("/api/worker/clock-in", headers=phone.h).json()
    assert (
        state["shift"]
        and client.post("/api/worker/clock-in", headers=phone.h).json()["shift"]["id"] == state["shift"]["id"]
    )
    # Pretend the shift started two hours ago.
    with get_sessionmaker()() as db:
        s = db.get(Shift, uuid.UUID(state["shift"]["id"]))
        s.clock_in = datetime.now(UTC) - timedelta(hours=2)
        db.commit()
    o = make_order(client, owner, [(UPC, 10)])
    for _ in range(10):
        scan(client, phone, o["id"], UPC)
    summary = client.get("/api/worker/summary", headers=phone.h).json()
    assert summary["uph"] == 5.0 and 1.9 < summary["clock_hours"] < 2.1
    stats = client.get("/api/dashboard/workers", headers=owner.h).json()["workers"][0]
    assert stats["uph"] == 5.0
    out = client.post("/api/worker/clock-out", headers=phone.h).json()
    assert out["shift"] is None and out["last"]["hours"] >= 2
    shifts = client.get("/api/shifts", headers=owner.h).json()
    assert shifts["totals"][0]["worker"] == "Maria"
    sid = shifts["shifts"][0]["id"]
    fixed = client.patch(
        f"/api/shifts/{sid}", json={"clock_out": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}, headers=owner.h
    ).json()
    assert fixed["edited"] and 0.9 < fixed["hours"] < 1.1
    csv = client.get("/api/exports/timesheet.csv", headers=owner.h).text
    assert csv.startswith("worker,date,clock_in") and "Maria" in csv


def test_forgotten_clock_outs_close_themselves(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    client.patch("/api/warehouse", json={"time_clock_enabled": True}, headers=owner.h)
    sid = client.post("/api/worker/clock-in", headers=phone.h).json()["shift"]["id"]
    s = db.get(Shift, uuid.UUID(sid))
    s.clock_in = datetime.now(UTC) - timedelta(hours=20)
    db.commit()
    assert floor.close_stale_shifts(db) == 1
    db.commit()
    db.refresh(s)
    assert s.closed_by == "auto" and s.clock_out - s.clock_in == floor.MAX_SHIFT
    assert db.query(Order).count() == 0
