"""Order entry: manual orders, editing rules, CSV import."""

from __future__ import annotations

from conftest import make_order, scan, signup, worker_on_phone


def upload(client, owner, content: bytes | str, commit: bool = False, name: str = "orders.csv", **form):
    data = content.encode() if isinstance(content, str) else content
    url = "/api/orders/import" if commit else "/api/orders/import/preview"
    return client.post(url, files={"file": (name, data, "text/csv")}, data=form, headers=owner.h)


CSV = (
    "order_number,barcode,quantity,description\n"
    "SO-1,012345678905,2,Widget\n"
    "SO-1,036000291452,1,Tape\n"
    "SO-2,012345678905,1,Widget\n"
)


def test_csv_preview_then_commit(client):
    owner = signup(client)
    p = upload(client, owner, CSV).json()
    assert p["orders_new"] == 2 and p["lines"] == 3 and p["units"] == 4 and p["error_count"] == 0
    assert client.get("/api/orders", headers=owner.h).json()["orders"] == []  # preview wrote nothing
    r = upload(client, owner, CSV, commit=True)
    assert r.status_code == 201 and r.json()["orders_created"] == 2
    orders = client.get("/api/orders", headers=owner.h).json()["orders"]
    assert sorted(o["external_order_number"] for o in orders) == ["SO-1", "SO-2"]
    assert all(o["source"] == "csv" for o in orders)


def test_reimport_skips_existing_orders(client):
    owner = signup(client)
    upload(client, owner, CSV, commit=True)
    p = upload(client, owner, CSV).json()
    assert p["orders_new"] == 0 and p["orders_existing"] == ["SO-1", "SO-2"]
    r = upload(client, owner, CSV, commit=True).json()
    assert r["orders_created"] == 0 and r["orders_skipped"] == 2


def test_header_synonyms_bom_and_semicolons(client):
    owner = signup(client)
    content = "﻿Order #;UPC;Qty;Item Description;Bin\r\nA1;012345678905;3;Widget;A-01\r\n".encode()
    p = upload(client, owner, content).json()
    assert p["columns"] == {
        "order_number": "Order #",
        "barcode": "UPC",
        "quantity": "Qty",
        "description": "Item Description",
        "location": "Bin",
    }
    assert p["sample"][0]["lines"][0]["location"] == "A-01"


def test_windows_1252_file(client):
    owner = signup(client)
    content = "order_number,barcode,description\nA1,012345678905,Caf\xe9 mug\n".encode("cp1252")
    p = upload(client, owner, content).json()
    assert p["sample"][0]["lines"][0]["description"] == "Café mug"
    assert any("no quantity" in w.lower() for w in p["warnings"])


def test_scientific_notation_barcodes_are_refused(client):
    owner = signup(client)
    p = upload(client, owner, "order_number,barcode,quantity\nA1,1.23457E+11,1\n").json()
    assert p["error_count"] == 1 and "scientific notation" in p["errors"][0]["message"]
    r = upload(client, owner, "order_number,barcode,quantity\nA1,1.23457E+11,1\n", commit=True)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "import_has_errors"


def test_skip_invalid_rows(client):
    owner = signup(client)
    content = "order_number,barcode,quantity\nA1,012345678905,1\nA2,,1\nA3,036000291452,zero\n"
    r = upload(client, owner, content, commit=True, skip_invalid_rows="true")
    assert r.status_code == 201 and r.json()["orders_created"] == 1


def test_duplicate_rows_merge_into_one_line(client):
    owner = signup(client)
    content = "order_number,barcode,quantity\nA1,012345678905,1\nA1, 012345678905 ,2\n"
    p = upload(client, owner, content).json()
    assert p["lines"] == 1 and p["units"] == 3
    assert any("merged" in w for w in p["warnings"])


def test_missing_required_columns(client):
    owner = signup(client)
    r = upload(client, owner, "foo,bar\n1,2\n")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "columns_missing"


def test_template_download(client):
    owner = signup(client)
    r = client.get("/api/orders/template.csv", headers=owner.h)
    assert r.status_code == 200 and r.text.startswith("order_number,barcode,quantity,description")
    assert upload(client, owner, r.text).json()["orders_new"] == 2


def test_manual_order_number_unique_until_cancelled(client):
    owner = signup(client)
    first = make_order(client, owner, number="X-1")
    r = client.post("/api/orders", json={"external_order_number": "X-1", "lines": [{"barcode": "1"}]}, headers=owner.h)
    assert r.status_code == 409
    client.post(f"/api/orders/{first['id']}/cancel", headers=owner.h)
    make_order(client, owner, number="X-1")


def test_line_editing_rules(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [("012345678905", 2), ("036000291452", 1)])
    oid = order["id"]
    scanned_line, other = order["lines"]
    scan(client, phone, oid, "012345678905")

    # Barcode of a scanned line is frozen; quantity isn't.
    r = client.patch(f"/api/orders/{oid}/lines/{scanned_line['id']}", json={"barcode": "999"}, headers=owner.h)
    assert r.status_code == 409
    r = client.patch(f"/api/orders/{oid}/lines/{scanned_line['id']}", json={"quantity": 1}, headers=owner.h)
    assert r.status_code == 200
    # Scanned lines can't be deleted; unscanned ones can.
    assert client.delete(f"/api/orders/{oid}/lines/{scanned_line['id']}", headers=owner.h).status_code == 409
    r = client.delete(f"/api/orders/{oid}/lines/{other['id']}", headers=owner.h)
    assert r.status_code == 200
    # Removing the only remaining unscanned line completed the order.
    assert r.json()["status"] == "completed"
    # Duplicate barcodes are refused.
    r = client.post(f"/api/orders/{oid}/lines", json={"barcode": " 012345678905"}, headers=owner.h)
    assert r.status_code == 409


def test_editing_bumps_version_for_offline_phones(client):
    owner = signup(client)
    order = make_order(client, owner)
    v1 = order["version"]
    r = client.post(f"/api/orders/{order['id']}/lines", json={"barcode": "NEW-1", "quantity": 2}, headers=owner.h)
    assert r.json()["version"] > v1


def test_order_search_and_filters(client):
    owner = signup(client)
    make_order(client, owner, [("012345678905", 1)], number="ALPHA-1")
    make_order(client, owner, [("036000291452", 1)], number="BETA-2")
    r = client.get("/api/orders", params={"q": "alpha"}, headers=owner.h).json()["orders"]
    assert [o["external_order_number"] for o in r] == ["ALPHA-1"]
    r = client.get("/api/orders", params={"q": "036000"}, headers=owner.h).json()["orders"]
    assert [o["external_order_number"] for o in r] == ["BETA-2"]
    assert len(client.get("/api/orders", params={"status": "open"}, headers=owner.h).json()["orders"]) == 2
    assert client.get("/api/orders", params={"status": "completed"}, headers=owner.h).json()["orders"] == []


def test_pick_sheets_sorted_by_location_with_qr(client):
    owner = signup(client)
    r = client.post(
        "/api/orders",
        json={
            "external_order_number": "PS-1",
            "lines": [
                {"barcode": "B", "location": "C-01"},
                {"barcode": "A", "location": "A-01"},
                {"barcode": "N"},
            ],
        },
        headers=owner.h,
    ).json()
    sheets = client.get("/api/orders/pick-sheets", params={"ids": r["id"]}, headers=owner.h).json()
    assert [li["location"] for li in sheets[0]["lines"]] == ["A-01", "C-01", None]
    assert sheets[0]["qr_svg"].startswith("<svg")
