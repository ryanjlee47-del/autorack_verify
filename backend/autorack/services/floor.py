"""Things on the floor around the pick itself: pack inserts, restocking empty
bins, and the time clock."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import matching
from ..models import (
    Order,
    OrderInsertCheck,
    OrderLineItem,
    PackInsert,
    RestockTask,
    Shift,
    Warehouse,
    utcnow,
)

# ---------------------------------------------------------------------------
# Pack inserts
# ---------------------------------------------------------------------------


def inserts_for(db: Session, order: Order, lines: list[OrderLineItem] | None = None) -> list[PackInsert]:
    """The inserts this order's box needs: every-order ones, its client's,
    and any tied to a product on the order."""
    if order.kind.value != "pick":
        return []
    if lines is None:
        lines = list(db.scalars(select(OrderLineItem).where(OrderLineItem.order_id == order.id)))
    product_ids = {li.product_id for li in lines if li.product_id} | {
        li.kit_product_id for li in lines if li.kit_product_id
    }
    stmt = select(PackInsert).where(PackInsert.warehouse_id == order.warehouse_id, PackInsert.active.is_(True))
    rows = db.scalars(stmt.order_by(PackInsert.created_at))
    return [
        i
        for i in rows
        if (i.client_id is None or i.client_id == order.client_id)
        and (i.product_id is None or i.product_id in product_ids)
    ]


def insert_checks(db: Session, order_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.scalars(select(OrderInsertCheck.insert_id).where(OrderInsertCheck.order_id == order_id)))


def missing_inserts(db: Session, order: Order, lines: list[OrderLineItem]) -> list[PackInsert]:
    done = insert_checks(db, order.id)
    return [i for i in inserts_for(db, order, lines) if i.id not in done]


def insert_dict(i: PackInsert) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "name": i.name,
        "barcode": i.barcode,
        "scan_required": i.scan_required,
        "client_id": str(i.client_id) if i.client_id else None,
        "product_id": str(i.product_id) if i.product_id else None,
        "active": i.active,
    }


def normalized(code: str | None) -> str | None:
    k = matching.normalized_key(code or "")
    return k[:200] or None


# ---------------------------------------------------------------------------
# Restocking
# ---------------------------------------------------------------------------


def report_empty(
    db: Session,
    wh: Warehouse,
    line: OrderLineItem,
    *,
    task_id: uuid.UUID | None = None,
    source: str = "worker",
    worker_id: uuid.UUID | None = None,
    note: str | None = None,
) -> tuple[RestockTask, bool]:
    """A bin is empty (or ran short). One open task per bin and item: a second
    report of the same bin returns the task already open."""
    key = line.normalized_barcode or normalized(line.expected_barcode)
    existing = db.scalar(
        select(RestockTask).where(
            RestockTask.warehouse_id == wh.id,
            RestockTask.status == "open",
            RestockTask.normalized_barcode == key,
            (RestockTask.location == line.location) if line.location else RestockTask.location.is_(None),
        )
    )
    if existing:
        return existing, False
    task = RestockTask(
        id=task_id or uuid.uuid4(),
        warehouse_id=wh.id,
        location=line.location,
        barcode=line.expected_barcode,
        normalized_barcode=key,
        sku=line.sku,
        description=line.sku_description,
        product_id=line.product_id,
        order_id=line.order_id,
        source=source,
        reported_by_worker_id=worker_id,
        note=(note or "").strip()[:500] or None,
        status="open",
    )
    db.add(task)
    db.flush()
    return task, True


def restock_dict(t: RestockTask, workers: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "location": t.location,
        "barcode": t.barcode,
        "sku": t.sku,
        "description": t.description,
        "product_id": str(t.product_id) if t.product_id else None,
        "order_id": str(t.order_id) if t.order_id else None,
        "source": t.source,
        "reported_by": workers.get(t.reported_by_worker_id) if t.reported_by_worker_id else None,
        "note": t.note,
        "status": t.status,
        "created_at": t.created_at.isoformat(),
        "done_at": t.done_at.isoformat() if t.done_at else None,
        "done_by": workers.get(t.done_by_worker_id) if t.done_by_worker_id else None,
    }


def open_restock(db: Session, wh: Warehouse, limit: int = 200) -> list[RestockTask]:
    return list(
        db.scalars(
            select(RestockTask)
            .where(RestockTask.warehouse_id == wh.id, RestockTask.status == "open")
            .order_by(RestockTask.location.nulls_last(), RestockTask.created_at)
            .limit(limit)
        )
    )


# ---------------------------------------------------------------------------
# Time clock
# ---------------------------------------------------------------------------

# A shift nobody clocked out of is closed after this long.
MAX_SHIFT = timedelta(hours=14)


def open_shift(db: Session, worker_id: uuid.UUID) -> Shift | None:
    return db.scalar(select(Shift).where(Shift.worker_id == worker_id, Shift.clock_out.is_(None)))


def shift_dict(s: Shift, workers: dict[uuid.UUID, str] | None = None) -> dict[str, Any]:
    end = s.clock_out or utcnow()
    return {
        "id": str(s.id),
        "worker_id": str(s.worker_id),
        "worker": workers.get(s.worker_id) if workers else None,
        "clock_in": s.clock_in.isoformat(),
        "clock_out": s.clock_out.isoformat() if s.clock_out else None,
        "hours": round((end - s.clock_in).total_seconds() / 3600, 2),
        "closed_by": s.closed_by,
        "edited": s.edited_by_user_id is not None,
    }


def hours_between(db: Session, wh_id: uuid.UUID, start: datetime, end: datetime) -> dict[uuid.UUID, float]:
    """Hours on the clock per worker inside [start, end)."""
    now = utcnow()
    out: dict[uuid.UUID, float] = {}
    for s in db.scalars(
        select(Shift).where(
            Shift.warehouse_id == wh_id,
            Shift.clock_in < end,
            or_(Shift.clock_out.is_(None), Shift.clock_out > start),
        )
    ):
        a = max(s.clock_in, start)
        b = min(s.clock_out or now, end)
        if b > a:
            out[s.worker_id] = out.get(s.worker_id, 0.0) + (b - a).total_seconds() / 3600
    return out


def close_stale_shifts(db: Session) -> int:
    """Clock out shifts left open past MAX_SHIFT, at their last scan (or at
    the limit). Marked 'auto' so a manager can see and fix them."""
    from ..models import ScanEvent

    now = utcnow()
    n = 0
    for s in db.scalars(select(Shift).where(Shift.clock_out.is_(None), Shift.clock_in < now - MAX_SHIFT)):
        last = db.scalar(
            select(ScanEvent.client_scanned_at)
            .where(ScanEvent.worker_id == s.worker_id, ScanEvent.client_scanned_at >= s.clock_in)
            .order_by(ScanEvent.client_scanned_at.desc())
            .limit(1)
        )
        s.clock_out = min(last, s.clock_in + MAX_SHIFT) if last else s.clock_in + MAX_SHIFT
        s.closed_by = "auto"
        n += 1
    return n
