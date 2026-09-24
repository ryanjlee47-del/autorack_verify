"""Billing: one flat monthly price per warehouse, via Stripe Subscriptions.

Flow:
  * Signup starts a local 14-day trial. No card needed to start.
  * "Subscribe" opens Stripe Checkout. We create the Stripe customer first and
    store its id, so every later webhook can find the warehouse even if events
    arrive before `checkout.session.completed`.
  * Webhooks keep `subscription_status` in sync. Stripe does not promise
    delivery order, so rather than trusting the event body we re-fetch the
    subscription and apply its *current* state. A late, stale event can then
    never overwrite a newer status.
  * "Manage billing" opens the Stripe Customer Portal (card updates,
    invoices, cancellation) -- no billing UI of our own to maintain.

Everything Stripe-specific goes through `gateway`, which returns plain dicts,
so tests can swap it out without network access.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError, bad_request
from ..models import StripeEvent, SubscriptionStatus, User, Warehouse, utcnow
from . import audit
from .access import evaluate
from .audit import Actor

log = logging.getLogger("autorack.billing")
STRIPE_ACTOR = Actor("stripe", label="webhook")


class StripeGateway:
    def _client(self) -> None:
        stripe.api_key = get_settings().stripe_secret_key

    def create_customer(self, *, email: str, name: str, warehouse_id: str) -> dict[str, Any]:
        self._client()
        c = stripe.Customer.create(email=email, name=name, metadata={"warehouse_id": warehouse_id})
        return c.to_dict()

    def create_checkout_session(self, **params: Any) -> dict[str, Any]:
        self._client()
        return stripe.checkout.Session.create(**params).to_dict()

    def create_portal_session(self, *, customer: str, return_url: str) -> dict[str, Any]:
        self._client()
        return stripe.billing_portal.Session.create(customer=customer, return_url=return_url).to_dict()

    def retrieve_subscription(self, sub_id: str) -> dict[str, Any]:
        self._client()
        return stripe.Subscription.retrieve(sub_id).to_dict()

    def construct_event(self, payload: bytes, sig_header: str | None) -> dict[str, Any]:
        event = stripe.Webhook.construct_event(payload, sig_header, get_settings().stripe_webhook_secret)
        return event.to_dict()


gateway = StripeGateway()


def _require_stripe() -> None:
    if not get_settings().stripe_enabled:
        raise ApiError(503, "billing_unavailable", "Online billing isn't configured yet. Contact support to subscribe.")


def billing_info(wh: Warehouse) -> dict[str, Any]:
    s = get_settings()
    acc = evaluate(wh)
    return {
        "plan_name": s.plan_name,
        "price_cents": s.plan_price_cents,
        "currency": "usd",
        "interval": "month",
        "status": wh.subscription_status.value,
        "access": {
            "allowed": acc.allowed,
            "state": acc.state,
            "message": acc.message,
            "trial_ends_at": acc.trial_ends_at.isoformat() if acc.trial_ends_at else None,
            "trial_days_left": acc.trial_days_left,
            "grace_ends_at": acc.grace_ends_at.isoformat() if acc.grace_ends_at else None,
        },
        "current_period_end": wh.current_period_end.isoformat() if wh.current_period_end else None,
        "cancel_at_period_end": wh.cancel_at_period_end,
        "has_subscription": bool(wh.stripe_subscription_id),
        "can_manage": bool(wh.stripe_customer_id) and s.stripe_enabled,
        "stripe_enabled": s.stripe_enabled,
    }


def create_checkout(db: Session, wh: Warehouse, user: User, actor: Actor) -> str:
    _require_stripe()
    s = get_settings()
    if wh.subscription_status in (SubscriptionStatus.active, SubscriptionStatus.past_due) and wh.stripe_subscription_id:
        raise bad_request("already_subscribed", "You already have a subscription. Use Manage billing instead.")
    if not wh.stripe_customer_id:
        customer = gateway.create_customer(email=wh.owner_email, name=wh.name, warehouse_id=str(wh.id))
        wh.stripe_customer_id = customer["id"]
        db.commit()
    base = s.frontend_url.rstrip("/")
    params: dict[str, Any] = {
        "mode": "subscription",
        "customer": wh.stripe_customer_id,
        "client_reference_id": str(wh.id),
        "line_items": [{"price": s.stripe_price_id, "quantity": 1}],
        "subscription_data": {"metadata": {"warehouse_id": str(wh.id)}},
        "success_url": f"{base}/app/#/billing?checkout=success",
        "cancel_url": f"{base}/app/#/billing?checkout=cancelled",
        "allow_promotion_codes": True,
    }
    # Carry the unused part of the local trial over, so subscribing early
    # doesn't cost the owner their remaining free days. Stripe requires a
    # trial end at least 48 hours out.
    if (
        wh.subscription_status == SubscriptionStatus.trialing
        and wh.trial_ends_at
        and wh.trial_ends_at - utcnow() > timedelta(hours=49)
    ):
        params["subscription_data"]["trial_end"] = int(wh.trial_ends_at.timestamp())
    session = gateway.create_checkout_session(**params)
    audit.record(db, actor, "billing.checkout_started", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id)
    db.commit()
    return str(session["url"])


def create_portal(wh: Warehouse) -> str:
    _require_stripe()
    if not wh.stripe_customer_id:
        raise bad_request("no_customer", "There's no billing account yet. Subscribe first.")
    s = get_settings()
    session = gateway.create_portal_session(
        customer=wh.stripe_customer_id, return_url=f"{s.frontend_url.rstrip('/')}/app/#/billing"
    )
    return str(session["url"])


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

HANDLED = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "invoice.paid",
    "invoice.payment_failed",
}


def handle_webhook(db: Session, payload: bytes, sig_header: str | None) -> dict[str, Any]:
    if not get_settings().stripe_webhook_secret:
        raise ApiError(503, "billing_unavailable", "Webhooks are not configured.")
    try:
        event = gateway.construct_event(payload, sig_header)
    except (ValueError, stripe.SignatureVerificationError):
        raise bad_request("bad_signature", "Invalid webhook signature.") from None

    event_id, etype = event["id"], event["type"]
    if etype not in HANDLED:
        return {"received": True, "handled": False}
    if db.get(StripeEvent, event_id):
        return {"received": True, "duplicate": True}

    obj = event["data"]["object"]
    wh = _find_warehouse(db, obj)
    if wh is None:
        log.warning("Stripe event %s (%s) matched no warehouse", event_id, etype)
        _remember(db, event_id, etype, None)
        return {"received": True, "handled": False}

    sub_id = _subscription_id(obj, etype)
    if etype == "checkout.session.completed" and obj.get("customer") and not wh.stripe_customer_id:
        wh.stripe_customer_id = obj["customer"]
    if sub_id:
        sub = gateway.retrieve_subscription(sub_id)
        apply_subscription(db, wh, sub)
    _remember(db, event_id, etype, wh.id)
    return {"received": True, "handled": True}


def _remember(db: Session, event_id: str, etype: str, wh_id: uuid.UUID | None) -> None:
    db.add(StripeEvent(id=event_id, type=etype, warehouse_id=wh_id))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event got there first.
        db.rollback()


def _subscription_id(obj: dict[str, Any], etype: str) -> str | None:
    if etype.startswith("customer.subscription."):
        return obj.get("id")
    sub = obj.get("subscription")
    if isinstance(sub, dict):
        return sub.get("id")
    if sub:
        return str(sub)
    # Newer API versions nest it under the invoice's parent.
    parent = obj.get("parent") or {}
    details = parent.get("subscription_details") or {}
    return details.get("subscription")


def _find_warehouse(db: Session, obj: dict[str, Any]) -> Warehouse | None:
    meta = obj.get("metadata") or {}
    for candidate in (meta.get("warehouse_id"), obj.get("client_reference_id")):
        if candidate:
            try:
                wh = db.get(Warehouse, uuid.UUID(str(candidate)))
            except ValueError:
                wh = None
            if wh:
                return wh
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer:
        return db.scalar(select(Warehouse).where(Warehouse.stripe_customer_id == customer))
    return None


def _ts(v: Any) -> datetime | None:
    return datetime.fromtimestamp(int(v), tz=UTC) if v else None


def apply_subscription(db: Session, wh: Warehouse, sub: dict[str, Any]) -> None:
    before = wh.subscription_status
    try:
        status = SubscriptionStatus(sub["status"])
    except ValueError:
        status = SubscriptionStatus.incomplete
    wh.subscription_status = status
    wh.stripe_subscription_id = sub.get("id") or wh.stripe_subscription_id
    if sub.get("customer") and isinstance(sub["customer"], str):
        wh.stripe_customer_id = sub["customer"]
    wh.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    period_end = sub.get("current_period_end")
    if not period_end:
        items = (sub.get("items") or {}).get("data") or []
        period_end = items[0].get("current_period_end") if items else None
    wh.current_period_end = _ts(period_end)
    if status == SubscriptionStatus.trialing:
        wh.trial_ends_at = _ts(sub.get("trial_end")) or wh.trial_ends_at
    if status == SubscriptionStatus.past_due:
        wh.past_due_since = wh.past_due_since or utcnow()
    else:
        wh.past_due_since = None
    if before != status:
        audit.record(
            db,
            STRIPE_ACTOR,
            "billing.status_changed",
            warehouse_id=wh.id,
            target_type="warehouse",
            target_id=wh.id,
            before=before.value,
            after=status.value,
        )
