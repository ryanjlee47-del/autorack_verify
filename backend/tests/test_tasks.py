"""Receiving, returns and cycle counts: tally what's there, finish by hand,
report the variance, and never leak into pick metrics."""

from __future__ import annotations

import uuid

from conftest import make_order, now_iso, scan, scan_event, signup, sync, worker_on_phone
from test_floor_features import ship_event

WIDGET = "012345678905"
TAPE = "036000291452"


def create_task(client, owner, kind, lines, number=None, blind=False):
    r = client.post(
        "/api/orders",
        json={
            "kind": kind,
            "blind": blind,
            "external_order_number": number or f"{kind.upper()}-{uuid.uuid4().hex[:5]}",
            "lines": [{"barcode": b, "quantity": q, "description": f"Item {b[-3:]}"} for b, q in lines],
        },
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def finish_event(phone, order_id):
    return {**scan_event(phone, order_id, ""), "kind": "finish", "scanned_barcode": None}


def undo_event(phone, order_id, target):
    return {**scan_event(phone, order_id, ""), "kind": "void", "scanned_barcode": None, "target_scan_id": target}


def detail(client, owner, oid):
    return client.get(f"/api/orders/{oid}", headers=owner.h).json()


def test_receiving_counts_everything_and_reports_variance(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    po = create_task(client, owner, "receive", [(WIDGET, 2), (TAPE, 3)], number="PO-7")
    assert po["kind"] == "receive" and po["status"] == "pending"

    # Over-receipt counts; nothing is refused.
    results = [scan(client, phone, po["id"], WIDGET)["result"] for _ in range(3)]
    assert results == ["counted", "counted", "counted"]
    stray = scan(client, phone, po["id"], "999999999993")
    assert stray["result"] == "extra" and "line_item_id" not in stray
    extra2 = scan(client, phone, po["id"], "999999999993")
    # A mis-scanned extra can be undone.
    out = sync(client, phone, undo_event(phone, po["id"], extra2["id"]))
    assert out["events"][0]["result"] == "uncounted"
    scan(client, phone, po["id"], TAPE)

    # Never auto-completes, even when every line is covered.
    assert detail(client, owner, po["id"])["status"] == "in_progress"
    out = sync(client, phone, finish_event(phone, po["id"]))
    assert out["events"][0]["result"] == "finished"
    d = detail(client, owner, po["id"])
    assert d["status"] == "completed" and d["units_scanned"] == 4
    v = d["variance"]
    by_code = {r["barcode"]: r for r in v["lines"]}
    assert (by_code[WIDGET]["difference"], by_code[WIDGET]["state"]) == (1, "over")
    assert (by_code[TAPE]["difference"], by_code[TAPE]["state"]) == (-2, "short")
    assert v["extras"] == [{"barcode": "999999999993", "counted": 1}]
    assert v["totals"] == {
        "expected": 5,
        "counted": 4,
        "extra": 1,
        "lines_short": 1,
        "lines_over": 1,
        "lines_ok": 0,
    }
    assert v["finished"] and v["finished_by"] == "Maria" and not v["matches"]

    # Finished means finished; a manager can reopen.
    assert scan(client, phone, po["id"], TAPE)["error"]["code"] == "task_finished"
    again = sync(client, phone, finish_event(phone, po["id"]))
    assert again["events"][0]["status"] == "duplicate"
    r = client.post(f"/api/orders/{po['id']}/reopen", headers=owner.h)
    assert r.json()["status"] == "in_progress"
    assert scan(client, phone, po["id"], TAPE)["result"] == "counted"

    csv = client.get(f"/api/orders/{po['id']}/variance.csv", headers=owner.h)
    assert csv.status_code == 200
    assert "autorack-receive-PO-7.csv" in csv.headers["content-disposition"]
    assert "999999999993,,NOT ON THE LIST,,0,1,1,extra" in csv.text


def test_tasks_stay_out_of_pick_metrics_and_lists(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    po = create_task(client, owner, "receive", [(WIDGET, 1)])
    scan(client, phone, po["id"], WIDGET)
    scan(client, phone, po["id"], "999999999993")
    sync(client, phone, finish_event(phone, po["id"]))

    s = client.get("/api/dashboard/summary", headers=owner.h).json()
    assert s["today"]["scans"] == 0 and s["today"]["units_picked"] == 0 and s["today"]["errors_caught"] == 0
    assert s["orders"]["completed_today"] == 0
    workers = client.get("/api/dashboard/workers", headers=owner.h).json()["workers"]
    assert all(w["scans"] == 0 for w in workers)
    # The Orders list is picks; the task is under its own kind.
    assert client.get("/api/orders", headers=owner.h).json()["orders"] == []
    listed = client.get("/api/orders?kind=receive", headers=owner.h).json()["orders"]
    assert [o["id"] for o in listed] == [po["id"]] and listed[0]["kind"] == "receive"
    assert len(client.get("/api/orders?kind=all", headers=owner.h).json()["orders"]) == 1
    assert client.get("/api/orders?kind=nope", headers=owner.h).status_code == 400
    # A finished receipt is never on the packing bench.
    client.patch("/api/warehouse", json={"require_ship_scan": True}, headers=owner.h)
    assert client.get("/api/worker/orders", headers=phone.h).json()["to_ship"] == []


def test_pick_only_events_are_refused_on_tasks(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    po = create_task(client, owner, "receive", [(WIDGET, 1)])
    scan(client, phone, po["id"], WIDGET)
    short = {
        **scan_event(phone, po["id"], ""),
        "kind": "short",
        "scanned_barcode": None,
        "line_item_id": po["lines"][0]["id"],
        "quantity": 1,
        "short_reason": "not_found",
    }
    out = sync(client, phone, short, ship_event(phone, po["id"], "1Z999AA10123456784"))
    assert [e["error"]["code"] for e in out["events"]] == ["not_for_task", "not_for_task"]
    # And a pick can't be "finished" by hand.
    pick = make_order(client, owner)
    out = sync(client, phone, finish_event(phone, pick["id"]))
    assert out["events"][0]["error"]["code"] == "not_for_task"


def test_blind_count_allows_zero_and_hides_expected(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    count = create_task(client, owner, "count", [(WIDGET, 5), (TAPE, 0)], number="CC-A1", blind=True)
    assert count["blind"] is True
    # Zero-quantity lines are only for counts.
    r = client.post("/api/orders", json={"lines": [{"barcode": WIDGET, "quantity": 0}]}, headers=owner.h)
    assert r.status_code == 400
    payload = client.get(f"/api/worker/orders/{count['id']}", headers=phone.h).json()
    assert payload["blind"] and all(li["expected_quantity"] == 0 for li in payload["lines"])
    assert payload["units_expected"] == 0
    row = next(o for o in client.get("/api/worker/orders", headers=phone.h).json()["orders"] if o["id"] == count["id"])
    assert row["kind"] == "count" and row["units_expected"] == 0
    for _ in range(4):
        scan(client, phone, count["id"], WIDGET)
    scan(client, phone, count["id"], TAPE)
    sync(client, phone, finish_event(phone, count["id"]))
    v = detail(client, owner, count["id"])["variance"]
    assert [(r["expected"], r["counted"], r["difference"]) for r in v["lines"]] == [(5, 4, -1), (0, 1, 1)]


def test_return_from_tracking_number(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    pick = make_order(client, owner, [(WIDGET, 2), (TAPE, 1)], number="SO-55")
    for code in (WIDGET, WIDGET, TAPE):
        scan(client, phone, pick["id"], code)
    sync(client, phone, ship_event(phone, pick["id"], "1Z 999 AA1 01234 56784"))

    # Unknown or unshipped: nothing to return.
    assert client.post("/api/worker/returns", json={"code": "nope"}, headers=phone.h).status_code == 404
    open_pick = make_order(client, owner, number="SO-56")
    assert client.post("/api/worker/returns", json={"code": "SO-56"}, headers=phone.h).status_code == 404

    r = client.post("/api/worker/returns", json={"code": "1z999aa10123456784"}, headers=phone.h)
    assert r.status_code == 201, r.text
    ret_id = r.json()["order_id"]
    assert r.json()["number"] == "RET-SO-55"
    # Scanning it again carries on with the same return.
    assert client.post("/api/worker/returns", json={"code": "SO-55"}, headers=phone.h).json()["order_id"] == ret_id

    ret = detail(client, owner, ret_id)
    assert ret["kind"] == "return" and ret["customer"] == pick["customer"]
    assert ret["return_of"]["number"] == "SO-55"
    assert {(li["expected_barcode"], li["expected_quantity"]) for li in ret["lines"]} == {(WIDGET, 2), (TAPE, 1)}

    scan(client, phone, ret_id, WIDGET)
    assert scan(client, phone, ret_id, "999999999993")["result"] == "extra"  # not from this order
    sync(client, phone, finish_event(phone, ret_id))
    v = detail(client, owner, ret_id)["variance"]
    assert v["totals"]["counted"] == 1 and v["totals"]["extra"] == 1
    assert detail(client, owner, pick["id"])["returns"] == [
        {"id": ret_id, "number": "RET-SO-55", "status": "completed"}
    ]

    # A second return of the same order gets its own number; the owner can start one too.
    r = client.post(f"/api/orders/{pick['id']}/return", headers=owner.h)
    assert r.status_code == 201 and r.json()["external_order_number"] == "RET-SO-55-2"
    assert client.post(f"/api/orders/{open_pick['id']}/return", headers=owner.h).status_code == 400
    assert client.get(f"/api/orders/{pick['id']}/variance.csv", headers=owner.h).status_code == 400


def test_import_purchase_orders_and_count_sheets(client):
    owner = signup(client)
    csv = b"po,upc,qty\nPO-1,012345678905,10\nPO-1,036000291452,4\nPO-2,012345678905,6\n"
    r = client.post(
        "/api/orders/import",
        files={"file": ("pos.csv", csv, "text/csv")},
        data={"kind": "receive"},
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["orders_created"] == 2
    listed = client.get("/api/orders?kind=receive", headers=owner.h).json()["orders"]
    assert sorted(o["external_order_number"] for o in listed) == ["PO-1", "PO-2"]

    counts = b"order_number,barcode,quantity,location\nA-01,012345678905,0,A-01-01\n"
    r = client.post(
        "/api/orders/import/preview",
        files={"file": ("c.csv", counts, "text/csv")},
        data={"kind": "count"},
        headers=owner.h,
    )
    assert r.json()["error_count"] == 0
    r = client.post("/api/orders/import/preview", files={"file": ("c.csv", counts, "text/csv")}, headers=owner.h)
    assert r.json()["error_count"] == 1  # 0 isn't a pick quantity
    r = client.post(
        "/api/orders/import",
        files={"file": ("c.csv", counts, "text/csv")},
        data={"kind": "count", "blind": "true"},
        headers=owner.h,
    )
    (c,) = client.get("/api/orders?kind=count", headers=owner.h).json()["orders"]
    assert c["blind"] is True


def test_scan_timestamps_still_validated_for_tasks(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    po = create_task(client, owner, "receive", [(WIDGET, 1)])
    ev = {**scan_event(phone, po["id"], WIDGET), "client_scanned_at": now_iso()}
    assert sync(client, phone, ev)["events"][0]["result"] == "counted"
