"""Background jobs: the emails nobody has to remember to send.

* Daily summary -- at the hour each warehouse picks (its local time): orders
  shipped, mistakes caught, and the money that saved.
* Instant alerts -- a worker flagged a problem or reported a short pick; or a
  worker's error rate suddenly spiked.
* Account emails -- trial ending soon / ended; a payment failed, and when the
  grace period is about to run out.

Every email is claimed in `notifications_sent` (unique per warehouse, kind and
key) in the same transaction that sends it, so running the jobs twice -- two
app processes, the in-process loop plus an external cron, a retry -- never
sends twice, and a failed send is retried on the next run.

`run_all` is called every minute by the in-process loop (main.py) and by
`POST /api/cron/run` / `python -m autorack.cli run-jobs`, for hosts that sleep
when idle and need an outside nudge.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, String, cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    MagicLinkToken,
    Membership,
    NotificationSent,
    Order,
    OrderFlag,
    OrderLineItem,
    OrderStatus,
    OwnerSession,
    ScanEvent,
    ScanResult,
    SubscriptionStatus,
    User,
    UserRole,
    Warehouse,
    Worker,
    utcnow,
)
from . import email, ratelimit
from .dashboard import day_bounds, tz_of

log = logging.getLogger("autorack.jobs")

ADVISORY_LOCK_ID = 0x4155_5452  # "AUTR": one job runner at a time, cluster-wide
SPIKE_WINDOW = timedelta(minutes=60)
SPIKE_MIN_SCANS = 20
SPIKE_MIN_RATE = 0.10
FLAG_LOOKBACK = timedelta(hours=6)

REASON_LABELS = {
    "wrong_item_in_location": "Wrong item in the bin",
    "out_of_stock": "Out of stock",
    "damaged": "Damaged",
    "label_unreadable": "Label won't scan",
    "short_pick": "Short pick",
    "other": "Other",
}


def app_url(path: str = "") -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/app/{path}"


def money(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def claim(db: Session, warehouse_id: uuid.UUID | None, kind: str, key: str, recipients: int = 0) -> bool:
    """Reserve this notification. False if it was already sent."""
    row = db.execute(
        insert(NotificationSent)
        .values(warehouse_id=warehouse_id, kind=kind, key=key[:128], recipients=recipients, created_at=utcnow())
        .on_conflict_do_nothing(constraint="uq_notifications_once")
        .returning(NotificationSent.id)
    ).first()
    return row is not None


def recipients(db: Session, wh: Warehouse, *, want: str) -> list[str]:
    """Active team members who opted in. `want`: summary | alerts | owners."""
    stmt = (
        select(User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.warehouse_id == wh.id, Membership.active.is_(True), User.active.is_(True))
    )
    if want == "summary":
        stmt = stmt.where(Membership.email_daily_summary.is_(True))
    elif want == "alerts":
        stmt = stmt.where(Membership.email_alerts.is_(True))
    elif want == "owners":
        stmt = stmt.where(Membership.role == UserRole.owner)
    emails = sorted(set(db.scalars(stmt)))
    if want == "owners" and wh.owner_email and wh.owner_email not in emails:
        emails.append(wh.owner_email)  # the billing contact
    return emails


def _send_all(db: Session, wh: Warehouse, kind: str, key: str, to: list[str], build: Callable[[str], Any]) -> bool:
    """Claim, send to everyone, commit. Rolls back (to retry) on failure."""
    if not to:
        # Nobody to tell; remember that we decided, so we don't re-check.
        claim(db, wh.id, kind, key, 0)
        db.commit()
        return False
    if not claim(db, wh.id, kind, key, len(to)):
        db.rollback()
        return False
    try:
        for addr in to:
            email.send(build(addr))
    except email.EmailError:
        db.rollback()
        log.exception("Sending %s to %s failed; will retry", kind, wh.id)
        return False
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------


def daily_numbers(db: Session, wh: Warehouse, day: Any = None) -> dict[str, Any]:
    start, end, day = day_bounds(wh, day)
    counts = {r.value: 0 for r in ScanResult}
    for result, n in db.execute(
        select(ScanEvent.result, func.count())
        .where(ScanEvent.warehouse_id == wh.id, ScanEvent.client_scanned_at >= start, ScanEvent.client_scanned_at < end)
        .group_by(ScanEvent.result)
    ):
        counts[result.value] = n
    errors = counts["mismatch"] + counts["over_pick"]
    attempts = counts["match"] + errors
    completed = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.warehouse_id == wh.id,
                Order.status.in_([OrderStatus.completed, OrderStatus.shipped]),
                Order.completed_at >= start,
                Order.completed_at < end,
            )
        )
        or 0
    )
    shipped = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.warehouse_id == wh.id, Order.shipped_at >= start, Order.shipped_at < end)
        )
        or 0
    )
    open_flags = (
        db.scalar(
            select(func.count())
            .select_from(OrderFlag)
            .join(Order, Order.id == OrderFlag.order_id)
            .where(
                OrderFlag.warehouse_id == wh.id,
                OrderFlag.resolved_at.is_(None),
                Order.status != OrderStatus.cancelled,
            )
        )
        or 0
    )
    short_units = (
        db.scalar(
            select(func.coalesce(func.sum(OrderFlag.short_quantity), 0)).where(
                OrderFlag.warehouse_id == wh.id, OrderFlag.created_at >= start, OrderFlag.created_at < end
            )
        )
        or 0
    )
    top_sku = db.execute(
        select(func.max(OrderLineItem.sku_description), func.max(OrderLineItem.sku), func.count())
        .join(ScanEvent, ScanEvent.intended_line_item_id == OrderLineItem.id)
        .where(
            ScanEvent.warehouse_id == wh.id,
            ScanEvent.result == ScanResult.mismatch,
            ScanEvent.client_scanned_at >= start,
            ScanEvent.client_scanned_at < end,
        )
        .group_by(OrderLineItem.normalized_barcode)
        .order_by(func.count().desc())
        .limit(1)
    ).first()
    return {
        "day": day,
        "scans": sum(v for k, v in counts.items() if k != "void"),
        "units_picked": counts["match"] - counts["void"],
        "errors_caught": errors,
        "accuracy": counts["match"] / attempts if attempts else None,
        "money_saved_cents": errors * wh.cost_per_error_cents,
        "orders_completed": completed,
        "orders_shipped": shipped,
        "open_problems": open_flags,
        "units_short": int(short_units),
        "top_mispick": (
            f"{top_sku[0] or top_sku[1] or 'An item'} ({top_sku[2]} times)" if top_sku and top_sku[2] >= 2 else None
        ),
    }


def summary_email(wh: Warehouse, n: dict[str, Any], to: str) -> email.Email:
    day = n["day"].strftime("%A %-d %B")
    headline = (
        f"Autorack caught {n['errors_caught']} mistake{'s' if n['errors_caught'] != 1 else ''} before they shipped, "
        f"worth about {money(n['money_saved_cents'])}."
        if n["errors_caught"]
        else "No wrong items were scanned today. Clean shift."
    )
    rows = [
        ("Orders completed", f"{n['orders_completed']:,}"),
        ("Orders shipped (label scanned)", f"{n['orders_shipped']:,}"),
        ("Units verified", f"{n['units_picked']:,}"),
        ("Mistakes caught", f"{n['errors_caught']:,}"),
        ("Estimated money saved", money(n["money_saved_cents"])),
        ("Pick accuracy", pct(n["accuracy"])),
        ("Units reported short", f"{n['units_short']:,}"),
        ("Problems still open", f"{n['open_problems']:,}"),
    ]
    extra = f"Most mis-picked today: {n['top_mispick']}." if n["top_mispick"] else ""
    return email.notice_email(
        to,
        subject=(
            f"{wh.name}: {n['errors_caught']} mistake{'s' if n['errors_caught'] != 1 else ''} caught today "
            f"({money(n['money_saved_cents'])} saved)"
        ),
        heading=f"{wh.name} · {day}",
        lines=[headline],
        rows=rows,
        extra_text=extra,
        button_label="Open the dashboard",
        url=app_url("#/"),
        footer=(
            f"Money saved = mistakes caught x {money(wh.cost_per_error_cents)}, your cost of one wrong shipment "
            "(change it in Settings). You get this email because you're on this warehouse's team; turn it off in "
            "Settings → My emails."
        ),
    )


def run_daily_summaries(db: Session, now: datetime) -> int:
    sent = 0
    for wh in db.scalars(select(Warehouse).where(Warehouse.daily_summary_enabled.is_(True))):
        local = now.astimezone(tz_of(wh))
        if local.hour < wh.daily_summary_hour:
            continue
        key = local.date().isoformat()
        if db.scalar(
            select(NotificationSent.id).where(
                NotificationSent.warehouse_id == wh.id,
                NotificationSent.kind == "daily_summary",
                NotificationSent.key == key,
            )
        ):
            continue
        numbers = daily_numbers(db, wh, local.date())
        if numbers["scans"] == 0 and numbers["orders_completed"] == 0:
            claim(db, wh.id, "daily_summary", key, 0)  # a quiet day: nothing to report
            db.commit()
            continue
        to = recipients(db, wh, want="summary")
        if _send_all(db, wh, "daily_summary", key, to, lambda addr, w=wh, n=numbers: summary_email(w, n, addr)):
            sent += 1
    return sent


# ---------------------------------------------------------------------------
# Instant alerts
# ---------------------------------------------------------------------------


def run_flag_alerts(db: Session, now: datetime) -> int:
    sent = 0
    since = now - FLAG_LOOKBACK
    for wh in db.scalars(select(Warehouse).where(Warehouse.alert_on_flag.is_(True))):
        already = select(NotificationSent.key).where(
            NotificationSent.warehouse_id == wh.id, NotificationSent.kind == "flag"
        )
        rows = list(
            db.execute(
                select(OrderFlag, Order.external_order_number, OrderLineItem.sku_description, OrderLineItem.sku)
                .join(Order, Order.id == OrderFlag.order_id)
                .outerjoin(OrderLineItem, OrderLineItem.id == OrderFlag.line_item_id)
                .where(
                    OrderFlag.warehouse_id == wh.id,
                    OrderFlag.created_at >= since,
                    OrderFlag.resolved_at.is_(None),
                    cast(OrderFlag.id, String).not_in(already),
                )
                .order_by(OrderFlag.created_at)
                .limit(20)
            )
        )
        if not rows:
            continue
        names = {w: n for w, n in db.execute(select(Worker.id, Worker.name).where(Worker.warehouse_id == wh.id))}
        to = recipients(db, wh, want="alerts")
        described = []
        for f, number, desc, sku in rows:
            what = REASON_LABELS.get(f.reason.value, f.reason.value)
            if f.short_quantity:
                what = f"Short {f.short_quantity}"
            item = desc or sku or "an item"
            who = names.get(f.worker_id, "A worker") if f.worker_id else "A worker"
            note = f" — “{f.note}”" if f.note else ""
            described.append(f"{who}: {what} on {item}, order {number or str(f.order_id)[:8]}{note}")
        # One email per batch; every flag in it is marked as alerted.
        batch_key = str(rows[-1][0].id)
        if not to:
            for f, *_ in rows:
                claim(db, wh.id, "flag", str(f.id))
            db.commit()
            continue
        for f, *_ in rows[:-1]:
            claim(db, wh.id, "flag", str(f.id))
        count = len(rows)
        subject = (
            f"{wh.name}: {described[0].split(':')[0]} flagged a problem"
            if count == 1
            else f"{wh.name}: {count} problems flagged on the floor"
        )

        def build(addr: str, w: Warehouse = wh, lines: list[str] = described, subj: str = subject) -> email.Email:
            return email.notice_email(
                addr,
                subject=subj,
                heading="A picker needs a decision",
                lines=[
                    "These orders are on hold until someone on the team resolves them:",
                    *lines,
                ],
                button_label="Review problems",
                url=app_url("#/"),
                footer=f"Instant alerts for {w.name}. Turn them off in Settings → My emails.",
            )

        if _send_all(db, wh, "flag", batch_key, to, build):
            sent += 1
    return sent


def run_error_spikes(db: Session, now: datetime) -> int:
    """A worker making mistakes at a very unusual rate right now: a new
    starter who needs help, a relabelled product, a bin that got swapped."""
    sent = 0
    since = now - SPIKE_WINDOW
    for wh in db.scalars(select(Warehouse).where(Warehouse.alert_error_rate.is_(True))):
        base_total, base_err = db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(cast(ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick]), Integer)), 0
                ),
            ).where(
                ScanEvent.warehouse_id == wh.id,
                ScanEvent.client_scanned_at >= now - timedelta(days=7),
                ScanEvent.client_scanned_at < since,  # the normal rate, not the spike itself
                ScanEvent.result != ScanResult.void,
            )
        ).one()
        baseline = (base_err / base_total) if base_total else 0.0
        threshold = max(SPIKE_MIN_RATE, 3 * baseline)
        local_day = now.astimezone(tz_of(wh)).date().isoformat()
        for wid, name, scans, errors in db.execute(
            select(
                ScanEvent.worker_id,
                Worker.name,
                func.count(),
                func.sum(cast(ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick]), Integer)),
            )
            .join(Worker, Worker.id == ScanEvent.worker_id)
            .where(
                ScanEvent.warehouse_id == wh.id,
                ScanEvent.client_scanned_at >= since,
                ScanEvent.result != ScanResult.void,
            )
            .group_by(ScanEvent.worker_id, Worker.name)
        ):
            rate = (errors or 0) / scans if scans else 0.0
            if scans < SPIKE_MIN_SCANS or rate < threshold:
                continue
            key = f"{wid}:{local_day}"
            to = recipients(db, wh, want="alerts")

            def build(
                addr: str,
                w: Warehouse = wh,
                nm: str = name,
                r: float = rate,
                e: int = errors,
                n: int = scans,
                base: float = baseline,
            ) -> email.Email:
                return email.notice_email(
                    addr,
                    subject=f"{w.name}: {nm}'s error rate jumped to {pct(r)}",
                    heading=f"{nm} may need a hand",
                    lines=[
                        f"In the last hour {nm} scanned {e} wrong item{'s' if e != 1 else ''} out of {n} scans "
                        f"({pct(r)}). Your warehouse normally runs at {pct(base)}.",
                        "Every one was caught before it shipped. It's usually a new starter, a relabelled product "
                        "or two bins that got swapped. Worth a look.",
                    ],
                    button_label="See worker details",
                    url=app_url("#/workers"),
                    footer=f"You'll get this at most once a day per worker. Instant alerts for {w.name}; "
                    "turn them off in Settings → My emails.",
                )

            if _send_all(db, wh, "error_spike", key, to, build):
                sent += 1
    return sent


# ---------------------------------------------------------------------------
# Account emails
# ---------------------------------------------------------------------------


def run_account_emails(db: Session, now: datetime) -> int:
    sent = 0
    s = get_settings()
    price = money(s.plan_price_cents)
    for wh in db.scalars(
        select(Warehouse).where(
            Warehouse.subscription_status.in_([SubscriptionStatus.trialing, SubscriptionStatus.past_due])
        )
    ):
        to = recipients(db, wh, want="owners")
        if wh.subscription_status == SubscriptionStatus.trialing and wh.trial_ends_at:
            left = wh.trial_ends_at - now
            has_card = bool(wh.stripe_subscription_id)
            ends = wh.trial_ends_at.astimezone(tz_of(wh)).strftime("%A %-d %B")
            if left <= timedelta(0):
                if has_card or left < -timedelta(days=7):
                    continue
                kind, subject, heading, lines = (
                    "trial_ended",
                    f"Your Autorack trial for {wh.name} has ended",
                    "Your free trial has ended",
                    [
                        "Phones can't start new picks until you subscribe. Your orders, scans and reports are all "
                        "still here.",
                        f"It's {price}/month for the warehouse, flat: unlimited workers, phones and scans.",
                    ],
                )
            elif left <= timedelta(days=1):
                kind, subject, heading, lines = (
                    "trial_1d",
                    f"Your Autorack trial ends tomorrow ({wh.name})",
                    "Your trial ends tomorrow",
                    [
                        f"Your free trial for {wh.name} ends {ends}.",
                        (
                            f"Your card will be charged {price}/month from then. Nothing else to do."
                            if has_card
                            else f"Subscribe now ({price}/month, flat) so scanning doesn't stop mid-shift."
                        ),
                    ],
                )
            elif left <= timedelta(days=3):
                kind, subject, heading, lines = (
                    "trial_3d",
                    f"3 days left on your Autorack trial ({wh.name})",
                    "Three days left on your trial",
                    [
                        f"Your free trial for {wh.name} ends {ends}.",
                        (
                            f"Your card is on file; billing starts at {price}/month."
                            if has_card
                            else f"To keep verifying picks, subscribe for {price}/month, flat. Cancel any time."
                        ),
                    ],
                )
            else:
                continue
            key = f"{kind}:{wh.trial_ends_at.date().isoformat()}"
        elif wh.subscription_status == SubscriptionStatus.past_due and wh.past_due_since:
            grace_end = wh.past_due_since + timedelta(days=s.past_due_grace_days)
            if now >= grace_end - timedelta(days=2) and now < grace_end + timedelta(days=3):
                kind = "grace_ending"
                subject = f"Action needed: scanning at {wh.name} pauses soon"
                heading = "Please update your card"
                lines = [
                    "We still couldn't collect your Autorack payment.",
                    "Scanning pauses when the grace period ends "
                    f"({grace_end.astimezone(tz_of(wh)).strftime('%A %-d %B')}). Your data is safe either way.",
                ]
            else:
                kind = "payment_failed"
                subject = f"Your Autorack payment for {wh.name} didn't go through"
                heading = "Your payment didn't go through"
                lines = [
                    "Your bank declined this month's payment. Nothing has stopped: you have "
                    f"{s.past_due_grace_days} days to update your card.",
                    "Stripe will retry automatically once the card is updated.",
                ]
            key = f"{kind}:{wh.past_due_since.date().isoformat()}"
        else:
            continue

        def build(
            addr: str, subj: str = subject, head: str = heading, ls: list[str] = lines, w: Warehouse = wh
        ) -> email.Email:
            return email.notice_email(
                addr,
                subject=subj,
                heading=head,
                lines=ls,
                button_label="Go to billing",
                url=app_url("#/billing"),
                footer=f"Account email for {w.name}, sent to its owners. Questions? Just reply.",
            )

        if db.scalar(
            select(NotificationSent.id).where(
                NotificationSent.warehouse_id == wh.id, NotificationSent.kind == kind, NotificationSent.key == key
            )
        ):
            continue
        if _send_all(db, wh, kind, key, to, build):
            sent += 1
    return sent


# ---------------------------------------------------------------------------
# Housekeeping and the runner
# ---------------------------------------------------------------------------


def prune(db: Session, now: datetime) -> int:
    n = db.execute(delete(MagicLinkToken).where(MagicLinkToken.expires_at < now - timedelta(days=1))).rowcount  # type: ignore[attr-defined]
    n += db.execute(delete(OwnerSession).where(OwnerSession.expires_at < now - timedelta(days=30))).rowcount  # type: ignore[attr-defined]
    n += ratelimit.prune_db(db)
    db.commit()
    return int(n)


def run_all(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Run every job once. Safe to call concurrently (advisory lock) and
    repeatedly (notifications are claimed once)."""
    now = now or utcnow()
    got = db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_ID}).scalar()
    if not got:
        db.rollback()
        return {"skipped": "another runner is busy"}
    out: dict[str, Any] = {}
    try:
        for name, job in (
            ("daily_summaries", run_daily_summaries),
            ("flag_alerts", run_flag_alerts),
            ("error_spikes", run_error_spikes),
            ("account_emails", run_account_emails),
        ):
            try:
                out[name] = job(db, now)
            except Exception:
                db.rollback()
                log.exception("job %s failed", name)
                out[name] = "error"
        if now.minute == 0:
            out["pruned"] = prune(db, now)
    finally:
        db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_ID})
        db.commit()
    return out


class JobLoop:
    """Runs `run_all` every `interval` seconds in a daemon thread."""

    def __init__(self, session_factory: Callable[[], Session], interval: float = 60.0) -> None:
        self.session_factory = session_factory
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="autorack-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Let the app finish starting (and migrations settle) first.
        if self._stop.wait(15):
            return
        while not self._stop.is_set():
            try:
                with self.session_factory() as db:
                    run_all(db)
            except Exception:
                log.exception("job loop iteration failed")
            self._stop.wait(self.interval)
