"""Lot, serial and expiry: captured from GS1 barcodes or typed, checked
before the unit counts, and searchable for recalls."""

from __future__ import annotations

from datetime import date

from conftest import scan, scan_event, signup, sync, worker_on_phone

from autorack.services.scans import gs1_date

GTIN = "09501101530003"
GS = "\x1d"


def order_with(client, owner, number="SO-T1", **line):
    r = client.post(
        "/api/orders",
        json={"external_order_number": number, "lines": [{"barcode": GTIN, "quantity": 3, **line}]},
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def history(client, owner, oid):
    return client.get(f"/api/orders/{oid}/scans", headers=owner.h).json()


def test_gs1_barcode_fills_lot_and_expiry(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    o = order_with(client, owner, track_lot=True, track_expiry=True)
    assert o["lines"][0]["track_lot"] and o["lines"][0]["track_expiry"]

    # GS1-128 with FNC1 separators, as a scanner sends it.
    r = scan(client, phone, o["id"], f"01{GTIN}17301231{GS}10LOT-7")
    assert r["result"] == "match", r
    # Human-readable form, bracketed.
    assert scan(client, phone, o["id"], f"(01){GTIN}(17)301200(10)LOT-8")["result"] == "match"
    h = history(client, owner, o["id"])
    assert {(s["lot"], s["expiry"]) for s in h} == {("LOT-7", "2030-12-31"), ("LOT-8", "2030-12-31")}

    # A plain product barcode: the phone has to ask. Without it, not counted.
    r = scan(client, phone, o["id"], GTIN)
    assert (r["result"], r["problem"]) == ("review", "details_missing")
    r = scan(client, phone, o["id"], GTIN, lot="LOT-9", expiry="2031-06-30")
    assert r["result"] == "match"
    d = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    assert d["lines"][0]["scanned_quantity"] == 3


def test_wrong_lot_and_expired_are_mistakes_caught(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    o = order_with(client, owner, required_lot="A100")
    assert o["lines"][0]["track_lot"] is True  # a required lot implies recording it
    r = scan(client, phone, o["id"], f"01{GTIN}10B200")
    assert (r["result"], r["problem"]) == ("mismatch", "wrong_lot")
    assert scan(client, phone, o["id"], f"01{GTIN}10a100")["result"] == "match"  # case doesn't matter

    exp = order_with(client, owner, number="SO-T2", track_expiry=True)
    r = scan(client, phone, exp["id"], f"01{GTIN}17200101")
    assert (r["result"], r["problem"]) == ("mismatch", "expired")
    s = client.get("/api/dashboard/summary", headers=owner.h).json()
    assert s["today"]["mismatches"] == 2 and s["today"]["units_picked"] == 1


def test_a_serial_ships_once(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    a = order_with(client, owner, number="SO-S1", track_serial=True)
    b = order_with(client, owner, number="SO-S2", track_serial=True)
    first = scan(client, phone, a["id"], f"01{GTIN}21SN-0001")
    assert first["result"] == "match"
    r = scan(client, phone, b["id"], GTIN, serial="SN-0001")
    assert (r["result"], r["problem"]) == ("mismatch", "serial_repeat")
    # Undo the first: the serial is free again.
    undo = {**scan_event(phone, a["id"], ""), "kind": "void", "scanned_barcode": None, "target_scan_id": first["id"]}
    sync(client, phone, undo)
    assert scan(client, phone, b["id"], GTIN, serial="SN-0001")["result"] == "match"


def test_recall_search_export_and_proof(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    o = order_with(client, owner, number="SO-R1", track_lot=True)
    order_with(client, owner, number="SO-R2", track_lot=True)
    scan(client, phone, o["id"], f"01{GTIN}10RECALL-42")
    found = client.get("/api/orders?q=recall-42", headers=owner.h).json()["orders"]
    assert [x["external_order_number"] for x in found] == ["SO-R1"]
    csv = client.get("/api/exports/scans.csv", headers=owner.h).text
    assert csv.splitlines()[0].endswith("lot,serial,expiry,problem")
    assert "RECALL-42" in csv
    proof = client.get(f"/api/orders/{o['id']}/proof", headers=owner.h).json()
    assert proof["picks"][0]["lot"] == "RECALL-42"


def test_csv_track_and_lot_columns(client):
    owner = signup(client)
    csv = (
        b"order_number,barcode,quantity,track,lot\n"
        b"SO-C1,012345678905,1,lot+expiry,\n"
        b"SO-C1,036000291452,2,serial,\n"
        b"SO-C2,012345678905,1,,L-55\n"
    )
    r = client.post("/api/orders/import", files={"file": ("t.csv", csv, "text/csv")}, headers=owner.h)
    assert r.status_code == 201, r.text
    orders = {o["external_order_number"]: o for o in client.get("/api/orders", headers=owner.h).json()["orders"]}
    c1 = client.get(f"/api/orders/{orders['SO-C1']['id']}", headers=owner.h).json()["lines"]
    flags = {li["expected_barcode"]: (li["track_lot"], li["track_serial"], li["track_expiry"]) for li in c1}
    assert flags == {"012345678905": (True, False, True), "036000291452": (False, True, False)}
    c2 = client.get(f"/api/orders/{orders['SO-C2']['id']}", headers=owner.h).json()["lines"][0]
    assert (c2["required_lot"], c2["track_lot"]) == ("L-55", True)
    # Edit a line's requirements.
    r = client.patch(
        f"/api/orders/{orders['SO-C2']['id']}/lines/{c2['id']}",
        json={"required_lot": "", "track_serial": True},
        headers=owner.h,
    )
    line = r.json()["lines"][0]
    assert (line["required_lot"], line["track_serial"]) == (None, True)


def test_receiving_records_lots_without_refusing(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    r = client.post(
        "/api/orders",
        json={"kind": "receive", "lines": [{"barcode": GTIN, "quantity": 2, "track_lot": True, "track_expiry": True}]},
        headers=owner.h,
    )
    po = r.json()
    # Expired stock arriving is recorded (and counted); the variance shows it.
    assert scan(client, phone, po["id"], f"01{GTIN}17200101{GS}10OLD")["result"] == "counted"
    assert scan(client, phone, po["id"], GTIN)["result"] == "counted"
    lots = {s["lot"] for s in history(client, owner, po["id"])}
    assert lots == {"OLD", None}


def test_gs1_dates():
    assert gs1_date("300229") is None  # 2030 is not a leap year
    assert gs1_date("280200") == date(2028, 2, 29)  # day 00 = end of month
    assert gs1_date("251301") is None
    assert gs1_date("abc") is None
