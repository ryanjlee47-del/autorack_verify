"""Receiving, returns and cycle counts: the jobs that tally what's there.

They're orders with a different `kind`, so they reuse everything a pick has
(the phone's offline scanning, the matching engine, flags and photos, the
append-only scan history). What differs:

* every scan of a listed item counts, even past the expected quantity, and a
  scan of something not on the list is recorded as an extra (not refused);
* the worker finishes the task; nothing auto-completes;
* the result is a variance: expected vs counted per line, plus the extras.
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..errors import bad_request, conflict
from ..models import (
    TALLY_KINDS,
    Order,
    OrderKind,
    OrderSource,
    OrderStatus,
    Package,
    ScanEvent,
    ScanResult,
    Warehouse,
    Worker,
)
from . import audit
from . import orders as order_svc
from .audit import Actor

KIND_LABELS = {
    OrderKind.pick: "Order",
    OrderKind.receive: "Receiving",
    OrderKind.ret: "Return",
    OrderKind.count: "Count",
}


def variance(db: Session, order: Order) -> dict[str, Any]:
    """Expected vs counted, line by line, and what turned up that wasn't
    on the list."""
    lines = order_svc.lines_for(db, order.id)
    rows = []
    for li in lines:
        diff = li.scanned_quantity - li.expected_quantity
        rows.append(
            {
                "line_item_id": str(li.id),
                "barcode": li.expected_barcode,
                "sku": li.sku,
                "description": li.sku_description,
                "location": li.location,
                "expected": li.expected_quantity,
                "counted": li.scanned_quantity,
                "difference": diff,
                "state": "ok" if diff == 0 else ("over" if diff > 0 else "short"),
            }
        )
    voided = select(ScanEvent.voids_scan_id).where(ScanEvent.order_id == order.id, ScanEvent.voids_scan_id.is_not(None))
    extras = [
        {"barcode": code, "counted": int(n)}
        for code, n in db.execute(
            select(ScanEvent.scanned_barcode, func.count())
            .where(
                ScanEvent.order_id == order.id,
                ScanEvent.result == ScanResult.extra,
                ScanEvent.id.not_in(voided),
            )
            .group_by(ScanEvent.scanned_barcode)
            .order_by(func.count().desc())
        )
    ]
    finished_by = db.get(Worker, order.finished_by_worker_id) if order.finished_by_worker_id else None
    return {
        "lines": rows,
        "extras": extras,
        "totals": {
            "expected": sum(r["expected"] for r in rows),
            "counted": sum(r["counted"] for r in rows),
            "extra": sum(e["counted"] for e in extras),
            "lines_short": sum(1 for r in rows if r["state"] == "short"),
            "lines_over": sum(1 for r in rows if r["state"] == "over"),
            "lines_ok": sum(1 for r in rows if r["state"] == "ok"),
        },
        "matches": all(r["state"] == "ok" for r in rows) and not extras,
        "finished": order.completed_at is not None,
        "finished_at": order.completed_at.isoformat() if order.completed_at else None,
        "finished_by": finished_by.name if finished_by else None,
    }


def variance_csv(db: Session, order: Order) -> str:
    v = variance(db, order)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["barcode", "sku", "description", "location", "expected", "counted", "difference", "state"])
    for r in v["lines"]:
        w.writerow(
            [
                r["barcode"],
                r["sku"] or "",
                r["description"] or "",
                r["location"] or "",
                r["expected"],
                r["counted"],
                r["difference"],
                r["state"],
            ]
        )
    for e in v["extras"]:
        w.writerow([e["barcode"], "", "NOT ON THE LIST", "", 0, e["counted"], e["counted"], "extra"])
    return buf.getvalue()


def find_returnable(db: Session, wh: Warehouse, code: str) -> Order | None:
    """The shipped order a return is for: by order number, tracking number,
    or the pick sheet's QR."""
    code = (code or "").strip()
    if not code:
        return None
    base = select(Order).where(
        Order.warehouse_id == wh.id,
        Order.kind == OrderKind.pick,
        Order.status.in_([OrderStatus.shipped, OrderStatus.completed]),
    )
    oid = order_svc.parse_order_qr(code)
    if oid:
        return db.scalar(base.where(Order.id == oid))
    hit = db.scalar(base.where(func.upper(Order.external_order_number) == code.upper().lstrip("#")))
    if hit:
        return hit
    tracking = order_svc.normalize_tracking(code)
    if len(tracking) < 8:
        return None
    in_box = select(Package.order_id).where(Package.warehouse_id == wh.id, Package.tracking_number == tracking)
    return db.scalar(base.where(or_(Order.tracking_number == tracking, Order.id.in_(in_box))).limit(1))


def create_return(db: Session, wh: Warehouse, original: Order, actor: Actor, user_id: uuid.UUID | None = None) -> Order:
    """A return task listing what was actually shipped on `original`."""
    if original.kind != OrderKind.pick or original.status not in (OrderStatus.shipped, OrderStatus.completed):
        raise bad_request("not_returnable", "Only a picked or shipped order can be returned.")
    open_return = db.scalar(
        select(Order).where(
            Order.return_of_order_id == original.id,
            Order.status.in_([OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]),
        )
    )
    if open_return:
        return open_return  # carry on with the one already started
    shipped = [li for li in order_svc.lines_for(db, original.id) if li.scanned_quantity > 0]
    if not shipped:
        raise bad_request("nothing_shipped", "Nothing was scanned out on that order, so there's nothing to return.")
    base = f"RET-{original.external_order_number or str(original.id)[:8]}"[:90]
    number = base
    n = 1
    while order_svc.order_number_in_use(db, wh.id, number):
        n += 1
        number = f"{base}-{n}"
    order = order_svc.create_order(
        db,
        wh,
        external_order_number=number,
        lines=[
            order_svc.LineInput(
                li.expected_barcode,
                li.scanned_quantity,
                li.sku,
                li.sku_description,
                li.location,
                # What came back gets the same traceability as what went out.
                track_lot=li.track_lot,
                track_serial=li.track_serial,
                track_expiry=li.track_expiry,
            )
            for li in shipped
        ],
        customer=original.customer,
        source=OrderSource.manual,
        created_by_user_id=user_id,
        kind=OrderKind.ret,
        return_of_order_id=original.id,
        notes=f"Return of order {original.external_order_number or original.id}",
        actor=actor,
    )
    return order


def reopen(db: Session, order: Order, actor: Actor) -> None:
    if order.kind not in TALLY_KINDS:
        raise bad_request("not_for_task", "Only receiving, returns and counts can be reopened.")
    if order.status == OrderStatus.cancelled:
        raise conflict("order_cancelled", "This was cancelled.")
    if order.completed_at is None:
        return
    order.completed_at = None
    order.finished_by_worker_id = None
    order_svc.recompute_status(db, order)
    order_svc.bump(order)
    audit.record(db, actor, "task.reopened", warehouse_id=order.warehouse_id, target_type="order", target_id=order.id)


def returns_of(db: Session, order: Order) -> list[dict[str, Any]]:
    return [
        {"id": str(o.id), "number": o.external_order_number, "status": o.status.value}
        for o in db.scalars(select(Order).where(Order.return_of_order_id == order.id).order_by(Order.created_at))
    ]
