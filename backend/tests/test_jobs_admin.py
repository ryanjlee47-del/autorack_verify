"""Scheduled emails (daily summary, alerts, trial and payment notices) and
the operator's cross-warehouse admin API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import JPEG, last_link_token, make_order, scan, scan_event, signup, sync, worker_on_phone
from sqlalchemy import select

from autorack.config import get_settings
from autorack.models import SubscriptionStatus, User, Warehouse, utcnow
from autorack.services import email, jobs

UPC_A = "012345678905"


def to(addr: str) -> list[email.Email]:
    return [m for m in email.outbox if m.to == addr]


def summaries(addr: str) -> list[email.Email]:
    return [m for m in to(addr) if "caught today" in m.subject]


def run(db, now=None):
    db.expire_all()
    return jobs.run_all(db, now)


def local_evening(hour: int = 18) -> datetime:
    # signup() uses America/Chicago. Today, at `hour` local time, in UTC.
    local_now = utcnow().astimezone(ZoneInfo("America/Chicago"))
    return local_now.replace(hour=hour, minute=5, second=0, microsecond=0).astimezone(UTC)


def test_daily_summary_sends_once_after_the_hour_with_money_saved(client, db):
    owner = signup(client)
    client.patch("/api/warehouse", json={"daily_summary_hour": 17, "cost_per_error_cents": 2500}, headers=owner.h)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [(UPC_A, 1)])["id"]
    scan(client, phone, oid, "999999999993")  # a mistake caught
    scan(client, phone, oid, UPC_A)
    email.outbox.clear()

    run(db, local_evening(16))
    assert not to(owner.email)
    run(db, local_evening(18))
    msgs = to(owner.email)
    assert len(msgs) == 1
    assert "1 mistakes caught" in msgs[0].subject and "$25" in msgs[0].subject
    assert "Orders completed: 1" in msgs[0].text
    run(db, local_evening(19))
    assert len(to(owner.email)) == 1  # once per day


def test_daily_summary_respects_opt_outs_and_quiet_days(client, db):
    owner = signup(client)
    run(db, local_evening(20))
    assert not summaries(owner.email)  # nothing happened: no email
    other = signup(client)
    client.patch("/api/auth/preferences", json={"email_daily_summary": False}, headers=other.h)
    phone = worker_on_phone(client, other)
    scan(client, phone, make_order(client, other)["id"], UPC_A)
    run(db, local_evening(20))
    assert not summaries(other.email)


def test_flag_alert_batches_and_never_repeats(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    for reason in ("damaged", "out_of_stock"):
        sync(client, phone, {**scan_event(phone, oid, ""), "kind": "flag", "reason": reason, "scanned_barcode": None})
    email.outbox.clear()
    run(db)
    msgs = to(owner.email)
    assert len(msgs) == 1 and "2 problems flagged" in msgs[0].subject
    assert "Damaged" in msgs[0].text and "Out of stock" in msgs[0].text
    run(db)
    assert len(to(owner.email)) == 1
    client.patch("/api/warehouse", json={"alert_on_flag": False}, headers=owner.h)
    sync(client, phone, {**scan_event(phone, oid, ""), "kind": "flag", "reason": "other", "scanned_barcode": None})
    run(db)
    assert len(to(owner.email)) == 1


def test_error_spike_alert(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner, "Newbie")
    oid = make_order(client, owner, [(UPC_A, 100)])["id"]
    events = [scan_event(phone, oid, UPC_A) for _ in range(16)]
    events += [scan_event(phone, oid, "999999999993") for _ in range(5)]
    sync(client, phone, *events)
    email.outbox.clear()
    run(db)
    msgs = [m for m in to(owner.email) if "error rate" in m.subject]
    assert len(msgs) == 1 and "Newbie" in msgs[0].subject
    run(db)
    assert len([m for m in to(owner.email) if "error rate" in m.subject]) == 1


def set_wh(db, owner, **fields):
    wh = db.get(Warehouse, uuid.UUID(owner.warehouse_id))
    for k, v in fields.items():
        setattr(wh, k, v)
    db.commit()


def test_trial_ending_emails(client, db):
    owner = signup(client)
    set_wh(db, owner, trial_ends_at=utcnow() + timedelta(days=2, hours=12))
    email.outbox.clear()
    run(db)
    assert [m.subject for m in to(owner.email)] == [f"3 days left on your Autorack trial ({'Acme Warehouse'})"]
    run(db)
    assert len(to(owner.email)) == 1
    set_wh(db, owner, trial_ends_at=utcnow() + timedelta(hours=20))
    run(db)
    assert "ends tomorrow" in to(owner.email)[-1].subject
    set_wh(db, owner, trial_ends_at=utcnow() - timedelta(hours=1))
    run(db)
    assert "has ended" in to(owner.email)[-1].subject
    assert "#/billing" in to(owner.email)[-1].text


def test_payment_failed_then_grace_ending(client, db):
    owner = signup(client)
    since = utcnow() - timedelta(hours=1)
    set_wh(db, owner, subscription_status=SubscriptionStatus.past_due, past_due_since=since)
    email.outbox.clear()
    run(db)
    assert "didn't go through" in to(owner.email)[-1].subject
    set_wh(db, owner, past_due_since=utcnow() - timedelta(days=6))
    run(db)
    assert "pauses soon" in to(owner.email)[-1].subject
    run(db)
    assert len(to(owner.email)) == 2


def test_failed_send_is_retried(client, db, monkeypatch):
    owner = signup(client)
    set_wh(db, owner, trial_ends_at=utcnow() + timedelta(days=2))
    email.outbox.clear()
    real = email.send

    def broken(msg):
        raise email.EmailError("smtp down")

    monkeypatch.setattr(email, "send", broken)
    run(db)
    monkeypatch.setattr(email, "send", real)
    run(db)
    assert len(to(owner.email)) == 1


def test_cron_endpoint_needs_secret(client, monkeypatch):
    assert client.post("/api/cron/run").status_code == 404  # no secret configured: route hidden
    monkeypatch.setattr(get_settings(), "cron_secret", "s3cret-cron")
    assert client.post("/api/cron/run", headers={"X-Cron-Secret": "nope"}).status_code == 403
    r = client.post("/api/cron/run", headers={"X-Cron-Secret": "s3cret-cron"})
    assert r.status_code == 200 and "daily_summaries" in r.json()


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------


@pytest.fixture
def operator(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "operator_emails", "ops@autorack.example.com, Boss@Autorack.example.com")
    client.post("/api/auth/magic-link", json={"email": "ops@autorack.example.com"})
    token = client.post("/api/auth/verify", json={"token": last_link_token("ops@autorack.example.com")}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_operator_without_warehouse_can_sign_in(client, operator):
    me = client.get("/api/auth/me", headers=operator).json()
    assert me["is_operator"] is True and me["warehouse"] is None and me["warehouses"] == []
    assert client.get("/api/dashboard/summary", headers=operator).json()["detail"]["code"] == "no_warehouse"


def test_operator_overview_detail_status_and_photos(client, db, operator):
    busy = signup(client, "Busy Co")
    idle = signup(client, "Idle Co")
    phone = worker_on_phone(client, busy)
    oid = make_order(client, busy)["id"]
    flag = {**scan_event(phone, oid, ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    sync(client, phone, scan_event(phone, oid, UPC_A), flag)
    pid = "5b0f6a0e-8f0c-4e2c-9a53-0d9ad1a6f001"
    client.post(
        "/api/worker/photos",
        params={"id": pid, "flag_id": flag["id"]},
        content=JPEG,
        headers={"X-Device-Token": phone.device_token, "Content-Type": "image/jpeg"},
    )
    set_wh(db, idle, trial_ends_at=utcnow() + timedelta(days=3))

    ov = client.get("/api/admin/overview", headers=operator).json()
    assert ov["totals"]["warehouses"] == 2 and ov["totals"]["signups_7d"] == 2
    rows = {r["name"]: r for r in ov["warehouses"]}
    assert rows["Busy Co"]["scans_7d"] == 1 and rows["Busy Co"]["photos"] == 1 and rows["Busy Co"]["health"] == "light"
    assert rows["Idle Co"]["health"] == "not_started"
    assert [r["name"] for r in ov["trials_ending"]] == ["Idle Co"]

    detail = client.get(f"/api/admin/warehouses/{busy.warehouse_id}", headers=operator).json()
    assert detail["photos"][0]["id"] == pid and detail["team"][0]["role"] == "owner"
    assert any(u["feature"] == "floor.scan" for u in detail["usage"])
    img = client.get(f"/api/admin/photos/{pid}", headers=operator)
    assert img.status_code == 200 and img.content == JPEG
    gallery = client.get("/api/admin/photos", headers=operator).json()
    assert gallery[0]["warehouse"] == "Busy Co"

    r = client.post(
        f"/api/admin/warehouses/{idle.warehouse_id}/status", json={"status": "pilot", "note": "LOI"}, headers=operator
    )
    assert r.json()["status"] == "pilot"
    r = client.post(
        f"/api/admin/warehouses/{busy.warehouse_id}/status",
        json={"status": "trialing", "trial_days": 30},
        headers=operator,
    )
    assert r.json()["trial_days_left"] == 30
    audit = client.get("/api/audit", headers=idle.h).json()
    assert audit[0]["action"] == "warehouse.status_set" and audit[0]["actor"] == "ops@autorack.example.com"

    usage = client.get("/api/admin/usage", headers=operator).json()["features"]
    by = {u["feature"]: u for u in usage}
    assert by["floor.scan"]["count"] == 1 and by["floor.photo"]["warehouses"] == 1
    acts = client.get("/api/admin/activity", headers=operator).json()
    assert any(a["action"] == "warehouse.created" for a in acts)


def test_operator_who_also_owns_a_warehouse(client, monkeypatch):
    owner = signup(client, "Mine", addr="boss@autorack.example.com")
    monkeypatch.setattr(get_settings(), "operator_emails", "boss@autorack.example.com")
    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["is_operator"] and me["warehouse"]["name"] == "Mine"
    assert client.get("/api/admin/overview", headers=owner.h).status_code == 200


def test_unknown_emails_still_get_no_account(client, db):
    client.post("/api/auth/magic-link", json={"email": "random@example.com"})
    assert db.scalar(select(User).where(User.email == "random@example.com")) is None
