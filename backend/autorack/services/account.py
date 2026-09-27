"""A customer's data, all of it: export, closing the account, deletion.

The license agreement (Section 9.1) lets Autorack delete a terminated
account's data 45 days after termination, and promises a copy first. So:

* Export -- one ZIP with every order, line, scan, problem report, photo,
  worker, phone, team member, taught barcode, import, activity-log entry, and
  the signed agreement. Available to owners any time, and to the operator.
* Close -- an owner (or the operator) closes the account: Stripe billing is
  cancelled, scanning stops, and deletion is scheduled `retention_days` out.
  Owners are emailed the date and a reminder a week before. Reopening before
  then cancels the deletion.
* Purge -- on the due date, everything is deleted except a tombstone row and
  the signed agreements (evidence of the contract). The append-only tables
  allow it only inside a transaction that sets autorack.purging.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from typing import IO, Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError, bad_request, conflict
from ..models import (
    AgreementSignature,
    AuditLog,
    BarcodeAlias,
    Client,
    Device,
    FeatureUsage,
    ImportBatch,
    Integration,
    KitComponent,
    MagicLinkToken,
    Membership,
    NotificationSent,
    Order,
    OrderFlag,
    OrderInsertCheck,
    OrderLineItem,
    OwnerSession,
    Package,
    PackInsert,
    Photo,
    PickBatch,
    Product,
    ProductBarcode,
    ProductImage,
    ProductSubstitute,
    RestockTask,
    ScanEvent,
    Shift,
    StripeEvent,
    SubscriptionStatus,
    User,
    UserRole,
    Warehouse,
    Worker,
    WorkerSession,
    utcnow,
)
from . import audit, email
from .audit import Actor
from .dashboard import tz_of

log = logging.getLogger("autorack.account")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _cell(v: Any) -> str:
    """CSV cell, safe to open in Excel (no formula injection), dates as ISO."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "value"):
        v = v.value
    s = str(v)
    return "'" + s if s and s[0] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv(zf: zipfile.ZipFile, name: str, header: list[str], rows: Iterable[Iterable[Any]]) -> int:
    n = 0
    with zf.open(name, "w") as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow([_cell(c) for c in row])
            n += 1
    return n


def export_zip(db: Session, wh: Warehouse) -> IO[bytes]:
    """Build the export in a temp file (spills to disk past 32 MB) and
    return it rewound. Photos are stored as-is; CSVs are UTF-8 for Excel."""
    out = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115 - returned to the caller
    wid = wh.id
    names = {w.id: w.name for w in db.scalars(select(Worker).where(Worker.warehouse_id == wid))}
    order_numbers = {
        oid: num
        for oid, num in db.execute(select(Order.id, Order.external_order_number).where(Order.warehouse_id == wid))
    }
    counts: dict[str, int] = {}
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        counts["orders"] = _csv(
            zf,
            "orders.csv",
            [
                "order_id",
                "order_number",
                "customer",
                "status",
                "source",
                "assigned_worker",
                "notes",
                "created_at",
                "started_at",
                "completed_at",
                "shipped_at",
                "carrier",
                "tracking_number",
                "shipped_by",
                "cancelled_at",
            ],
            (
                [
                    o.id,
                    o.external_order_number,
                    o.customer,
                    o.status,
                    o.source,
                    names.get(o.assigned_worker_id),
                    o.notes,
                    o.created_at,
                    o.started_at,
                    o.completed_at,
                    o.shipped_at,
                    o.carrier,
                    o.tracking_number,
                    names.get(o.shipped_by_worker_id),
                    o.cancelled_at,
                ]
                for o in db.scalars(select(Order).where(Order.warehouse_id == wid).order_by(Order.created_at))
            ),
        )
        counts["order_lines"] = _csv(
            zf,
            "order_lines.csv",
            [
                "order_id",
                "order_number",
                "line_no",
                "barcode",
                "sku",
                "description",
                "location",
                "quantity_expected",
                "quantity_scanned",
                "quantity_short",
            ],
            (
                [
                    li.order_id,
                    order_numbers.get(li.order_id),
                    li.line_no,
                    li.expected_barcode,
                    li.sku,
                    li.sku_description,
                    li.location,
                    li.expected_quantity,
                    li.scanned_quantity,
                    li.short_quantity,
                ]
                for li in db.scalars(
                    select(OrderLineItem)
                    .where(OrderLineItem.warehouse_id == wid)
                    .order_by(OrderLineItem.order_id, OrderLineItem.line_no)
                )
            ),
        )
        counts["scans"] = _csv(
            zf,
            "scans.csv",
            [
                "scan_id",
                "order_id",
                "order_number",
                "worker",
                "result",
                "scanned_barcode",
                "line_id",
                "intended_line_id",
                "match_tier",
                "phone_showed",
                "offline",
                "undoes_scan_id",
                "scanned_at",
                "received_at",
            ],
            (
                [
                    s.id,
                    s.order_id,
                    order_numbers.get(s.order_id),
                    names.get(s.worker_id),
                    s.result,
                    s.scanned_barcode,
                    s.line_item_id,
                    s.intended_line_item_id,
                    s.match_tier,
                    s.client_result,
                    s.was_offline,
                    s.voids_scan_id,
                    s.client_scanned_at,
                    s.received_at,
                ]
                for s in db.scalars(
                    select(ScanEvent)
                    .where(ScanEvent.warehouse_id == wid)
                    .order_by(ScanEvent.client_scanned_at)
                    .execution_options(yield_per=2000)
                )
            ),
        )
        counts["problems"] = _csv(
            zf,
            "problems.csv",
            [
                "flag_id",
                "order_id",
                "order_number",
                "line_id",
                "worker",
                "reason",
                "short_quantity",
                "short_reason",
                "note",
                "created_at",
                "resolved_at",
                "resolution",
                "resolution_note",
            ],
            (
                [
                    f.id,
                    f.order_id,
                    order_numbers.get(f.order_id),
                    f.line_item_id,
                    names.get(f.worker_id),
                    f.reason,
                    f.short_quantity,
                    f.short_reason,
                    f.note,
                    f.created_at,
                    f.resolved_at,
                    f.resolution,
                    f.resolution_note,
                ]
                for f in db.scalars(
                    select(OrderFlag).where(OrderFlag.warehouse_id == wid).order_by(OrderFlag.created_at)
                )
            ),
        )
        counts["workers"] = _csv(
            zf,
            "workers.csv",
            ["worker_id", "name", "active", "created_at", "privacy_notice_acknowledged_at"],
            (
                [w.id, w.name, w.active, w.created_at, w.notice_acknowledged_at]
                for w in db.scalars(select(Worker).where(Worker.warehouse_id == wid).order_by(Worker.name))
            ),
        )
        counts["phones"] = _csv(
            zf,
            "phones.csv",
            ["phone_id", "label", "browser", "linked_at", "last_seen_at", "unlinked_at"],
            (
                [d.id, d.label, d.user_agent, d.created_at, d.last_seen_at, d.revoked_at]
                for d in db.scalars(select(Device).where(Device.warehouse_id == wid).order_by(Device.created_at))
            ),
        )
        counts["team"] = _csv(
            zf,
            "team.csv",
            ["email", "name", "role", "active", "added_at", "last_sign_in"],
            (
                [u.email, u.name, m.role, m.active and u.active, m.created_at, u.last_login_at]
                for u, m in db.execute(
                    select(User, Membership)
                    .join(Membership, Membership.user_id == User.id)
                    .where(Membership.warehouse_id == wid)
                    .order_by(Membership.created_at)
                )
            ),
        )
        counts["barcode_aliases"] = _csv(
            zf,
            "barcode_aliases.csv",
            ["scanned_barcode", "counts_as", "note", "created_at"],
            (
                [a.alias_key, a.target_key, a.note, a.created_at]
                for a in db.scalars(select(BarcodeAlias).where(BarcodeAlias.warehouse_id == wid))
            ),
        )
        counts["products"] = _csv(
            zf,
            "products.csv",
            ["product_id", "sku", "barcode", "name", "location", "weight_grams", "packer_note", "active", "created_at"],
            (
                [p.id, p.sku, p.barcode, p.name, p.location, p.weight_grams, p.packer_note, p.active, p.created_at]
                for p in db.scalars(select(Product).where(Product.warehouse_id == wid).order_by(Product.name))
            ),
        )
        counts["product_barcodes"] = _csv(
            zf,
            "product_barcodes.csv",
            ["product_id", "barcode", "pack_qty", "label"],
            (
                [b.product_id, b.barcode, b.pack_qty, b.label]
                for b in db.scalars(select(ProductBarcode).where(ProductBarcode.warehouse_id == wid))
            ),
        )
        counts["kits"] = _csv(
            zf,
            "kit_components.csv",
            ["kit_product_id", "component_product_id", "quantity"],
            (
                [k.kit_id, k.component_id, k.quantity]
                for k in db.scalars(select(KitComponent).where(KitComponent.warehouse_id == wid))
            ),
        )
        counts["boxes"] = _csv(
            zf,
            "shipment_boxes.csv",
            ["order_id", "box", "tracking_number", "carrier", "worker_id", "labelled_at"],
            (
                [b.order_id, b.box_no, b.tracking_number, b.carrier, b.worker_id, b.created_at]
                for b in db.scalars(select(Package).where(Package.warehouse_id == wid).order_by(Package.created_at))
            ),
        )
        counts["shifts"] = _csv(
            zf,
            "time_clock.csv",
            ["worker_id", "clock_in", "clock_out", "closed_by", "edited_by_user_id"],
            (
                [t.worker_id, t.clock_in, t.clock_out, t.closed_by, t.edited_by_user_id]
                for t in db.scalars(select(Shift).where(Shift.warehouse_id == wid).order_by(Shift.clock_in))
            ),
        )
        counts["restock"] = _csv(
            zf,
            "restock_tasks.csv",
            ["location", "barcode", "sku", "description", "source", "status", "reported_at", "done_at"],
            (
                [t.location, t.barcode, t.sku, t.description, t.source, t.status, t.created_at, t.done_at]
                for t in db.scalars(select(RestockTask).where(RestockTask.warehouse_id == wid))
            ),
        )
        counts["clients"] = _csv(
            zf,
            "clients.csv",
            ["client_id", "name", "code", "contact_email", "active"],
            (
                [c.id, c.name, c.code, c.contact_email, c.active]
                for c in db.scalars(select(Client).where(Client.warehouse_id == wid))
            ),
        )
        counts["imports"] = _csv(
            zf,
            "imports.csv",
            ["import_id", "filename", "orders_created", "lines_created", "orders_skipped", "created_at"],
            (
                [b.id, b.filename, b.orders_created, b.lines_created, b.orders_skipped, b.created_at]
                for b in db.scalars(select(ImportBatch).where(ImportBatch.warehouse_id == wid))
            ),
        )
        counts["activity_log"] = _csv(
            zf,
            "activity_log.csv",
            ["at", "who", "who_type", "action", "target_type", "target_id", "details", "ip"],
            (
                [
                    a.created_at,
                    a.actor_label,
                    a.actor_type,
                    a.action,
                    a.target_type,
                    a.target_id,
                    json.dumps(a.details, default=str),
                    a.ip,
                ]
                for a in db.scalars(
                    select(AuditLog)
                    .where(AuditLog.warehouse_id == wid)
                    .order_by(AuditLog.id)
                    .execution_options(yield_per=2000)
                )
            ),
        )
        photos = 0
        for p in db.scalars(select(Photo).where(Photo.warehouse_id == wid).order_by(Photo.created_at)):
            ext = {"image/png": "png", "image/webp": "webp"}.get(p.content_type, "jpg")
            label = (order_numbers.get(p.order_id) or str(p.order_id)[:8]).replace("/", "-")
            zf.writestr(f"photos/{label}_{p.created_at:%Y%m%d-%H%M%S}_{str(p.id)[:8]}.{ext}", p.data)
            photos += 1
        counts["photos"] = photos
        sigs = 0
        for sig in db.scalars(select(AgreementSignature).where(AgreementSignature.warehouse_id == wid)):
            zf.writestr(
                f"license-agreement/{sig.agreement_version}_{sig.signed_at:%Y-%m-%d}_{str(sig.id)[:8]}.pdf",
                sig.signed_pdf,
            )
            sigs += 1
        counts["signed_agreements"] = sigs
        zf.writestr(
            "warehouse.json",
            json.dumps(
                {
                    "id": str(wh.id),
                    "name": wh.name,
                    "timezone": wh.timezone,
                    "billing_email": wh.owner_email,
                    "subscription_status": wh.subscription_status.value,
                    "created_at": wh.created_at.isoformat(),
                    "closed_at": wh.closed_at.isoformat() if wh.closed_at else None,
                    "deletion_due_at": wh.deletion_due_at.isoformat() if wh.deletion_due_at else None,
                    "exported_at": utcnow().isoformat(),
                    "counts": counts,
                },
                indent=2,
            ),
        )
        zf.writestr("README.txt", _readme(wh, counts))
    out.seek(0)
    return out


def _readme(wh: Warehouse, counts: dict[str, int]) -> str:
    lines = [
        f"Autorack data export: {wh.name}",
        f"Exported {utcnow():%Y-%m-%d %H:%M UTC}. Times in the CSV files are UTC (ISO 8601).",
        "",
        "Files:",
        f"  orders.csv          {counts['orders']} orders",
        f"  order_lines.csv     {counts['order_lines']} lines (expected, scanned and short quantities)",
        f"  scans.csv           {counts['scans']} scans: every scan, right or wrong, and every undo",
        f"  problems.csv        {counts['problems']} problems and short picks workers reported",
        f"  photos/             {counts['photos']} photos workers took",
        f"  workers.csv         {counts['workers']} workers (PINs are never exported)",
        f"  phones.csv          {counts['phones']} linked phones",
        f"  team.csv            {counts['team']} dashboard users",
        f"  barcode_aliases.csv {counts['barcode_aliases']} taught barcodes",
        f"  products.csv        {counts.get('products', 0)} catalog products (plus product_barcodes, kit_components)",
        f"  shipment_boxes.csv  {counts.get('boxes', 0)} labelled boxes (tracking numbers)",
        f"  time_clock.csv      {counts.get('shifts', 0)} shifts on the time clock",
        f"  restock_tasks.csv   {counts.get('restock', 0)} restock tasks",
        f"  clients.csv         {counts.get('clients', 0)} 3PL clients",
        f"  imports.csv         {counts['imports']} CSV imports",
        f"  activity_log.csv    {counts['activity_log']} activity log entries",
        f"  license-agreement/  {counts['signed_agreements']} signed agreement(s)",
        "  warehouse.json      settings and totals",
        "",
        "CSV files open in Excel, Numbers or Google Sheets. Cells that begin with = + - or @ are prefixed",
        "with an apostrophe so a spreadsheet never runs them as formulas.",
    ]
    return "\n".join(lines) + "\n"


def export_filename(wh: Warehouse) -> str:
    slug = "".join(ch for ch in wh.name if ch.isalnum() or ch in " -_").strip().replace(" ", "-")[:50] or "warehouse"
    return f"autorack-export-{slug}-{utcnow():%Y-%m-%d}.zip"


def stream(fh: IO[bytes], chunk: int = 256 * 1024) -> Iterator[bytes]:
    try:
        while data := fh.read(chunk):
            yield data
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# Close and reopen
# ---------------------------------------------------------------------------


def retention_days() -> int:
    return get_settings().account_retention_days


def owners(db: Session, wh: Warehouse) -> list[str]:
    emails = set(
        db.scalars(
            select(User.email)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.warehouse_id == wh.id,
                Membership.role == UserRole.owner,
                Membership.active.is_(True),
                User.active.is_(True),
            )
        )
    )
    if wh.owner_email:
        emails.add(wh.owner_email)
    return sorted(emails)


def app_url(path: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/app/{path}"


def _local_date(wh: Warehouse, when: datetime) -> str:
    local = when.astimezone(tz_of(wh))
    return f"{local:%A} {local.day} {local:%B %Y}"


def close(
    db: Session,
    wh: Warehouse,
    actor: Actor,
    *,
    reason: str | None = None,
    cancel_billing: Callable[[str], Any] | None = None,
) -> Warehouse:
    """Close now; delete later. Caller commits."""
    if wh.purged_at:
        raise conflict("account_deleted", "This account's data has already been deleted.")
    if wh.closed_at:
        return wh
    from . import billing  # billing imports this module's neighbours; keep the cycle out of import time

    if wh.stripe_subscription_id and wh.subscription_status in (
        SubscriptionStatus.active,
        SubscriptionStatus.trialing,
        SubscriptionStatus.past_due,
        SubscriptionStatus.unpaid,
        SubscriptionStatus.paused,
        SubscriptionStatus.incomplete,
    ):
        try:
            (cancel_billing or billing.gateway.cancel_subscription)(wh.stripe_subscription_id)
        except Exception:
            log.exception("Stripe cancel failed for %s", wh.id)
            raise ApiError(
                502, "billing_cancel_failed", "We couldn't cancel the subscription with Stripe. Try again in a minute."
            ) from None
        wh.subscription_status = SubscriptionStatus.canceled
    now = utcnow()
    wh.closed_at = now
    wh.closed_by = actor.label or actor.type
    wh.close_reason = (reason or "").strip()[:500] or None
    wh.deletion_due_at = now + timedelta(days=retention_days())
    # Sign every worker out: the floor stops now.
    db.execute(
        update(WorkerSession)
        .where(WorkerSession.warehouse_id == wh.id, WorkerSession.ended_at.is_(None))
        .values(ended_at=now)
    )
    audit.record(
        db,
        actor,
        "account.closed",
        warehouse_id=wh.id,
        target_type="warehouse",
        target_id=wh.id,
        reason=wh.close_reason,
        deletion_due_at=wh.deletion_due_at.isoformat(),
    )
    due = _local_date(wh, wh.deletion_due_at)
    for addr in owners(db, wh):
        with contextlib.suppress(email.EmailError):
            email.send(
                email.notice_email(
                    addr,
                    subject=f"Your Autorack account for {wh.name} is closed",
                    heading="Your account is closed",
                    lines=[
                        f"{wh.name} was closed by {wh.closed_by}. Phones can no longer scan, and any subscription "
                        "has been cancelled.",
                        f"Your data stays available until {due}. Download a full copy from Settings before then. "
                        "After that date it is permanently deleted.",
                        "Changed your mind? Reopen the account from Settings any time before that date.",
                    ],
                    button_label="Download your data",
                    url=app_url("#/settings"),
                    footer="Sent to the owners of this warehouse. Questions? Just reply.",
                )
            )
    return wh


def reopen(db: Session, wh: Warehouse, actor: Actor) -> Warehouse:
    if wh.purged_at:
        raise conflict("account_deleted", "This account's data has already been deleted and can't be reopened.")
    if not wh.closed_at:
        return wh
    wh.closed_at = None
    wh.closed_by = None
    wh.close_reason = None
    wh.deletion_due_at = None
    audit.record(db, actor, "account.reopened", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id)
    return wh


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


def purge(db: Session, wh: Warehouse, actor: Actor) -> dict[str, int]:
    """Delete everything a closed account holds, in one transaction.

    Kept: the warehouse row (renamed, as a tombstone) and its signed license
    agreements, which evidence the contract. People who belong to no other
    warehouse are deactivated, and deleted when nothing else refers to them.
    """
    if not wh.closed_at:
        raise bad_request("account_open", "Only a closed account can be deleted.")
    if wh.purged_at:
        return {}
    wid = wh.id
    db.execute(text("SET LOCAL autorack.purging = 'on'"))
    counts: dict[str, int] = {}

    def gone(name: str, stmt: Any) -> None:
        counts[name] = int(db.execute(stmt).rowcount or 0)  # type: ignore[attr-defined]

    gone("photos", delete(Photo).where(Photo.warehouse_id == wid))
    gone("insert_checks", delete(OrderInsertCheck).where(OrderInsertCheck.warehouse_id == wid))
    gone("boxes", delete(Package).where(Package.warehouse_id == wid))
    gone("restock_tasks", delete(RestockTask).where(RestockTask.warehouse_id == wid))
    gone("problems", delete(OrderFlag).where(OrderFlag.warehouse_id == wid))
    gone("scans", delete(ScanEvent).where(ScanEvent.warehouse_id == wid))
    gone("order_lines", delete(OrderLineItem).where(OrderLineItem.warehouse_id == wid))
    gone("orders", delete(Order).where(Order.warehouse_id == wid))
    gone("pick_batches", delete(PickBatch).where(PickBatch.warehouse_id == wid))
    gone("product_substitutes", delete(ProductSubstitute).where(ProductSubstitute.warehouse_id == wid))
    gone("kit_components", delete(KitComponent).where(KitComponent.warehouse_id == wid))
    gone("product_barcodes", delete(ProductBarcode).where(ProductBarcode.warehouse_id == wid))
    gone("product_images", delete(ProductImage).where(ProductImage.warehouse_id == wid))
    gone("pack_inserts", delete(PackInsert).where(PackInsert.warehouse_id == wid))
    gone("products", delete(Product).where(Product.warehouse_id == wid))
    db.execute(update(Membership).where(Membership.warehouse_id == wid).values(client_id=None))
    gone("clients", delete(Client).where(Client.warehouse_id == wid))
    gone("imports", delete(ImportBatch).where(ImportBatch.warehouse_id == wid))
    gone("connections", delete(Integration).where(Integration.warehouse_id == wid))
    gone("barcode_aliases", delete(BarcodeAlias).where(BarcodeAlias.warehouse_id == wid))
    gone("shifts", delete(Shift).where(Shift.warehouse_id == wid))
    gone("worker_sessions", delete(WorkerSession).where(WorkerSession.warehouse_id == wid))
    gone("workers", delete(Worker).where(Worker.warehouse_id == wid))
    gone("phones", delete(Device).where(Device.warehouse_id == wid))
    gone("notifications", delete(NotificationSent).where(NotificationSent.warehouse_id == wid))
    gone("usage", delete(FeatureUsage).where(FeatureUsage.warehouse_id == wid))
    gone("stripe_events", delete(StripeEvent).where(StripeEvent.warehouse_id == wid))
    gone("activity_log", delete(AuditLog).where(AuditLog.warehouse_id == wid))
    db.execute(update(OwnerSession).where(OwnerSession.warehouse_id == wid).values(warehouse_id=None))

    member_ids = list(db.scalars(select(Membership.user_id).where(Membership.warehouse_id == wid)))
    gone("memberships", delete(Membership).where(Membership.warehouse_id == wid))
    removed = deactivated = 0
    for uid in member_ids:
        user = db.get(User, uid)
        if user is None:
            continue
        elsewhere = db.scalar(select(Membership).where(Membership.user_id == uid, Membership.active.is_(True)))
        if elsewhere:
            if user.warehouse_id == wid:
                user.warehouse_id = elsewhere.warehouse_id
            continue
        db.execute(delete(MagicLinkToken).where(MagicLinkToken.user_id == uid))
        db.execute(delete(OwnerSession).where(OwnerSession.user_id == uid))
        try:
            with db.begin_nested():
                db.delete(user)
                db.flush()
            removed += 1
        except IntegrityError:
            # Still named on a signed agreement or another warehouse's
            # history: keep the row, but it can never sign in again.
            user = db.get(User, uid)
            if user is not None:
                user.active = False
                user.name = None
                if user.warehouse_id == wid:
                    user.warehouse_id = None
            deactivated += 1
    counts["people_deleted"] = removed
    counts["people_deactivated"] = deactivated

    wh.purged_at = utcnow()
    wh.name = f"Deleted warehouse {str(wid)[:8]}"
    wh.join_code = f"X{uuid.uuid4().hex[:15]}"
    wh.import_token = None
    wh.owner_email = ""
    wh.stripe_customer_id = None
    wh.stripe_subscription_id = None
    audit.record(db, actor, "account.purged", warehouse_id=None, target_type="warehouse", target_id=wid, counts=counts)
    return counts
