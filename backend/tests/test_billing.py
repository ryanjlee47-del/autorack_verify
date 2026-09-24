"""Flat-price billing, access gating, Stripe webhooks, append-only history."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_order, scan_event, signup, sync, worker_on_phone
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from autorack.models import SubscriptionStatus, Warehouse, utcnow
from autorack.services import access, billing


def set_wh(db, owner, **fields):
    wh = db.scalar(select(Warehouse).where(Warehouse.id == owner.warehouse_id))
    for k, v in fields.items():
        setattr(wh, k, v)
    db.commit()
    return wh


class FakeGateway:
    def __init__(self) -> None:
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.checkouts: list[dict[str, Any]] = []
        self.customers = 0

    def create_customer(self, **kw):
        self.customers += 1
        return {"id": f"cus_{self.customers}"}

    def create_checkout_session(self, **params):
        self.checkouts.append(params)
        return {"url": "https://checkout.stripe.test/session"}

    def create_portal_session(self, **kw):
        return {"url": "https://billing.stripe.test/portal"}

    def retrieve_subscription(self, sub_id):
        return self.subscriptions[sub_id]

    construct_event = billing.StripeGateway.construct_event  # real signature check


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(billing, "gateway", fake)
    return fake


def signed(payload: dict[str, Any], secret: str = "whsec_test_secret") -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


def post_event(client, event: dict[str, Any], secret: str = "whsec_test_secret"):
    body, header = signed(event, secret)
    return client.post("/api/webhooks/stripe", content=body, headers={"Stripe-Signature": header})


def sub_event(event_id: str, etype: str, sub_id: str, customer: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": etype,
        "object": "event",
        "data": {"object": {"id": sub_id, "object": "subscription", "customer": customer, "metadata": {}}},
    }


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_expired_trial_blocks_new_work_but_not_history(client, db):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    queued = scan_event(phone, oid, "012345678905", offline=True)
    set_wh(db, owner, trial_ends_at=utcnow() - timedelta(minutes=1))

    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["access"] == {**me["access"], "allowed": False, "state": "trial_expired"}
    r = client.get("/api/worker/orders", headers=phone.h)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "subscription_inactive"
    assert client.post("/api/orders", json={"lines": [{"barcode": "1"}]}, headers=owner.h).status_code == 402
    # Reading history and syncing scans that already happened still work.
    assert client.get("/api/orders", headers=owner.h).status_code == 200
    assert sync(client, phone, queued)["events"][0]["status"] == "applied"
    assert client.get("/api/billing", headers=owner.h).status_code == 200


def test_past_due_has_grace_period(db, client):
    owner = signup(client)
    wh = set_wh(db, owner, subscription_status=SubscriptionStatus.past_due, past_due_since=utcnow() - timedelta(days=2))
    acc = access.evaluate(wh)
    assert acc.allowed and acc.state == "grace"
    wh = set_wh(db, owner, past_due_since=utcnow() - timedelta(days=8))
    acc = access.evaluate(wh)
    assert not acc.allowed and acc.state == "past_due"


def test_pilot_is_free_and_unlimited(db, client):
    owner = signup(client)
    wh = set_wh(db, owner, subscription_status=SubscriptionStatus.pilot, trial_ends_at=utcnow() - timedelta(days=90))
    assert access.evaluate(wh).allowed


def test_locked_warehouse_cannot_sign_workers_in(client, db):
    owner = signup(client)
    from conftest import add_worker, link_phone

    w = add_worker(client, owner)
    phone = link_phone(client, owner)
    set_wh(db, owner, subscription_status=SubscriptionStatus.canceled)
    r = client.post("/api/worker/login", json={"pin": w["pin"]}, headers=phone.h)
    assert r.status_code == 402
    assert client.get("/api/worker/device", headers=phone.h).json()["access"]["state"] == "canceled"


# ---------------------------------------------------------------------------
# Checkout and portal
# ---------------------------------------------------------------------------


def test_checkout_creates_customer_once_and_carries_trial(client, db, fake_stripe):
    owner = signup(client)
    r = client.post("/api/billing/checkout", headers=owner.h)
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test")
    client.post("/api/billing/checkout", headers=owner.h)
    assert fake_stripe.customers == 1
    params = fake_stripe.checkouts[0]
    assert params["line_items"] == [{"price": "price_test_175", "quantity": 1}]
    assert params["client_reference_id"] == owner.warehouse_id
    assert "trial_end" in params["subscription_data"]  # 14 unused trial days carried over
    assert params["success_url"].startswith("https://app.autorack.test/app/")


def test_portal_requires_customer(client, fake_stripe):
    owner = signup(client)
    assert client.post("/api/billing/portal", headers=owner.h).status_code == 400
    client.post("/api/billing/checkout", headers=owner.h)
    assert client.post("/api/billing/portal", headers=owner.h).json()["url"].startswith("https://billing")


def test_billing_info_shows_flat_price(client):
    owner = signup(client)
    info = client.get("/api/billing", headers=owner.h).json()
    assert info["price_cents"] == 17500 and info["interval"] == "month"
    assert client.get("/api/public/config").json()["price_cents"] == 17500


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def test_webhook_rejects_bad_signature(client, fake_stripe):
    r = post_event(client, sub_event("evt_1", "customer.subscription.updated", "sub_1", "cus_1"), secret="whsec_wrong")
    assert r.status_code == 400


def test_webhook_activates_and_is_idempotent(client, db, fake_stripe):
    owner = signup(client)
    client.post("/api/billing/checkout", headers=owner.h)  # creates cus_1 on the warehouse
    period_end = int(time.time()) + 30 * 86400
    fake_stripe.subscriptions["sub_1"] = {
        "id": "sub_1",
        "status": "active",
        "customer": "cus_1",
        "cancel_at_period_end": False,
        "items": {"data": [{"current_period_end": period_end}]},
    }
    event = sub_event("evt_1", "customer.subscription.created", "sub_1", "cus_1")
    assert post_event(client, event).json()["handled"] is True
    assert post_event(client, event).json()["duplicate"] is True
    info = client.get("/api/billing", headers=owner.h).json()
    assert info["status"] == "active" and info["has_subscription"] and info["current_period_end"]


def test_webhook_applies_current_state_not_stale_event(client, db, fake_stripe):
    """Stripe may deliver 'updated: active' after 'updated: past_due'. We always
    re-fetch, so the late event cannot resurrect a stale status."""
    owner = signup(client)
    client.post("/api/billing/checkout", headers=owner.h)
    fake_stripe.subscriptions["sub_1"] = {"id": "sub_1", "status": "past_due", "customer": "cus_1"}
    post_event(client, sub_event("evt_new", "customer.subscription.updated", "sub_1", "cus_1"))
    post_event(client, sub_event("evt_old", "customer.subscription.updated", "sub_1", "cus_1"))
    info = client.get("/api/billing", headers=owner.h).json()
    assert info["status"] == "past_due" and info["access"]["state"] == "grace"


def test_payment_failed_invoice_moves_to_past_due(client, fake_stripe):
    owner = signup(client)
    client.post("/api/billing/checkout", headers=owner.h)
    fake_stripe.subscriptions["sub_9"] = {"id": "sub_9", "status": "past_due", "customer": "cus_1"}
    event = {
        "id": "evt_inv",
        "type": "invoice.payment_failed",
        "data": {
            "object": {"id": "in_1", "customer": "cus_1", "parent": {"subscription_details": {"subscription": "sub_9"}}}
        },
    }
    assert post_event(client, event).json()["handled"]
    assert client.get("/api/billing", headers=owner.h).json()["status"] == "past_due"
    log = client.get("/api/audit", headers=owner.h).json()
    assert any(e["action"] == "billing.status_changed" and e["details"]["after"] == "past_due" for e in log)


def test_unhandled_event_types_are_acknowledged(client, fake_stripe):
    r = post_event(client, {"id": "evt_x", "type": "charge.refunded", "data": {"object": {}}})
    assert r.status_code == 200 and r.json()["handled"] is False


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE scan_events SET result = 'match'",
        "DELETE FROM scan_events",
        "UPDATE audit_log SET action = 'x'",
        "DELETE FROM audit_log",
    ],
)
def test_history_tables_are_append_only(client, db, statement):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    sync(client, phone, scan_event(phone, oid, "wrong-item"))
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text(statement))
        db.commit()
    db.rollback()
