"""Practice rounds, confused pairs, and the 3PL client accuracy report."""

from __future__ import annotations

import io
from datetime import datetime, timedelta

from conftest import make_order, scan, scan_event, signup, sync, worker_on_phone
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import func, select
from test_client_portal import UPC, client_order, portal_login, ship

from autorack.models import Order, ScanEvent, TrainingRun, utcnow
from autorack.services import client_report, email

# ---------------------------------------------------------------------------
# Practice
# ---------------------------------------------------------------------------


def test_practice_order_uses_real_products_and_stores_nothing(client, db):
    owner = signup(client)
    for i, (code, loc) in enumerate([("012345678905", "A-01"), ("036000291452", "A-02"), ("042100005264", "B-01")]):
        client.post(
            "/api/products", json={"name": f"Item {i}", "barcode": code, "location": loc}, headers=owner.h
        ).raise_for_status()
    phone = worker_on_phone(client, owner)
    before = db.scalar(select(func.count()).select_from(Order))
    r = client.post("/api/worker/practice", headers=phone.h)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["practice"] is True and p["id"].startswith("practice-")
    assert {li["expected_barcode"] for li in p["lines"]} <= {"012345678905", "036000291452", "042100005264"}
    assert len(p["lines"]) == 3 and all(1 <= li["expected_quantity"] <= 3 for li in p["lines"])
    assert p["match"]["index"]  # the phone can check scans on its own
    assert db.scalar(select(func.count()).select_from(Order)) == before  # nothing saved
    # Only the summary comes back, and the Workers page shows it.
    r = client.post(
        "/api/worker/practice/result", json={"units": 5, "scans": 6, "mistakes": 1, "seconds": 75}, headers=phone.h
    )
    assert r.status_code == 201 and abs(r.json()["accuracy"] - 5 / 6) < 1e-9
    assert db.scalar(select(func.count()).select_from(ScanEvent)) == 0
    (w,) = client.get("/api/workers", headers=owner.h).json()["workers"]
    assert w["practice"]["rounds"] == 1 and abs(w["practice"]["last_accuracy"] - 5 / 6) < 1e-9


def test_practice_without_a_catalog_uses_sample_barcodes(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    p = client.post("/api/worker/practice", headers=phone.h).json()
    assert len(p["lines"]) == 4
    assert client.post("/api/worker/practice", headers={"X-Device-Token": phone.device_token}).status_code == 401
    bad = {"units": -1, "scans": 0, "mistakes": 0, "seconds": 0}
    assert client.post("/api/worker/practice/result", json=bad, headers=phone.h).status_code == 422
    assert db.scalar(select(func.count()).select_from(TrainingRun)) == 0


# ---------------------------------------------------------------------------
# Confused pairs
# ---------------------------------------------------------------------------


def _product(client, owner, name, code, loc):
    client.post(
        "/api/products", json={"name": name, "barcode": code, "location": loc}, headers=owner.h
    ).raise_for_status()


def _wrong(client, phone, order, wanted_line, barcode):
    ev = scan_event(phone, order["id"], barcode, intended_line_item_id=wanted_line)
    assert sync(client, phone, ev)["events"][0]["result"] == "mismatch"


def test_confused_pairs_name_both_items_their_bins_and_a_fix(client):
    owner = signup(client)
    _product(client, owner, "Blue mug 12oz", "012345678905", "A-03-2")
    _product(client, owner, "Navy mug 12oz", "036000291452", "A-03-3")
    _product(client, owner, "Tape", "042100005264", "F-01-1")
    phone = worker_on_phone(client, owner)
    o1 = make_order(client, owner, [("012345678905", 3)])
    o2 = make_order(client, owner, [("036000291452", 3)])
    _wrong(client, phone, o1, o1["lines"][0]["id"], "036000291452")
    _wrong(client, phone, o1, o1["lines"][0]["id"], "036000291452")
    _wrong(client, phone, o2, o2["lines"][0]["id"], "012345678905")  # the other way round: same pair
    _wrong(client, phone, o1, o1["lines"][0]["id"], "9999999999994")  # not in the catalog
    pairs = client.get("/api/dashboard/confused-pairs?days=7", headers=owner.h).json()["pairs"]
    top = pairs[0]
    assert top["times"] == 3 and top["orders"] == 2 and top["cause"] == "neighbours"
    assert {top["wanted"]["name"], top["scanned"]["name"]} == {"Blue mug 12oz", "Navy mug 12oz"}
    assert "A-03-2" in top["advice"] and "A-03-3" in top["advice"]
    unknown = pairs[1]
    assert unknown["cause"] == "unknown_barcode" and unknown["scanned"]["known"] is False


# ---------------------------------------------------------------------------
# Client accuracy report
# ---------------------------------------------------------------------------


def _shipped_client_month(client, owner):
    glow = client.post("/api/clients", json={"name": "Glow Skincare"}, headers=owner.h).json()
    phone = worker_on_phone(client, owner)
    for n in range(2):
        o = client_order(client, owner, glow["id"], f"GLOW-{n}", qty=2)
        scan(client, phone, o["id"], UPC)
        if n == 0:
            scan(client, phone, o["id"], "036000291452")  # a wrong item, caught
        scan(client, phone, o["id"], UPC)
        ship(client, phone, o["id"], f"1Z999AA1012345678{n}")
    return glow


def test_accuracy_report_numbers_pdf_and_portal(client):
    owner = signup(client)
    glow = _shipped_client_month(client, owner)
    month = utcnow().strftime("%Y-%m")
    r = client.get(f"/api/clients/{glow['id']}/accuracy.pdf?month={month}", headers=owner.h)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(r.content)).pages)
    assert "Glow Skincare" in text and "80.0% picked right the first time" in text  # 4 right, 1 caught
    assert "Verified with Autorack" in text

    portal = portal_login(client, owner, glow["id"])
    rep = client.get(f"/api/portal/report?month={month}", headers=portal).json()
    assert rep["orders_shipped"] == 2 and rep["wrong_items_caught"] == 1 and abs(rep["accuracy"] - 0.8) < 1e-9
    assert client.get(f"/api/portal/accuracy.pdf?month={month}", headers=portal).content.startswith(b"%PDF")
    # The owner's dashboard isn't the client's to open.
    assert client.get(f"/api/clients/{glow['id']}/accuracy.pdf", headers=portal).status_code == 403


def test_logo_shows_on_the_portal_and_the_report(client):
    owner = signup(client)
    glow = _shipped_client_month(client, owner)
    buf = io.BytesIO()
    Image.new("RGBA", (400, 100), (20, 60, 200, 255)).save(buf, "PNG")
    r = client.post("/api/warehouse/logo", content=buf.getvalue(), headers={**owner.h, "Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    assert client.post("/api/warehouse/logo", content=b"not an image", headers=owner.h).status_code == 400
    portal = portal_login(client, owner, glow["id"])
    assert client.get("/api/portal/me", headers=portal).json()["warehouse"]["has_logo"] is True
    assert client.get("/api/portal/logo", headers=portal).content.startswith(b"\x89PNG")
    pdf = client.get(f"/api/clients/{glow['id']}/accuracy.pdf", headers=owner.h).content
    assert b"/Subtype /Image" in pdf
    assert client.delete("/api/warehouse/logo", headers=owner.h).status_code == 204
    assert client.get("/api/portal/logo", headers=portal).status_code == 404


def test_monthly_email_to_the_clients_portal_logins(client, db, monkeypatch):
    owner = signup(client)
    glow = _shipped_client_month(client, owner)
    portal_login(client, owner, glow["id"], addr="buyer@glow.example")
    # Pretend it's the 1st of next month, 9am.
    now = utcnow()
    first = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=15, minute=0)
    email.outbox.clear()
    assert client_report.run_client_reports(db, first) == 1
    (msg,) = [m for m in email.outbox if "accuracy report" in m.subject]
    assert msg.to == "buyer@glow.example" and msg.reply_to == owner.email
    assert msg.attachments and msg.attachments[0].content.startswith(b"%PDF")
    assert client_report.run_client_reports(db, first) == 0  # once a month
    # Switched off: nothing next time.
    client.patch(f"/api/clients/{glow['id']}", json={"monthly_report": False}, headers=owner.h)
    later = (first + timedelta(days=32)).replace(day=1, hour=15)
    email.outbox.clear()
    assert client_report.run_client_reports(db, later) == 0
    assert isinstance(first, datetime)
