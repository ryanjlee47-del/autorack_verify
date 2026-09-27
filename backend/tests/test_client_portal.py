"""3PL clients: portal logins that see only their own orders, per-client
billing statements, and the claim/evidence pack PDF."""

from __future__ import annotations

import io
import uuid

from conftest import JPEG, bearer, google_login, make_order, scan, scan_event, signup, sync, worker_on_phone
from pypdf import PdfReader

from autorack.services import email

UPC = "012345678905"


def client_order(client, owner, client_id, number, qty=1):
    r = client.post(
        "/api/orders",
        json={"external_order_number": number, "client_id": client_id, "lines": [{"barcode": UPC, "quantity": qty}]},
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def ship(client, phone, order_id, *trackings):
    for i, t in enumerate(trackings):
        ev = {**scan_event(phone, order_id, ""), "kind": "ship", "scanned_barcode": None, "tracking_number": t}
        ev["final"] = i == len(trackings) - 1
        out = sync(client, phone, ev)
        assert out["events"][0]["status"] == "applied", out


def pack_photo(client, phone, order_id):
    pid = str(uuid.uuid4())
    r = client.post(
        "/api/worker/photos",
        params={"id": pid, "order_id": order_id, "kind": "pack"},
        content=JPEG,
        headers={**phone.h, "Content-Type": "image/jpeg"},
    )
    assert r.status_code == 201, r.text
    return pid


def portal_login(client, owner, client_id, addr="buyer@glow.example"):
    email.outbox.clear()
    r = client.post(f"/api/clients/{client_id}/users", json={"email": addr, "name": "Sam"}, headers=owner.h)
    assert r.status_code == 201, r.text
    assert email.outbox and "Sign in with Google" in email.outbox[-1].text
    return bearer(google_login(client, addr))


def test_client_sees_only_its_own_orders(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    glow = client.post("/api/clients", json={"name": "Glow Skincare"}, headers=owner.h).json()
    other = client.post("/api/clients", json={"name": "Other Brand"}, headers=owner.h).json()
    mine = client_order(client, owner, glow["id"], "GLOW-1")
    theirs = client_order(client, owner, other["id"], "OTHER-1")
    house = make_order(client, owner, number="HOUSE-1")
    scan(client, phone, mine["id"], UPC)
    mine_photo = pack_photo(client, phone, mine["id"])
    ship(client, phone, mine["id"], "1Z999AA10123456784")
    scan(client, phone, theirs["id"], UPC)
    their_photo = pack_photo(client, phone, theirs["id"])
    flag = {**scan_event(phone, mine["id"], ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}

    h = portal_login(client, owner, glow["id"])
    me = client.get("/api/auth/me", headers=h).json()
    assert me["membership"]["role"] == "client" and me["client"]["name"] == "Glow Skincare"
    # Not the dashboard.
    for url in ("/api/orders", "/api/dashboard/summary", "/api/workers", f"/api/orders/{mine['id']}"):
        r = client.get(url, headers=h)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "client_portal_only", url
    assert client.get("/api/portal/me", headers=h).json()["client"]["name"] == "Glow Skincare"
    listed = client.get("/api/portal/orders", headers=h).json()["orders"]
    assert [o["number"] for o in listed] == ["GLOW-1"]
    detail = client.get(f"/api/portal/orders/{mine['id']}", headers=h).json()
    assert detail["status"] == "shipped" and detail["boxes"][0]["tracking_number"] == "1Z999AA10123456784"
    assert "worker" not in str(detail["units"]) and detail["pack_photos"] == [mine_photo]
    for oid in (theirs["id"], house["id"]):
        assert client.get(f"/api/portal/orders/{oid}", headers=h).status_code == 404
        assert client.get(f"/api/portal/orders/{oid}/claim.pdf", headers=h).status_code == 404
    assert client.get(f"/api/portal/photos/{mine_photo}", headers=h).content == JPEG
    assert client.get(f"/api/portal/photos/{their_photo}", headers=h).status_code == 404
    # Problem photos stay internal even on the client's own orders.
    sync(client, phone, flag)
    prob = str(uuid.uuid4())
    client.post(
        "/api/worker/photos",
        params={"id": prob, "flag_id": flag["id"]},
        content=JPEG,
        headers={**phone.h, "Content-Type": "image/jpeg"},
    )
    assert client.get(f"/api/portal/photos/{prob}", headers=h).status_code == 404
    # Staff can't use the portal.
    assert client.get("/api/portal/orders", headers=owner.h).json()["detail"]["code"] == "not_a_client"
    # Portal logins aren't on the team list, and the team can't invite a "client".
    assert all(u["role"] != "client" for u in client.get("/api/team", headers=owner.h).json())
    assert client.post("/api/team", json={"email": "x@y.com", "role": "client"}, headers=owner.h).status_code == 422
    # Removing the login cuts access.
    uid = client.get(f"/api/clients/{glow['id']}/users", headers=owner.h).json()[0]["id"]
    assert client.delete(f"/api/clients/{glow['id']}/users/{uid}", headers=owner.h).status_code == 204
    assert client.get("/api/portal/orders", headers=h).status_code == 403


def test_claim_pack_pdf(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner, "Maria")
    glow = client.post("/api/clients", json={"name": "Glow"}, headers=owner.h).json()
    o = client_order(client, owner, glow["id"], "GLOW-9", qty=2)
    scan(client, phone, o["id"], "036000291452")  # wrong item, caught
    scan(client, phone, o["id"], UPC, lot="L-77")
    scan(client, phone, o["id"], UPC, lot="L-77")
    pack_photo(client, phone, o["id"])
    ship(client, phone, o["id"], "1Z999AA10123456784", "9400111899223197428490")
    r = client.get(f"/api/orders/{o['id']}/claim.pdf", headers=owner.h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    for want in ("GLOW-9", "1Z999AA10123456784", "9400111899223197428490", "Lot L-77", "Maria", "caught"):
        assert want in text, want
    h = portal_login(client, owner, glow["id"])
    r = client.get(f"/api/portal/orders/{o['id']}/claim.pdf", headers=h)
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "GLOW-9" in text and "Maria" not in text  # the client's copy leaves out who picked it


def test_client_billing_statement(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    glow = client.post("/api/clients", json={"name": "Glow", "code": "GLOW"}, headers=owner.h).json()
    r = client.patch(
        f"/api/clients/{glow['id']}",
        json={"rates": {"monthly_fee": 5000, "per_order": 250, "per_unit": 30, "per_extra_box": 100, "per_insert": 15}},
        headers=owner.h,
    )
    assert r.json()["rates"]["per_order"] == 250
    assert client.patch(f"/api/clients/{glow['id']}", json={"rates": {"bogus": 1}}, headers=owner.h).status_code == 400
    client.post("/api/inserts", json={"name": "Glow card", "client_id": glow["id"]}, headers=owner.h)
    a = client_order(client, owner, glow["id"], "G-1", qty=3)
    b = client_order(client, owner, glow["id"], "G-2", qty=1)
    make_order(client, owner, number="NOT-GLOW")
    for o, n in ((a, 3), (b, 1)):
        for _ in range(n):
            scan(client, phone, o["id"], UPC)
        ins = client.get(f"/api/worker/orders/{o['id']}", headers=phone.h).json()["inserts"][0]["id"]
        sync(
            client,
            phone,
            {**scan_event(phone, o["id"], ""), "kind": "insert", "scanned_barcode": None, "insert_id": ins},
        )
    ship(client, phone, a["id"], "1Z999AA10123456784", "9400111899223197428490")
    ship(client, phone, b["id"], "1Z999AA10123456791")
    st = client.get(f"/api/clients/{glow['id']}/statement", headers=owner.h).json()
    amounts = {line["key"]: (line["quantity"], line["amount_cents"]) for line in st["lines"]}
    assert amounts["monthly_fee"] == (1, 5000)
    assert amounts["per_order"] == (2, 500)
    assert amounts["per_unit"] == (4, 120)
    assert amounts["per_extra_box"] == (1, 100)
    assert amounts["per_insert"] == (2, 30)
    assert st["total_cents"] == 5750
    csv = client.get(f"/api/clients/{glow['id']}/statement.csv", headers=owner.h).text
    assert "Total" in csv and "57.50" in csv
    summary = client.get("/api/billing/clients", headers=owner.h).json()
    assert summary["clients"][0]["total_cents"] == 5750
    # The client sees the same statement, and its month in numbers.
    h = portal_login(client, owner, glow["id"])
    assert client.get("/api/portal/statement", headers=h).json()["total_cents"] == 5750
    report = client.get("/api/portal/report", headers=h).json()
    assert (report["orders_shipped"], report["units_shipped"]) == (2, 4)
    assert client.get("/api/portal/report?month=nope", headers=h).status_code == 400
