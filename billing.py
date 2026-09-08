"""Billing engine: turns a qualifying scan into a billing_events row.

The business model is per-error-caught: N free catches, then a flat rate
per prevented mis-ship (see pricing.py for the constants). A "catch" is a
REJECT -- an item confidently identified as not belonging on the
manifest -- never an 'ok' or 'duplicate' scan. See the trigger comment in
migrations/0001_initial.sql and static/js/worker/app.js for how 'reject'
(confident) is distinguished from 'unresolved' (ambiguous, never
auto-billed).

Idempotency: billing_events has a UNIQUE(scan_uuid, kind) index, so
calling process_scan_for_billing() twice for the same scan (e.g. because
the outbox resent it) is a safe no-op, not a double charge.
"""

from __future__ import annotations

import db
import pricing
from sqlstore import SQL


def process_scan_for_billing(conn, account_id: int, scan_uuid: str) -> int | None:
    """Create a 'catch' billing_events row for a confidently-rejected scan,
    if one doesn't already exist. Returns the new row id, or None if the
    scan isn't billable (wrong result) or was already processed.
    """
    if db.billing_event_exists_for_scan(conn, scan_uuid, "catch"):
        return None
    scan = db.get_scan(conn, scan_uuid)
    if not scan or scan["result"] != "reject":
        return None
    return _insert_catch(conn, account_id, scan_uuid)


def confirm_exception_as_billable_catch(conn, account_id: int, scan_uuid: str) -> int | None:
    """Called when an owner confirms an 'unresolved' (ambiguous) scan was
    genuinely a wrong item. The confirmed exception is what makes this
    billable -- see the billing_events trigger.
    """
    if db.billing_event_exists_for_scan(conn, scan_uuid, "catch"):
        return None
    exc = db.get_exception_for_scan(conn, scan_uuid)
    if not exc or not exc["resolved_at"]:
        return None
    return _insert_catch(conn, account_id, scan_uuid)


def _insert_catch(conn, account_id: int, scan_uuid: str) -> int:
    # BEGIN IMMEDIATE, not the default deferred BEGIN: this counts existing
    # catches and then inserts based on that count, and under a deferred
    # transaction the count runs before any write lock is taken. Two
    # concurrent /w/sync batches landing at the free-allowance boundary both
    # observed `already_billed < free_allowance` and both charged zero.
    #
    # If a caller already opened a transaction this joins it (db.transaction
    # is re-entrant), so callers doing read-then-write on billing state must
    # request `immediate=True` at their own outermost level -- w_sync does.
    with db.transaction(conn, immediate=True):
        return _insert_catch_locked(conn, account_id, scan_uuid)


def _insert_catch_locked(conn, account_id: int, scan_uuid: str) -> int:
    already_billed = db.count_billable_catches(conn, account_id)
    account = db.get_account(conn, account_id)
    if account is None:
        # Every caller reaches here from a scan that is already tied to
        # this account, so a missing row means referential damage, not a
        # normal miss. Fail rather than invent a price.
        raise LookupError(f"cannot bill unknown account {account_id}")
    free_allowance = account["free_allowance"]
    price = account["price_per_catch_cents"]
    cents = 0 if already_billed < free_allowance else price
    return db.insert_billing_event(conn, account_id, scan_uuid, "catch", cents, billable=True)


def reverse_billing_event(conn, billing_event_id: int, reason: str, actor: str) -> int:
    """Insert an offsetting 'reversal' row. Never mutates the original
    row -- billing_events is append-only (see the no-UPDATE/no-DELETE
    triggers) -- and requires a typed reason, logged to audit_log."""
    if not reason or not reason.strip():
        raise ValueError("a reversal requires a typed reason")
    row = db.query_one(conn, SQL["billing.get_billing_event"], (billing_event_id,))
    if not row:
        raise ValueError("billing event not found")

    db.record_audit(
        conn,
        actor=actor,
        action="billing.reverse",
        target_table="billing_events",
        target_id=str(billing_event_id),
        before=dict(row),
        after={"reason": reason},
    )
    # UNIQUE(scan_uuid, kind) means at most one reversal per scan_uuid --
    # a given catch can only be reversed once, which is the right limit.
    return db.execute(
        conn,
        SQL["billing.insert_reversal"],
        (row["account_id"], row["scan_uuid"], "reversal", -row["cents"], 1, reason),
    )


def reverse_if_billed(conn, scan_uuid: str, reason: str, actor: str) -> int | None:
    """When an exception is resolved to a real manifest line (i.e. the
    scan turns out NOT to have been a genuine mis-ship after all -- the
    common case for an approved worker appeal, but also for an owner
    correcting a mistaken reject noticed in the Live feed), reverse any
    'catch' billing event that was already created for it. Returns the
    new reversal row id, or None if the scan was never billed."""
    row = db.query_one(
        conn,
        SQL["billing.find_catch_for_scan"],
        (scan_uuid,),
    )
    if not row:
        return None
    return reverse_billing_event(conn, row["id"], reason, actor)


def net_amount_owed_cents(
    conn, account_id: int, start: str | None = None, end: str | None = None
) -> int:
    events = db.billing_events_for_account(conn, account_id, start, end)
    total = sum(e["cents"] for e in events if e["billable"])
    # Credits follow the same window as the events they offset -- see
    # db.total_credit_cents.
    total -= db.total_credit_cents(conn, account_id, start, end)
    # Signed, deliberately. This used to return max(total, 0), which meant a
    # credit larger than the current balance was silently discarded rather
    # than carried forward -- a goodwill credit issued in a quiet month
    # simply evaporated. A negative result is a real answer: credit the
    # account holds and has not yet used, and pricing.format_cents_as_dollars
    # already renders the sign. Anything that eventually produces an invoice
    # (nothing does yet; billing_events.invoice_id is unwritten) must clamp at
    # its own boundary and carry the remainder, not lose it here.
    return total


def savings_to_date_cents(conn, account_id: int) -> int:
    """ "Money saved" framing for the owner dashboard/marketing: total
    catches * the assumed average cost of a mis-ship. Distinct from what
    is actually billed -- see pricing.py."""
    catches = db.count_billable_catches(conn, account_id)
    return catches * pricing.SAVINGS_PER_CATCH_CENTS
