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
from datetime import UTC, date, datetime
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
from ..models import (
    UNIT_RESULTS,
    FlagReason,
    Order,
    OrderFlag,
    OrderKind,
    OrderStatus,
    Photo,
    RestockTask,
    ScanEvent,
    ScanResult,
    Shift,
    ShortReason,
    Warehouse,
    Worker,
    utcnow,
)
from ..security import is_valid_pin, normalize_join_code
from ..services import audit, batches, catalog, floor, scans, tasks, training, usage
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
        "require_ship_scan": dctx.warehouse.require_ship_scan,
        "require_pack_photo": dctx.warehouse.require_pack_photo,
        "time_clock_enabled": dctx.warehouse.time_clock_enabled,
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
        "notice_required": worker.notice_version != auth_svc.WORKER_NOTICE_VERSION,
        "notice_version": auth_svc.WORKER_NOTICE_VERSION,
    }


class NoticeAck(BaseModel):
    version: str = Field(max_length=16)


@router.post("/notice")
def acknowledge_notice(
    body: NoticeAck, ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """The worker read what Autorack records about them (their name, every
    scan, problem reports and photos) and who can see it."""
    if body.version != auth_svc.WORKER_NOTICE_VERSION:
        raise bad_request("notice_outdated", "The notice changed. Reload to read the current one.")
    if ctx.worker.notice_version != body.version:
        ctx.worker.notice_version = body.version
        ctx.worker.notice_acknowledged_at = utcnow()
        audit.record(
            db,
            ctx.actor,
            "worker.notice_acknowledged",
            warehouse_id=ctx.warehouse.id,
            target_type="worker",
            target_id=ctx.worker.id,
            version=body.version,
            device=str(ctx.device.id),
        )
        db.commit()
    return {"ok": True, "acknowledged_at": ctx.worker.notice_acknowledged_at.isoformat()}


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
            .limit(500)
        )
    )
    # Rush first, then this worker's, then by when they have to ship.
    far = datetime.max.replace(tzinfo=UTC)
    due = {o.id: order_svc.due_at(o, ctx.warehouse) or far for o in orders}
    orders.sort(key=lambda o: (not o.rush, o.assigned_worker_id != ctx.worker.id, due[o.id]))
    orders = orders[:200]
    open_batches = batches.open_batches(db, ctx.warehouse, ctx.worker.id)
    batched = {b.id for b in open_batches}
    # A batched order is picked from its batch, not on its own.
    orders = [o for o in orders if o.batch_id not in batched]
    rows = dash.order_rows(db, ctx.warehouse, orders)
    for r, o in zip(rows, orders, strict=True):
        r["version"] = o.version
        r["assigned_to_me"] = o.assigned_worker_id == ctx.worker.id
        if o.blind:
            r["units_expected"] = 0
    to_ship: list[dict[str, Any]] = []
    if ctx.warehouse.require_ship_scan:
        # Picked but no label scanned yet: the packing bench's queue.
        ready = list(
            db.scalars(
                select(Order)
                .where(
                    Order.warehouse_id == ctx.warehouse.id,
                    Order.status == OrderStatus.completed,
                    Order.kind == OrderKind.pick,
                )
                .order_by(Order.completed_at)
                .limit(100)
            )
        )
        to_ship = dash.order_rows(db, ctx.warehouse, ready)
        for r, o in zip(to_ship, ready, strict=True):
            r["version"] = o.version
    batch_rows = []
    for b in open_batches:
        d = batches.batch_dict(db, ctx.warehouse, b)
        d.pop("orders")
        d["assigned_to_me"] = b.assigned_worker_id == ctx.worker.id
        batch_rows.append(d)
    db.commit()
    shift = floor.open_shift(db, ctx.worker.id)
    return {
        "orders": rows,
        "batches": batch_rows,
        "restock_open": len(floor.open_restock(db, ctx.warehouse)),
        "time_clock_enabled": ctx.warehouse.time_clock_enabled,
        "shift": floor.shift_dict(shift) if shift else None,
        "to_ship": to_ship,
        "require_ship_scan": ctx.warehouse.require_ship_scan,
        "server_time": utcnow().isoformat(),
    }


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
    if order.status == OrderStatus.shipped:
        raise bad_request("order_shipped", f"That order already shipped (tracking {order.tracking_number}).")
    return {"order_id": str(order.id), "status": order.status.value}


class ReturnStart(BaseModel):
    code: str = Field(min_length=1, max_length=200)


@router.post("/returns", status_code=201)
def start_return(
    body: ReturnStart, ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """A parcel came back: find what shipped (order number, tracking number
    or the pick-sheet QR) and open a return task listing it."""
    original = tasks.find_returnable(db, ctx.warehouse, body.code)
    if not original:
        raise not_found("No shipped order matches that. Try the order number or the tracking number on the label.")
    ret = tasks.create_return(db, ctx.warehouse, original, ctx.actor)
    usage.track(db, ctx.warehouse.id, "tasks.return")
    db.commit()
    return {"order_id": str(ret.id), "number": ret.external_order_number}


@router.get("/orders/{order_id}")
def order_payload(
    order_id: uuid.UUID, ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = order_svc.get_order(db, ctx.warehouse.id, order_id)
    return order_svc.offline_payload(db, ctx.warehouse, order)


# ---------------------------------------------------------------------------
# Practice (training mode)
# ---------------------------------------------------------------------------


@router.post("/practice")
def practice_order(ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)) -> dict[str, Any]:
    """A practice order from this warehouse's real products. Nothing is
    stored: the phone checks practice scans itself and keeps them local."""
    memory_limiter.check(f"practice:{ctx.worker.id}", 30, 3600, "That's a lot of practice. Take a break!")
    return training.practice_order(db, ctx.warehouse, ctx.worker)


class PracticeResult(BaseModel):
    units: int = Field(ge=0, le=1000)
    scans: int = Field(ge=0, le=5000)
    mistakes: int = Field(ge=0, le=5000)
    seconds: int = Field(ge=0, le=24 * 3600)


@router.post("/practice/result", status_code=201)
def practice_result(
    body: PracticeResult, ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """How a practice round went, for the Workers page."""
    memory_limiter.check(f"practice-result:{ctx.worker.id}", 30, 3600, "Too many practice results.")
    run = training.record(db, ctx.warehouse, ctx.worker, body.units, body.scans, body.mistakes, body.seconds)
    usage.track(db, ctx.warehouse.id, "floor.practice")
    db.commit()
    acc = training.accuracy(run.units, run.mistakes)
    return {"id": str(run.id), "accuracy": acc}


@router.get("/batches/{batch_id}")
def batch_payload(
    batch_id: uuid.UUID, ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Every order in the batch, each with what the phone needs offline."""
    b = batches.get(db, ctx.warehouse, batch_id)
    orders = batches.orders_of(db, b)
    return {
        "id": str(b.id),
        "number": b.number,
        "closed_at": b.closed_at.isoformat() if b.closed_at else None,
        "orders": [
            {**order_svc.offline_payload(db, ctx.warehouse, o), "tote": o.tote}
            for o in orders
            if o.status != OrderStatus.cancelled
        ],
    }


class SyncEventIn(BaseModel):
    id: uuid.UUID
    kind: Literal["scan", "void", "flag", "short", "ship", "finish", "confirm", "insert", "restock"] = "scan"
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
    quantity: int | None = Field(default=None, ge=1, le=100_000)
    short_reason: ShortReason | None = None
    tracking_number: str | None = Field(default=None, max_length=200)
    lot: str | None = Field(default=None, max_length=100)
    serial: str | None = Field(default=None, max_length=100)
    expiry: date | None = None
    insert_id: uuid.UUID | None = None
    final: bool = True

    @model_validator(mode="after")
    def check_kind(self) -> SyncEventIn:
        if self.kind == "scan" and not self.scanned_barcode:
            raise ValueError("scan events need scanned_barcode")
        if self.kind == "void" and not self.target_scan_id:
            raise ValueError("void events need target_scan_id")
        if self.kind == "flag" and not self.reason:
            raise ValueError("flag events need reason")
        if self.kind == "short" and not (self.line_item_id and self.quantity):
            raise ValueError("short events need line_item_id and quantity")
        if self.kind == "confirm" and not self.line_item_id:
            raise ValueError("confirm events need line_item_id")
        if self.kind == "ship" and not self.tracking_number and not self.final:
            raise ValueError("ship events need tracking_number")
        if self.kind == "insert" and not self.insert_id:
            raise ValueError("insert events need insert_id")
        if self.kind == "restock" and not self.line_item_id:
            raise ValueError("restock events need line_item_id")
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
    applied: dict[str, int] = {}
    for e in outcome.events:
        if e.status == "applied":
            applied[e.kind] = applied.get(e.kind, 0) + 1
    for kind, n in applied.items():
        usage.track(db, dctx.warehouse.id, f"floor.{kind}", n)
    db.commit()
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
        select(ScanEvent.result, func.sum(case((ScanEvent.result.in_(UNIT_RESULTS), ScanEvent.quantity), else_=1)))
        .where(ScanEvent.worker_session_id == ctx.session.id)
        .group_by(ScanEvent.result)
    ):
        counts[result.value] = n
    orders_done = (
        db.scalar(
            select(func.count(func.distinct(ScanEvent.order_id)))
            .join(Order, Order.id == ScanEvent.order_id)
            .where(
                ScanEvent.worker_session_id == ctx.session.id,
                Order.status.in_([OrderStatus.completed, OrderStatus.shipped]),
            )
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
        **_clock_numbers(db, ctx),
    }


def _clock_numbers(db: Session, ctx: WorkerContext) -> dict[str, Any]:
    """Hours on the clock this shift and units per hour, if the time clock is on."""
    shift = floor.open_shift(db, ctx.worker.id)
    if not shift:
        return {}
    hours = (utcnow() - shift.clock_in).total_seconds() / 3600
    units = (
        db.scalar(
            select(
                func.sum(
                    case(
                        (ScanEvent.result == ScanResult.match, ScanEvent.quantity),
                        (ScanEvent.result == ScanResult.void, -ScanEvent.quantity),
                        else_=0,
                    )
                )
            ).where(ScanEvent.worker_id == ctx.worker.id, ScanEvent.client_scanned_at >= shift.clock_in)
        )
        or 0
    )
    return {
        "clock_in": shift.clock_in.isoformat(),
        "clock_hours": round(hours, 2),
        "uph": round(units / hours, 1) if hours >= 0.25 else None,
    }


PHOTO_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}
MAX_PHOTOS_PER_FLAG = 4
MAX_PACK_PHOTOS = 6


@router.post("/photos", status_code=201)
async def upload_photo(
    request: Request,
    id: uuid.UUID = Query(...),
    flag_id: uuid.UUID | None = Query(None),
    order_id: uuid.UUID | None = Query(None),
    kind: Literal["problem", "pack"] = Query("problem"),
    worker_id: uuid.UUID | None = Query(None),
    box: int | None = Query(None, ge=1, le=99),
    dctx: DeviceContext = Depends(current_device),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A picture of a problem (attached to a flag or short pick the phone has
    already synced) or of the packed box (attached to its order, proof of what
    went in it). Raw image bytes in the body; compressed on the phone.

    Device-authenticated like sync, so a photo taken offline still uploads
    after the worker's session ended. Idempotent on the photo id.
    """
    s = get_settings()
    memory_limiter.check(f"photo:{dctx.device.id}", 30, 60, "Too many photos. Wait a minute.")
    existing = db.get(Photo, id)
    if existing is not None:
        if existing.warehouse_id != dctx.warehouse.id:
            raise bad_request("id_conflict", "Duplicate id.")
        return {"id": str(existing.id), "status": "duplicate"}
    flag: OrderFlag | None = None
    order: Order | None = None
    if kind == "pack":
        if order_id is None:
            raise bad_request("order_required", "Say which order the box is for.")
        order = db.scalar(select(Order).where(Order.id == order_id, Order.warehouse_id == dctx.warehouse.id))
        if not order:
            raise not_found("Order not found.")
        if order.status == OrderStatus.cancelled:
            raise bad_request("order_cancelled", "This order was cancelled.")
        limit, same = MAX_PACK_PHOTOS, (Photo.order_id == order.id) & (Photo.kind == "pack")
        who = worker_id if worker_id and _works_here(db, dctx, worker_id) else None
    else:
        if flag_id is None:
            raise bad_request("flag_required", "Say which problem the photo is for.")
        flag = db.scalar(select(OrderFlag).where(OrderFlag.id == flag_id, OrderFlag.warehouse_id == dctx.warehouse.id))
        if not flag:
            # The flag is still in the phone's outbox; it retries after syncing.
            raise ApiError(409, "flag_not_synced", "Sync the problem report first.")
        limit, same = MAX_PHOTOS_PER_FLAG, Photo.flag_id == flag.id
        who = flag.worker_id
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in PHOTO_TYPES:
        raise ApiError(415, "photo_type", "Photos must be JPEG, PNG or WebP.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > s.max_photo_bytes:
            raise ApiError(413, "photo_too_large", "That photo is too large.")
    data = bytes(body)
    if not data or not data.startswith(PHOTO_TYPES[ctype]):
        raise bad_request("photo_invalid", "That file isn't a valid image.")
    data, ctype = catalog.clean_photo(data), "image/jpeg"
    count = db.scalar(select(func.count()).select_from(Photo).where(same)) or 0
    if count >= limit:
        what = "per box" if kind == "pack" else "per problem"
        raise bad_request("photo_limit", f"At most {limit} photos {what}.")
    db.add(
        Photo(
            id=id,
            warehouse_id=dctx.warehouse.id,
            flag_id=flag.id if flag else None,
            kind=kind,
            box=box if kind == "pack" else None,
            order_id=flag.order_id if flag else order.id,  # type: ignore[union-attr]
            worker_id=who,
            content_type=ctype,
            size_bytes=len(data),
            data=data,
        )
    )
    if order is not None:
        order_svc.bump(order)
    usage.track(db, dctx.warehouse.id, f"floor.{kind}_photo" if kind == "pack" else "floor.photo")
    db.commit()
    return {"id": str(id), "status": "applied"}


def _works_here(db: Session, dctx: DeviceContext, worker_id: uuid.UUID) -> bool:
    w = db.get(Worker, worker_id)
    return w is not None and w.warehouse_id == dctx.warehouse.id


# ---------------------------------------------------------------------------
# Restocking
# ---------------------------------------------------------------------------


@router.get("/restock")
def restock_list(ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    workers = dash.worker_names(db, ctx.warehouse.id)
    return {"tasks": [floor.restock_dict(t, workers) for t in floor.open_restock(db, ctx.warehouse)]}


@router.post("/restock/{task_id}/done")
def restock_done(
    task_id: uuid.UUID, ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    t = db.scalar(select(RestockTask).where(RestockTask.id == task_id, RestockTask.warehouse_id == ctx.warehouse.id))
    if not t:
        raise not_found("That restock task is gone.")
    if t.status == "open":
        t.status, t.done_at, t.done_by_worker_id = "done", utcnow(), ctx.worker.id
        usage.track(db, ctx.warehouse.id, "floor.restocked")
        db.commit()
    return floor.restock_dict(t, dash.worker_names(db, ctx.warehouse.id))


# ---------------------------------------------------------------------------
# Time clock
# ---------------------------------------------------------------------------


def _shift_state(db: Session, ctx: WorkerContext) -> dict[str, Any]:
    s = floor.open_shift(db, ctx.worker.id)
    return {"enabled": ctx.warehouse.time_clock_enabled, "shift": floor.shift_dict(s) if s else None}


@router.get("/shift")
def shift_status(ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _shift_state(db, ctx)


@router.post("/clock-in")
def clock_in(ctx: WorkerContext = Depends(require_worker_access), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not ctx.warehouse.time_clock_enabled:
        raise bad_request("time_clock_off", "The time clock isn't turned on for this warehouse.")
    if not floor.open_shift(db, ctx.worker.id):
        wh, w = ctx.warehouse, ctx.worker
        db.add(Shift(warehouse_id=wh.id, worker_id=w.id, device_id=ctx.device.id, clock_in=utcnow()))
        audit.record(db, ctx.actor, "worker.clock_in", warehouse_id=wh.id, target_type="worker", target_id=w.id)
        db.commit()
    return _shift_state(db, ctx)


@router.post("/clock-out")
def clock_out(ctx: WorkerContext = Depends(current_worker), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = floor.open_shift(db, ctx.worker.id)
    if s:
        s.clock_out, s.closed_by = utcnow(), "worker"
        wh, w = ctx.warehouse, ctx.worker
        audit.record(db, ctx.actor, "worker.clock_out", warehouse_id=wh.id, target_type="worker", target_id=w.id)
        db.commit()
        return {"enabled": ctx.warehouse.time_clock_enabled, "shift": None, "last": floor.shift_dict(s)}
    return _shift_state(db, ctx)
