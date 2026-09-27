"""Batch picking: several small orders picked in one walk through the
warehouse, each into its own tote.

The server's part is small. A batch is a group of orders with a tote letter
each. The phone walks the combined list by location; every scan is still a
scan on one order (the first order in the batch that still needs that item),
so matching, undo, short picks and the proof all work exactly as for one
order. The batch closes by itself once none of its orders is left to pick.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import bad_request, not_found
from ..models import Order, OrderKind, OrderStatus, PickBatch, Warehouse, Worker, utcnow
from . import audit
from . import dashboard as dash
from . import orders as order_svc
from .audit import Actor

TOTES = "ABCDEFGHJKLM"  # no I: it reads as 1 on a tote
MAX_ORDERS = len(TOTES)
PICKABLE = (OrderStatus.pending, OrderStatus.in_progress)
DONE = (OrderStatus.completed, OrderStatus.shipped, OrderStatus.cancelled)


def get(db: Session, wh: Warehouse, batch_id: uuid.UUID) -> PickBatch:
    b = db.scalar(select(PickBatch).where(PickBatch.id == batch_id, PickBatch.warehouse_id == wh.id))
    if not b:
        raise not_found("Batch not found.")
    return b


def orders_of(db: Session, batch: PickBatch) -> list[Order]:
    return list(db.scalars(select(Order).where(Order.batch_id == batch.id).order_by(Order.tote, Order.created_at)))


def create(
    db: Session, wh: Warehouse, order_ids: list[uuid.UUID], worker_id: uuid.UUID | None, actor: Actor
) -> PickBatch:
    ids = list(dict.fromkeys(order_ids))
    if len(ids) < 2:
        raise bad_request("batch_too_small", "Pick at least 2 orders to batch.")
    if len(ids) > MAX_ORDERS:
        raise bad_request("batch_too_large", f"A batch holds at most {MAX_ORDERS} orders (one per tote).")
    if worker_id is not None:
        w = db.get(Worker, worker_id)
        if not w or w.warehouse_id != wh.id or not w.active:
            raise bad_request("worker_invalid", "That worker isn't on this warehouse's team.")
    found = {
        o.id: o
        for o in db.scalars(select(Order).where(Order.id.in_(ids), Order.warehouse_id == wh.id).with_for_update())
    }
    if len(found) != len(ids):
        raise not_found("Some of those orders no longer exist.")
    for o in found.values():
        name = o.external_order_number or str(o.id)[:8]
        if o.kind != OrderKind.pick:
            raise bad_request("batch_not_pick", f"{name} isn't a picking order.")
        if o.status not in PICKABLE:
            state = o.status.value.replace("_", " ")
            raise bad_request("batch_not_open", f"{name} is {state}, not waiting to be picked.")
        if o.batch_id and not _closed(db, o.batch_id):
            raise bad_request("batch_already", f"{name} is already in another batch.")
    n = (db.scalar(select(func.count()).select_from(PickBatch).where(PickBatch.warehouse_id == wh.id)) or 0) + 1
    batch = PickBatch(
        warehouse_id=wh.id,
        number=f"B{n:04d}",
        assigned_worker_id=worker_id,
        created_by_user_id=uuid.UUID(actor.id) if actor.type == "user" and actor.id else None,
    )
    db.add(batch)
    db.flush()
    for tote, oid in zip(TOTES, ids, strict=False):
        o = found[oid]
        o.batch_id, o.tote = batch.id, tote
        if worker_id is not None:
            o.assigned_worker_id = worker_id
        order_svc.bump(o)
    audit.record(
        db, actor, "batch.created", warehouse_id=wh.id, target_type="batch", target_id=batch.id, orders=len(ids)
    )
    return batch


def _closed(db: Session, batch_id: uuid.UUID) -> bool:
    b = db.get(PickBatch, batch_id)
    return b is None or b.closed_at is not None


def close_if_done(db: Session, batch: PickBatch) -> bool:
    if batch.closed_at:
        return True
    left = db.scalar(
        select(func.count()).select_from(Order).where(Order.batch_id == batch.id, Order.status.not_in(DONE))
    )
    if not left:
        batch.closed_at = utcnow()
        return True
    return False


def release(db: Session, wh: Warehouse, batch: PickBatch, actor: Actor) -> None:
    """Break the batch up: its unfinished orders go back to the normal list."""
    for o in orders_of(db, batch):
        if o.status not in DONE:
            o.batch_id, o.tote = None, None
            order_svc.bump(o)
    batch.closed_at = batch.closed_at or utcnow()
    audit.record(db, actor, "batch.released", warehouse_id=wh.id, target_type="batch", target_id=batch.id)


def open_batches(db: Session, wh: Warehouse, worker_id: uuid.UUID | None = None) -> list[PickBatch]:
    stmt = select(PickBatch).where(PickBatch.warehouse_id == wh.id, PickBatch.closed_at.is_(None))
    if worker_id is not None:
        stmt = stmt.where((PickBatch.assigned_worker_id.is_(None)) | (PickBatch.assigned_worker_id == worker_id))
    out = []
    for b in db.scalars(stmt.order_by(PickBatch.created_at)):
        if not close_if_done(db, b):
            out.append(b)
    return out


def batch_dict(db: Session, wh: Warehouse, batch: PickBatch, orders: list[Order] | None = None) -> dict[str, Any]:
    orders = orders if orders is not None else orders_of(db, batch)
    rows = dash.order_rows(db, wh, orders)
    names = dash.worker_names(db, wh.id)
    for r, o in zip(rows, orders, strict=True):
        r["tote"] = o.tote
        r["version"] = o.version
    return {
        "id": str(batch.id),
        "number": batch.number,
        "assigned_worker_id": str(batch.assigned_worker_id) if batch.assigned_worker_id else None,
        "assigned_worker": names.get(batch.assigned_worker_id) if batch.assigned_worker_id else None,
        "created_at": batch.created_at.isoformat(),
        "closed_at": batch.closed_at.isoformat() if batch.closed_at else None,
        "order_count": len(orders),
        "orders_left": sum(1 for o in orders if o.status not in DONE),
        "units_expected": sum(r.get("units_expected") or 0 for r in rows),
        "units_scanned": sum(r.get("units_scanned") or 0 for r in rows),
        "orders": rows,
    }
