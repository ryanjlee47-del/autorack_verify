"""Shareable proof-of-shipment links, and the monthly PDF report."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from conftest import make_order, scan, signup, sync, worker_on_phone
from test_floor_features import ship_event

from autorack.models import utcnow
from autorack.services import email, monthly


def shipped_order(client, owner, phone, number="SO-P1"):
    o = make_order(client, owner, [("012345678905", 2)], number=number)
    scan(client, phone, o["id"], "099999999993")  # a mistake caught
    scan(client, phone, o["id"], "012345678905")
    scan(client, phone, o["id"], "012345678905", serial="SN-1")
    sync(client, phone, ship_event(phone, o["id"], "1Z999AA10123456784"))
    return o


def test_share_proof_link(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner, "Secret Worker Name")
    open_order = make_order(client, owner)
    assert client.post(f"/api/orders/{open_order['id']}/share", headers=owner.h).status_code == 400

    o = shipped_order(client, owner, phone)
    r = client.post(f"/api/orders/{o['id']}/share", headers=owner.h)
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://app.autorack.test/proof.html#t=")
    token = url.split("#t=")[1]
    # Same link on repeat; shown on the order.
    assert client.post(f"/api/orders/{o['id']}/share", headers=owner.h).json()["url"] == url
    assert client.get(f"/api/orders/{o['id']}", headers=owner.h).json()["share_url"] == url

    p = client.get(f"/api/public/proof/{token}")  # no auth
    assert p.status_code == 200
    body = p.json()
    assert body["order_number"] == "SO-P1" and body["tracking_number"] == "1Z999AA10123456784"
    assert body["errors_caught"] == 1 and len(body["units"]) == 2
    assert body["lines"][0]["verified"] == 2
    assert "Secret Worker Name" not in p.text  # who picked it stays private
    assert "worker" not in p.text

    assert client.delete(f"/api/orders/{o['id']}/share", headers=owner.h).status_code == 204
    assert client.get(f"/api/public/proof/{token}").status_code == 404
    assert client.get("/api/public/proof/short").status_code == 404
    assert client.get(f"/api/public/proof/{'x' * 32}").status_code == 404


def test_monthly_pdf_download(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    shipped_order(client, owner, phone)
    this_month = utcnow().astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m")
    r = client.get(f"/api/reports/monthly.pdf?month={this_month}", headers=owner.h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and len(r.content) > 2000
    assert f'filename="autorack-{this_month}.pdf"' in r.headers["content-disposition"]
    assert client.get("/api/reports/monthly.pdf", headers=owner.h).status_code == 200  # last month, empty is fine
    assert client.get("/api/reports/monthly.pdf?month=nope", headers=owner.h).status_code == 400
    assert client.get("/api/reports/monthly.pdf?month=2099-01", headers=owner.h).status_code == 400


def test_monthly_email_on_the_first(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    shipped_order(client, owner, phone)
    quiet = signup(client, "Quiet Warehouse")  # no activity: no email
    now_local = utcnow().astimezone(ZoneInfo("America/Chicago"))
    y, m = (now_local.year + (now_local.month == 12), now_local.month % 12 + 1)
    first_7am = datetime(y, m, 1, 7, 0, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)
    first_9am = datetime(y, m, 1, 9, 0, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)

    email.outbox.clear()
    assert monthly.run_monthly_reports(db, first_7am) == 0
    assert monthly.run_monthly_reports(db, first_9am) == 1
    assert monthly.run_monthly_reports(db, first_9am) == 0  # once per month
    (msg,) = [x for x in email.outbox if "Autorack report" in x.subject]
    assert msg.to == owner.email
    assert "caught 1 mistake" in msg.text
    (att,) = msg.attachments
    assert att.filename == f"autorack-{now_local.year}-{now_local.month:02d}.pdf" and att.content.startswith(b"%PDF")
    assert not any(x.to == quiet.email for x in email.outbox)


def test_monthly_email_can_be_turned_off(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    shipped_order(client, owner, phone)
    r = client.patch("/api/warehouse", json={"monthly_report_enabled": False}, headers=owner.h)
    assert r.json()["monthly_report_enabled"] is False
    now_local = utcnow().astimezone(ZoneInfo("America/Chicago"))
    y, m = (now_local.year + (now_local.month == 12), now_local.month % 12 + 1)
    email.outbox.clear()
    at = datetime(y, m, 1, 10, 0, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)
    assert monthly.run_monthly_reports(db, at) == 0
    assert email.outbox == []


def test_month_helpers():
    from datetime import date

    assert monthly.month_range(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
    assert monthly.month_range(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))
    assert monthly.previous_month(date(2026, 1, 1)) == (2025, 12)
    assert monthly.parse_month("2026-07", date(2026, 9, 1)) == (2026, 7)
    assert monthly.parse_month(None, date(2026, 9, 1)) == (2026, 8)
