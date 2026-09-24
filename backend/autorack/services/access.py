"""Can this warehouse use the product right now?

A single function decides, so the dashboard banner, the lockout screen on the
phone, and the API gate can never disagree. Being locked out blocks *new work*
(worker sign-in, opening orders, creating/importing orders). It never blocks
reading history, managing billing, or syncing scans that were already made:
those happened, and the audit trail must hold them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import get_settings
from ..models import SubscriptionStatus, Warehouse, utcnow


@dataclass(frozen=True)
class Access:
    allowed: bool
    state: str
    message: str
    trial_ends_at: datetime | None = None
    trial_days_left: int | None = None
    grace_ends_at: datetime | None = None


def evaluate(wh: Warehouse, now: datetime | None = None) -> Access:
    now = now or utcnow()
    status = wh.subscription_status
    s = get_settings()

    if status == SubscriptionStatus.pilot:
        return Access(True, "pilot", "Free pilot.")
    if status == SubscriptionStatus.active:
        if wh.cancel_at_period_end and wh.current_period_end:
            return Access(True, "active", "Your subscription ends at the close of this billing period.")
        return Access(True, "active", "Subscription active.")
    if status == SubscriptionStatus.trialing:
        ends = wh.trial_ends_at
        # With a Stripe subscription attached, Stripe owns the trial clock and
        # tells us when it ends; without one, the trial is ours to expire.
        if ends and not wh.stripe_subscription_id and now >= ends:
            return Access(
                False,
                "trial_expired",
                "Your free trial has ended. Subscribe to keep verifying picks.",
                trial_ends_at=ends,
                trial_days_left=0,
            )
        days_left = max(0, math.ceil((ends - now).total_seconds() / 86400)) if ends else None
        return Access(True, "trialing", "Free trial.", trial_ends_at=ends, trial_days_left=days_left)
    if status == SubscriptionStatus.past_due:
        since = wh.past_due_since or now
        grace_end = since + timedelta(days=s.past_due_grace_days)
        if now < grace_end:
            return Access(
                True,
                "grace",
                "Your last payment failed. Update your card to avoid interruption.",
                grace_ends_at=grace_end,
            )
        return Access(
            False,
            "past_due",
            "Your account is past due. Update your payment method to resume scanning.",
            grace_ends_at=grace_end,
        )
    if status == SubscriptionStatus.canceled:
        return Access(False, "canceled", "Your subscription is canceled. Resubscribe to resume scanning.")
    return Access(False, status.value, "Your subscription needs attention. Visit Billing to resolve it.")
