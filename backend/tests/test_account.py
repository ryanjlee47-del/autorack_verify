"""Account lifecycle: export everything, close, reopen, delete; operator
notices; the worker privacy notice."""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from datetime import timedelta

import pytest
from conftest import (
    AGREEMENT,
    JPEG,
    Owner,
    add_worker,
    last_link_token,
    link_phone,
    make_order,
    scan,
    scan_event,
    signup,
    sync,
    worker_on_phone,
)
from sqlalchemy import func, select

from autorack.config import get_settings
from autorack.models import (
    AgreementSignature,
    AuditLog,
    Membership,
    Order,
    ScanEvent,
    User,
    Warehouse,
    Worker,
    utcnow,
)
from autorack.services import billing, email, jobs

UPC = "012345678905"


def busy_warehouse(client) -> tuple[Owner, str]:
    owner = signup(client, "Harbor DC")
    phone = worker_on_phone(client, owner, "Maria")
    order = client.post(
        "/api/orders",
        json={"external_order_number": "=SO-1", "customer": "Acme", "lines": [{"barcode": UPC, "quantity": 2}]},
        headers=owner.h,
    ).json()
    scan(client, phone, order["id"], UPC)
    scan(client, phone, order["id"], "999999999993")
    flag = {**scan_event(phone, order["id"], ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    sync(client, phone, flag)
    client.post(
        "/api/worker/photos",
        params={"id": str(uuid.uuid4()), "flag_id": flag["id"]},
        content=JPEG,
        headers={"X-Device-Token": phone.device_token, "Content-Type": "image/jpeg"},
    )
    return owner, order["id"]


def read_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8-sig"))))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_contains_everything_and_no_pins(client):
    owner, _ = busy_warehouse(client)
    r = client.get("/api/account/export.zip", headers=owner.h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "autorack-export-Harbor-DC" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    for expected in (
        "README.txt",
        "warehouse.json",
        "orders.csv",
        "order_lines.csv",
        "scans.csv",
        "problems.csv",
        "workers.csv",
        "phones.csv",
        "team.csv",
        "barcode_aliases.csv",
        "imports.csv",
        "activity_log.csv",
    ):
        assert expected in names, expected
    assert any(n.startswith("photos/") and n.endswith(".jpg") for n in names)
    assert any(n.startswith("license-agreement/") and n.endswith(".pdf") for n in names)
    orders = read_csv(zf, "orders.csv")
    assert orders[0]["order_number"] == "'=SO-1"  # spreadsheet formula neutralised
    assert orders[0]["customer"] == "Acme"
    assert {s["result"] for s in read_csv(zf, "scans.csv")} == {"match", "mismatch"}
    workers = read_csv(zf, "workers.csv")
    assert workers[0]["name"] == "Maria" and "pin" not in ",".join(workers[0]).lower()
    assert b"pin_hash" not in r.content and b"pin_fingerprint" not in r.content
    assert "account.exported" in client.get("/api/audit", headers=owner.h).text


def test_only_owners_export(client):
    owner, _ = busy_warehouse(client)
    client.post("/api/team", json={"email": "mgr@example.com", "role": "manager"}, headers=owner.h)
    token = client.post("/api/auth/verify", json={"token": last_link_token("mgr@example.com")}).json()["token"]
    r = client.get("/api/account/export.zip", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Close, reopen
# ---------------------------------------------------------------------------


def test_close_stops_scanning_keeps_data_and_can_reopen(client, db):
    owner, _ = busy_warehouse(client)
    r = client.post("/api/account/close", json={"confirm_name": "wrong"}, headers=owner.h)
    assert r.json()["detail"]["code"] == "confirm_name_mismatch"
    email.outbox.clear()
    r = client.post("/api/account/close", json={"confirm_name": "harbor dc", "reason": "Season over"}, headers=owner.h)
    assert r.status_code == 200
    state = r.json()
    assert state["closed_at"] and state["deletion_due_at"]
    msgs = [m for m in email.outbox if m.to == owner.email]
    assert "is closed" in msgs[0].subject and "Download" in msgs[0].text

    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["access"]["state"] == "closed" and me["warehouse"]["closed_at"]
    # History still readable and exportable; new work refused.
    assert client.get("/api/orders", headers=owner.h).status_code == 200
    assert client.get("/api/account/export.zip", headers=owner.h).status_code == 200
    assert client.post("/api/orders", json={"lines": [{"barcode": "1"}]}, headers=owner.h).status_code == 402
    # Every worker was signed out.
    from autorack.models import WorkerSession

    assert db.scalar(select(func.count()).select_from(WorkerSession).where(WorkerSession.ended_at.is_(None))) == 0
    r = client.post("/api/account/reopen", headers=owner.h)
    assert r.json()["closed_at"] is None and r.json()["deletion_due_at"] is None
    assert client.get("/api/auth/me", headers=owner.h).json()["access"]["allowed"] is True


def test_closing_a_closed_account_worker_login_is_refused(client):
    owner = signup(client)
    w = add_worker(client, owner, "Devon")
    phone = link_phone(client, owner)
    client.post("/api/account/close", json={"confirm_name": "Acme Warehouse"}, headers=owner.h)
    r = client.post("/api/worker/login", json={"pin": w["pin"]}, headers=phone.h)
    assert r.status_code == 402


class CancelGateway:
    def __init__(self, fail: bool = False) -> None:
        self.cancelled: list[str] = []
        self.fail = fail

    def cancel_subscription(self, sub_id):
        if self.fail:
            raise RuntimeError("stripe down")
        self.cancelled.append(sub_id)
        return {"id": sub_id, "status": "canceled"}


def paying(db, owner: Owner) -> Warehouse:
    wh = db.get(Warehouse, uuid.UUID(owner.warehouse_id))
    wh.subscription_status = "active"
    wh.stripe_subscription_id = "sub_123"
    db.commit()
    return wh


def test_close_cancels_stripe_or_refuses(client, db, monkeypatch):
    owner = signup(client)
    paying(db, owner)
    monkeypatch.setattr(billing, "gateway", CancelGateway(fail=True))
    r = client.post("/api/account/close", json={"confirm_name": "Acme Warehouse"}, headers=owner.h)
    assert r.status_code == 502
    db.expire_all()
    assert db.get(Warehouse, uuid.UUID(owner.warehouse_id)).closed_at is None
    gw = CancelGateway()
    monkeypatch.setattr(billing, "gateway", gw)
    r = client.post("/api/account/close", json={"confirm_name": "Acme Warehouse"}, headers=owner.h)
    assert r.status_code == 200 and gw.cancelled == ["sub_123"]
    db.expire_all()
    assert db.get(Warehouse, uuid.UUID(owner.warehouse_id)).subscription_status.value == "canceled"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def run_jobs(db, now=None):
    db.expire_all()
    out = jobs.run_all(db, now)
    assert "skipped" not in out
    return out


def test_reminder_then_deletion_keeps_only_tombstone_and_signature(client, db):
    owner, _ = busy_warehouse(client)
    client.post("/api/account/close", json={"confirm_name": "Harbor DC"}, headers=owner.h)
    wid = uuid.UUID(owner.warehouse_id)
    due = db.get(Warehouse, wid).deletion_due_at
    assert (due - utcnow()).days in (44, 45)

    email.outbox.clear()
    run_jobs(db, due - timedelta(days=6))
    reminders = [m for m in email.outbox if "will be deleted" in m.subject]
    assert len(reminders) == 1 and reminders[0].to == owner.email
    run_jobs(db, due - timedelta(days=5))
    assert len([m for m in email.outbox if "will be deleted" in m.subject]) == 1  # once

    out = run_jobs(db, due + timedelta(minutes=1))
    assert out["account_deletions"] == 1
    db.expire_all()
    wh = db.get(Warehouse, wid)
    assert wh.purged_at and wh.name.startswith("Deleted warehouse") and wh.owner_email == ""
    for model in (Order, ScanEvent, Worker, Membership):
        assert db.scalar(select(func.count()).select_from(model).where(model.warehouse_id == wid)) == 0, model
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.warehouse_id == wid)) == 0
    assert (
        db.scalar(select(func.count()).select_from(AgreementSignature).where(AgreementSignature.warehouse_id == wid))
        == 1
    )
    # The signer is still named on the agreement, so kept but disabled.
    user = db.scalar(select(User).where(User.email == owner.email))
    assert user is not None and user.active is False
    assert client.get("/api/auth/me", headers=owner.h).status_code == 401
    # Nothing to reopen.
    assert "account.purged" in {a.action for a in db.scalars(select(AuditLog))}


def test_deletion_leaves_a_members_other_warehouses_alone(client, db):
    owner = signup(client, "North")
    client.post("/api/auth/warehouses", json={"name": "South"}, headers=owner.h)
    client.post("/api/agreement/sign", json=AGREEMENT, headers=owner.h)
    client.post("/api/auth/switch", json={"warehouse_id": owner.warehouse_id}, headers=owner.h)
    client.post("/api/account/close", json={"confirm_name": "North"}, headers=owner.h)
    wh = db.get(Warehouse, uuid.UUID(owner.warehouse_id))
    run_jobs(db, wh.deletion_due_at + timedelta(seconds=1))
    me = client.get("/api/auth/me", headers=owner.h).json()
    assert [w["name"] for w in me["warehouses"]] == ["South"]
    assert me["warehouse"]["name"] == "South"


def test_operator_purge_needs_confirmation_and_a_closed_account(client, db, monkeypatch):
    owner, _ = busy_warehouse(client)
    ops = operator(client, monkeypatch)
    url = f"/api/admin/warehouses/{owner.warehouse_id}"
    assert client.post(f"{url}/purge", json={"confirm": "DELETE"}, headers=ops).status_code == 400  # not closed
    client.post(f"{url}/close", json={"reason": "Non-payment"}, headers=ops)
    assert client.get("/api/auth/me", headers=owner.h).json()["access"]["state"] == "closed"
    assert client.get(f"{url}/export.zip", headers=ops).status_code == 200
    assert client.post(f"{url}/purge", json={"confirm": "delete"}, headers=ops).status_code == 400
    r = client.post(f"{url}/purge", json={"confirm": "DELETE"}, headers=ops)
    assert r.status_code == 200 and r.json()["deleted"]["scans"] == 2


def test_append_only_tables_still_refuse_deletes_outside_a_purge(client, db):
    busy_warehouse(client)
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM scan_events"))
    db.rollback()


# ---------------------------------------------------------------------------
# Operator notices
# ---------------------------------------------------------------------------


def operator(client, monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(get_settings(), "operator_emails", "ops@example.com")
    client.post("/api/auth/magic-link", json={"email": "ops@example.com"})
    token = client.post("/api/auth/verify", json={"token": last_link_token("ops@example.com")}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_operator_notice_preview_then_send(client, monkeypatch):
    a = signup(client, "A")
    b = signup(client, "B")
    closed = signup(client, "C")
    client.post("/api/account/close", json={"confirm_name": "C"}, headers=closed.h)
    ops = operator(client, monkeypatch)
    body = {"subject": "Security notice", "message": "First paragraph.\n\nSecond paragraph."}
    r = client.post("/api/admin/notices", json=body, headers=ops).json()
    assert r == {"warehouses": 2, "recipients": 2, "sent": 0}
    email.outbox.clear()
    r = client.post("/api/admin/notices", json={**body, "send": True}, headers=ops).json()
    assert r["sent"] == 2
    assert sorted(m.to for m in email.outbox) == sorted([a.email, b.email])
    assert "Second paragraph." in email.outbox[0].text
    assert client.get("/api/audit", headers=a.h).json()[0]["action"] == "notice.sent"
    assert client.get("/api/admin/notices", headers=ops).json()[0]["subject"] == "Security notice"
    only = client.post("/api/admin/notices", json={**body, "warehouse_ids": [closed.warehouse_id]}, headers=ops).json()
    assert only["warehouses"] == 1  # chosen explicitly, closed accounts can be reached too
    assert client.post("/api/admin/notices", json=body, headers=a.h).status_code == 403


# ---------------------------------------------------------------------------
# Worker notice
# ---------------------------------------------------------------------------


def test_worker_sees_the_privacy_notice_before_the_first_order(client):
    owner = signup(client)
    w = add_worker(client, owner, "Sam")
    phone = link_phone(client, owner)
    r = client.post("/api/worker/login", json={"pin": w["pin"]}, headers=phone.h).json()
    assert r["notice_required"] is True
    h = {**phone.h, "Authorization": f"Bearer {r['session_token']}"}
    blocked = client.get("/api/worker/orders", headers=h)
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "notice_required"
    assert client.post("/api/worker/notice", json={"version": "0"}, headers=h).status_code == 400
    ack = client.post("/api/worker/notice", json={"version": r["notice_version"]}, headers=h)
    assert ack.status_code == 200
    assert client.get("/api/worker/orders", headers=h).status_code == 200
    # Next sign-in: no notice again.
    again = client.post("/api/worker/login", json={"pin": w["pin"]}, headers=phone.h).json()
    assert again["notice_required"] is False
    listed = client.get("/api/workers", headers=owner.h).json()["workers"]
    assert listed[0]["notice_acknowledged_at"]
    make_order(client, owner)


def test_revoke_sessions_cli(client, db):
    from autorack import cli

    a = signup(client, "A")
    b = signup(client, "B")
    phone = worker_on_phone(client, a)
    cli.main(["revoke-sessions", a.email, "--workers"])
    assert client.get("/api/auth/me", headers=a.h).status_code == 401
    assert client.get("/api/auth/me", headers=b.h).status_code == 200
    assert client.get("/api/worker/orders", headers=phone.h).status_code == 401
    cli.main(["revoke-sessions", "--all"])
    assert client.get("/api/auth/me", headers=b.h).status_code == 401
