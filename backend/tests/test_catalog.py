"""The product catalog: linking orders to products, pictures, case packs,
kits and approved substitutes."""

from __future__ import annotations

import io

from conftest import make_order, scan, scan_event, signup, sync, worker_on_phone
from PIL import Image

UPC = "012345678905"
CASE = "10012345678902"
TAPE = "036000291452"


def product(client, owner, **fields):
    r = client.post("/api/products", json={"name": "Blue widget", **fields}, headers=owner.h)
    assert r.status_code == 201, r.text
    return r.json()


def png(color=(20, 120, 220), size=(900, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def payload(client, phone, oid):
    return client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()


def test_orders_link_to_products_and_carry_picture_and_note(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    p = product(client, owner, sku="WID-BLU", barcode=UPC, location="A-01", packer_note="Fragile: bubble wrap")
    r = client.post(f"/api/products/{p['id']}/image", content=png(), headers={**owner.h, "Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    assert r.json()["thumb"].startswith("data:image/jpeg;base64,")
    full = client.get(f"/api/products/{p['id']}/image", headers=owner.h)
    assert full.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(full.content)).size) == 900  # under 1024: not upscaled

    # The order only knows the SKU; the phone can still scan the real UPC.
    o = client.post("/api/orders", json={"lines": [{"barcode": "WID-BLU", "quantity": 2}]}, headers=owner.h).json()
    line = o["lines"][0]
    assert line["product_id"] == p["id"] and line["location"] == "A-01" and line["description"] == "Blue widget"
    pl = payload(client, phone, o["id"])
    info = pl["products"][p["id"]]
    assert info["packer_note"] == "Fragile: bubble wrap" and info["thumb"].startswith("data:image/jpeg")
    assert scan(client, phone, o["id"], UPC)["result"] == "match"


def test_products_added_later_link_open_orders(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    o = make_order(client, owner, [("TAPE-48", 1)])
    before = payload(client, phone, o["id"])["version"]
    assert scan(client, phone, o["id"], TAPE)["result"] == "mismatch"
    product(client, owner, name="Packing tape", sku="TAPE-48", barcode=TAPE)
    after = payload(client, phone, o["id"])
    assert after["version"] > before and after["lines"][0]["product_id"]
    assert scan(client, phone, o["id"], TAPE)["result"] == "match"


def test_case_barcode_counts_its_pack_size(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    p = product(client, owner, sku="WID", barcode=UPC)
    r = client.post(f"/api/products/{p['id']}/barcodes", json={"barcode": CASE, "pack_qty": 12}, headers=owner.h)
    assert r.json()["barcodes"] == [
        {"id": r.json()["barcodes"][0]["id"], "barcode": CASE, "pack_qty": 12, "label": "Case of 12"}
    ]
    o = make_order(client, owner, [(UPC, 25)])
    assert payload(client, phone, o["id"])["match"]["packs"] == {CASE: 12}
    first = scan(client, phone, o["id"], CASE)
    assert (first["result"], first["quantity"]) == ("match", 12)
    assert scan(client, phone, o["id"], CASE)["result"] == "match"
    assert scan(client, phone, o["id"], CASE)["result"] == "over_pick"  # 1 left: open the case
    assert scan(client, phone, o["id"], UPC)["result"] == "match"
    d = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    assert d["lines"][0]["scanned_quantity"] == 25 and d["status"] == "completed"
    # Undoing a case scan takes 12 off.
    undo = {**scan_event(phone, o["id"], ""), "kind": "void", "scanned_barcode": None, "target_scan_id": first["id"]}
    sync(client, phone, undo)
    assert client.get(f"/api/orders/{o['id']}", headers=owner.h).json()["lines"][0]["scanned_quantity"] == 13
    # Units, not scans, in the numbers.
    t = client.get("/api/dashboard/summary", headers=owner.h).json()["today"]
    assert t["units_picked"] == 13 and t["over_picks"] == 1


def test_ordering_by_case_barcode_means_units(client):
    owner = signup(client)
    p = product(client, owner, sku="WID", barcode=UPC)
    client.post(f"/api/products/{p['id']}/barcodes", json={"barcode": CASE, "pack_qty": 12}, headers=owner.h)
    o = make_order(client, owner, [(CASE, 2)])
    assert o["lines"][0]["expected_quantity"] == 24


def test_kits_are_picked_as_their_parts(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    a = product(client, owner, name="Widget", sku="A", barcode=UPC)
    b = product(client, owner, name="Tape", sku="B", barcode=TAPE)
    kit = product(client, owner, name="Starter kit", sku="KIT-1")
    r = client.put(
        f"/api/products/{kit['id']}/components",
        json={"components": [{"product_id": a["id"], "quantity": 2}, {"product_id": b["id"], "quantity": 1}]},
        headers=owner.h,
    )
    assert [c["quantity"] for c in r.json()["components"]] == [1, 2] or len(r.json()["components"]) == 2
    o = client.post(
        "/api/orders",
        json={"lines": [{"barcode": "KIT-1", "quantity": 3}, {"barcode": UPC, "quantity": 1}]},
        headers=owner.h,
    ).json()
    lines = {li["expected_barcode"]: li for li in o["lines"]}
    assert lines[UPC]["expected_quantity"] == 7  # 3 kits x 2 + 1 loose, merged
    assert lines[TAPE]["expected_quantity"] == 3 and lines[TAPE]["kit_name"] == "Starter kit"
    assert scan(client, phone, o["id"], TAPE)["result"] == "match"
    # Kits can't nest or contain themselves.
    assert (
        client.put(
            f"/api/products/{a['id']}/components", json={"components": [{"product_id": kit["id"]}]}, headers=owner.h
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/products/{kit['id']}/components", json={"components": [{"product_id": kit["id"]}]}, headers=owner.h
        ).status_code
        == 400
    )
    listed = client.get("/api/products?show=kits", headers=owner.h).json()["products"]
    assert [p["sku"] for p in listed] == ["KIT-1"]


def test_approved_substitute_counts_and_is_recorded(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    a = product(client, owner, name="Blue widget", sku="A", barcode=UPC)
    c = product(client, owner, name="Blue widget (new packaging)", sku="C", barcode=TAPE)
    r = client.post(
        f"/api/products/{a['id']}/substitutes", json={"substitute_id": c["id"], "note": "Same item"}, headers=owner.h
    )
    assert r.status_code == 201 and r.json()["substitutes"][0]["name"] == "Blue widget (new packaging)"
    o = make_order(client, owner, [(UPC, 2)])
    assert payload(client, phone, o["id"])["match"]["subs"] == {TAPE: "Blue widget (new packaging)"}
    sub = scan(client, phone, o["id"], TAPE)
    assert (sub["result"], sub["substitution"]) == ("match", True)
    assert scan(client, phone, o["id"], "099999999993")["result"] == "mismatch"
    hist = client.get(f"/api/orders/{o['id']}/scans", headers=owner.h).json()
    assert any(s["substitution"] for s in hist)
    # Removing the approval stops it.
    client.delete(f"/api/products/{a['id']}/substitutes/{c['id']}", headers=owner.h)
    assert scan(client, phone, o["id"], TAPE)["result"] == "mismatch"


def test_barcodes_and_skus_are_unique(client):
    owner = signup(client)
    product(client, owner, sku="A", barcode=UPC)
    assert client.post("/api/products", json={"name": "x", "barcode": UPC}, headers=owner.h).status_code == 400
    assert client.post("/api/products", json={"name": "x", "sku": "a"}, headers=owner.h).status_code == 409
    assert client.post("/api/products", json={"name": "nothing"}, headers=owner.h).status_code == 400


def test_assign_barcode_for_items_without_one(client):
    owner = signup(client)
    p = product(client, owner, name="Gift card", sku="GIFT-CARD")
    r = client.post(f"/api/products/{p['id']}/assign-barcode", headers=owner.h)
    assert r.json()["barcode"] == "GIFT-CARD"
    q = product(client, owner, name="Loose screws", no_barcode=True)
    assert client.post(f"/api/products/{q['id']}/assign-barcode", headers=owner.h).json()["barcode"].startswith("AR")


def test_csv_import_creates_updates_and_reports(client):
    owner = signup(client)
    csv = (
        b"SKU,UPC,Title,Bin,Weight (lb),Case barcode,Case qty,Track\n"
        b"WID,012345678905,Blue widget,A-01,1.5,10012345678902,12,\n"
        b"SAL,09501101530003,Saline,C-02,,,,lot+expiry\n"
        b"BAD,012345678905,Duplicate barcode,,,,,\n"
        b",,,,,,,\n"
    )
    r = client.post("/api/products/import", files={"file": ("p.csv", csv, "text/csv")}, headers=owner.h)
    body = r.json()
    assert (body["created"], body["updated"], body["error_count"]) == (2, 0, 1), body
    assert "already belongs to Blue widget" in body["errors"][0]["message"], body
    csv2 = b"sku,name,location\nWID,Blue widget 12pk,A-02\n"
    body = client.post("/api/products/import", files={"file": ("p.csv", csv2, "text/csv")}, headers=owner.h).json()
    assert (body["created"], body["updated"]) == (0, 1)
    listed = {p["sku"]: p for p in client.get("/api/products", headers=owner.h).json()["products"]}
    assert (
        listed["WID"]["location"] == "A-02" and listed["WID"]["weight_grams"] == 680 and listed["WID"]["max_pack"] == 12
    )
    assert listed["SAL"]["track_lot"] and listed["SAL"]["track_expiry"]
    tmpl = client.get("/api/products/template.csv", headers=owner.h)
    assert tmpl.status_code == 200 and tmpl.text.startswith("sku,barcode,name")


def test_bad_pictures_are_refused(client):
    owner = signup(client)
    p = product(client, owner, sku="A", barcode=UPC)
    r = client.post(
        f"/api/products/{p['id']}/image", content=b"not an image", headers={**owner.h, "Content-Type": "image/png"}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "image_invalid"


def test_archived_products_stop_linking(client):
    owner = signup(client)
    p = product(client, owner, sku="A", barcode=UPC)
    assert client.delete(f"/api/products/{p['id']}", headers=owner.h).status_code == 204
    o = make_order(client, owner, [(UPC, 1)])
    assert o["lines"][0]["product_id"] is None
    assert [x["id"] for x in client.get("/api/products?show=archived", headers=owner.h).json()["products"]] == [p["id"]]
    # Its barcode is free again.
    assert client.post("/api/products", json={"name": "New", "barcode": UPC}, headers=owner.h).status_code == 201
