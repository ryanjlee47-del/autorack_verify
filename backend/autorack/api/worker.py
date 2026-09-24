"""The worker PWA's API: link a phone, sign in by PIN, fetch orders, sync scans.

Authentication is two-layered:
  * `X-Device-Token` -- the phone, linked once to a warehouse via its setup code.
  * `Authorization: Bearer <worker session>` -- the person holding it, by PIN.

Sync needs only the device token. Each queued event names the worker session
it was made under, so scans made offline before a session expired, or by the
previous worker on a shared phone, are still attributed to whoever made them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import (
    DeviceContext,
    WorkerContext,
    client_ip,
    current_device,
    current_worker,
    require_worker_access,
)
from ..errors import ApiError, bad_request, not_found
from ..models import FlagReason, Order, OrderFlag, OrderStatus, ScanEvent, ScanResult, Warehouse, utcnow
from ..security import is_valid_pin, normalize_join_code
from ..services import audit, scans
from ..services import auth as auth_svc
from ..services import dashboard as dash
from ..services import orders as order_svc
from ..services.access import evaluate
from ..services.ratelimit import memory_limiter

router = APIRouter(prefix="/worker", tags=["worker"])

OPEN_STATUSES = [OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]


class LinkIn(BaseModel):
    join_code: str = Field(min_length=4, max_length=20)
    label: str = Field(default="Phone", max_length=100)


class PinIn(BaseModel):
    pin: str = Field(min_length=1, max_length=12)


def _access_dict(dctx: DeviceContext) -> dict[str, Any]:
    acc = evaluate(dctx.warehouse)
    return {"allowed": acc.allowed, "state": acc.state, "message": acc.message}


@router.post("/link")
def link(body: LinkIn, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    token, device = auth_svc.link_device(
        db, normalize_join_code(body.join_code), body.label, client_ip(request), request.headers.get("user-agent")
    )
    wh = db.get(Warehouse, device.warehouse_id)
    assert wh is not None
    return {
        "device_token": token,
        "device": {"id": str(device.id), "label": device.label},
        "warehouse": {"id": str(wh.id), "name": wh.name},
    }


@router.get("/device")
def device_info(dctx: DeviceContext = Depends(current_device)) -> dict[str, Any]:
    return {
        "device": {"id": str(dctx.device.id), "label": dctx.device.label},
        "warehouse": {"id": str(dctx.warehouse.id), "name": dctx.warehouse.name},
        "access": _access_dict(dctx),
        "pin_length": get_settings().pin_length,
        "server_time": utcnow().isoformat(),
    }


@router.post("/login")
def login(body: PinIn, dctx: DeviceContext = Depends(current_device), db: Session = Depends(get_db)) -> dict[str, Any]:
    acc = evaluate(dctx.warehouse)
    if not acc.allowed:
        raise ApiError(402, "subscription_inactive", acc.message, state=acc.state)
    if not is_valid_pin(body.pin):
        raise bad_request("pin_invalid", "Enter your 4-digit PIN.")
    token, sess, worker = auth_svc.worker_login(db, dctx.device, body.pin, dctx.ip)
    return {
        "session_token": token,
        "session_id": str(sess.id),
        "expires_at": sess.expires_at.isoformat(),
        "worker": {"id": str(worker.id), "name": worker.name},
    }


@router.post("/logout")
def logout(ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)) -> dict[str, Any]:
    ctx.session.ended_at = utcnow()
    audit.record(
        db, ctx.actor, "worker.logout", warehouse_id=ctx.warehouse.id, target_type="device", target_id=ctx.device.id
    )
    db.commit()
    return {"ok": True}


@router.get("/orders")
def open_orders(ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    orders = list(
        db.scalars(
            select(Order)
            .where(
                Order.warehouse_id == ctx.warehouse.id,
                Order.status.in_(OPEN_STATUSES),
                or_(Order.assigned_worker_id.is_(None), Order.assigned_worker_id == ctx.worker.id),
            )
            .order_by(
                case((Order.assigned_worker_id == ctx.worker.id, 0), else_=1),
                case((Order.status == OrderStatus.in_progress, 0), (Order.status == OrderStatus.flagged, 1), else_=2),
                Order.created_at,
            )
            .limit(200)
        )
    )
    rows = dash.order_rows(db, ctx.warehouse, orders)
    for r, o in zip(rows, orders, strict=True):
        r["version"] = o.version
        r["assigned_to_me"] = o.assigned_worker_id == ctx.worker.id
    return {"orders": rows, "server_time": utcnow().isoformat()}


@router.get("/orders/lookup")
def lookup_order(
    code: str = Query(..., min_length=1, max_length=200),
    ctx: WorkerContext = Depends(require_worker_access),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Find an order from whatever the worker scanned on the pick sheet:
    our own order QR, or the order-number barcode their WMS printed."""
    order_id = order_svc.parse_order_qr(code)
    stmt = select(Order).where(Order.warehouse_id == ctx.warehouse.id)
    if order_id:
        order = db.scalar(stmt.where(Order.id == order_id))
    else:
        text = code.strip()
        order = db.scalar(
            stmt.where(
                func.upper(Order.external_order_number) == text.upper(), Order.status != OrderStatus.cancelled
            ).order_by(Order.created_at.desc())
        )
    if not order:
        raise not_found("No order matches that code.")
    if order.status == OrderStatus.cancelled:
        raise bad_request("order_cancelled", "That order was cancelled.")
    return {"order_id": str(order.id), "status": order.status.value}


@router.get("/orders/{order_id}")
def order_payload(
    order_id: uuid.UUID, ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = order_svc.get_order(db, ctx.warehouse.id, order_id)
    return order_svc.offline_payload(db, ctx.warehouse, order)


class SyncEventIn(BaseModel):
    id: uuid.UUID
    kind: Literal["scan", "void", "flag"] = "scan"
    order_id: uuid.UUID
    session_id: uuid.UUID
    client_scanned_at: datetime
    client_seq: int | None = Field(default=None, ge=0)
    scanned_barcode: str | None = Field(default=None, max_length=500)
    intended_line_item_id: uuid.UUID | None = None
    client_result: str | None = Field(default=None, max_length=32)
    offline: bool = False
    target_scan_id: uuid.UUID | None = None
    line_item_id: uuid.UUID | None = None
    scan_event_id: uuid.UUID | None = None
    reason: FlagReason | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_kind(self) -> SyncEventIn:
        if self.kind == "scan" and not self.scanned_barcode:
            raise ValueError("scan events need scanned_barcode")
        if self.kind == "void" and not self.target_scan_id:
            raise ValueError("void events need target_scan_id")
        if self.kind == "flag" and not self.reason:
            raise ValueError("flag events need reason")
        return self


class SyncIn(BaseModel):
    events: list[SyncEventIn] = Field(max_length=500)


@router.post("/sync")
def sync(body: SyncIn, dctx: DeviceContext = Depends(current_device), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    memory_limiter.check(f"sync:{dctx.device.id}", s.sync_requests_per_minute, 60, "Syncing too fast. Slow down.")
    if len(body.events) > s.max_sync_events:
        raise bad_request("batch_too_large", f"Send at most {s.max_sync_events} events per request.")
    events = [scans.SyncEvent(**e.model_dump()) for e in body.events]
    outcome = scans.sync(db, dctx.device, dctx.warehouse, events)
    return {
        "events": [e.as_dict() for e in outcome.events],
        "orders": outcome.orders,
        "server_time": utcnow().isoformat(),
        "access": _access_dict(dctx),
    }


@router.get("/summary")
def shift_summary(ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Counts for this sign-in session. Counts only, no money: the phone
    belongs to the person being measured."""
    counts = dict.fromkeys((r.value for r in ScanResult), 0)
    for result, n in db.execute(
        select(ScanEvent.result, func.count())
        .where(ScanEvent.worker_session_id == ctx.session.id)
        .group_by(ScanEvent.result)
    ):
        counts[result.value] = n
    orders_done = (
        db.scalar(
            select(func.count(func.distinct(ScanEvent.order_id)))
            .join(Order, Order.id == ScanEvent.order_id)
            .where(ScanEvent.worker_session_id == ctx.session.id, Order.status == OrderStatus.completed)
        )
        or 0
    )
    flags = (
        db.scalar(
            select(func.count())
            .select_from(OrderFlag)
            .where(OrderFlag.worker_id == ctx.worker.id, OrderFlag.created_at >= ctx.session.created_at)
        )
        or 0
    )
    return {
        "worker": ctx.worker.name,
        "since": ctx.session.created_at.isoformat(),
        "units_picked": counts["match"] - counts["void"],
        "errors_caught": counts["mismatch"] + counts["over_pick"],
        "needs_review": counts["review"],
        "orders_completed": orders_done,
        "flags": flags,
    }
