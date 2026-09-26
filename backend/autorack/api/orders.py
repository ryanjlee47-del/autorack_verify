"""Orders: list, create, edit, import, pick sheets, flags, aliases."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import matching
from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, current_owner, require_manager, require_owner_access
from ..errors import bad_request, conflict, not_found
from ..models import (
    BarcodeAlias,
    ImportBatch,
    Order,
    OrderFlag,
    OrderLineItem,
    OrderStatus,
    Photo,
    ScanEvent,
    ScanResult,
    Worker,
    utcnow,
)
from ..services import audit, csv_import, usage
from ..services import dashboard as dash
from ..services import orders as order_svc
from ..services.ratelimit import memory_limiter
from .warehouse import qr_svg

router = APIRouter(tags=["orders"])


class LineIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=1, le=100_000)
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=100)

    def to_input(self) -> order_svc.LineInput:
        return order_svc.LineInput(self.barcode, self.quantity, self.sku, self.description, self.location)


class OrderCreate(BaseModel):
    external_order_number: str | None = Field(default=None, max_length=100)
    customer: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    assigned_worker_id: uuid.UUID | None = None
    lines: list[LineIn] = Field(min_length=1, max_length=order_svc.MAX_LINES_PER_ORDER)


class OrderUpdate(BaseModel):
    external_order_number: str | None = Field(default=None, max_length=100)
    customer: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    assigned_worker_id: uuid.UUID | None = None
    clear_assignment: bool = False


class LineUpdate(BaseModel):
    barcode: str | None = Field(default=None, min_length=1, max_length=200)
    quantity: int | None = Field(default=None, ge=1, le=100_000)
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=100)


class FlagResolve(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    # Short picks only: "accept" ships the order short; "reopen" sends the
    # missing units back to the pick list. Other flags just resolve.
    action: Literal["resolve", "accept", "reopen"] = "resolve"


def _check_worker(db: Session, ctx: OwnerContext, worker_id: uuid.UUID | None) -> None:
    if worker_id and not db.scalar(
        select(Worker.id).where(Worker.id == worker_id, Worker.warehouse_id == ctx.warehouse.id)
    ):
        raise bad_request("worker_invalid", "That worker isn't at this warehouse.")


@router.get("/orders")
def list_orders(
    status: str | None = Query(None, description="Comma-separated statuses, or 'open'"),
    q: str | None = Query(None, max_length=100),
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Order).where(Order.warehouse_id == ctx.warehouse.id)
    if status:
        wanted = (
            [OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]
            if status == "open"
            else [OrderStatus(s) for s in status.split(",") if s in OrderStatus.__members__]
        )
        stmt = stmt.where(Order.status.in_(wanted))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Order.external_order_number.ilike(like),
                Order.customer.ilike(like),
                Order.tracking_number.ilike(like.replace(" ", "")),
                Order.id.in_(
                    select(OrderLineItem.order_id).where(
                        OrderLineItem.warehouse_id == ctx.warehouse.id,
                        or_(
                            OrderLineItem.expected_barcode.ilike(like),
                            OrderLineItem.sku.ilike(like),
                            OrderLineItem.sku_description.ilike(like),
                        ),
                    )
                ),
            )
        )
    if created_from:
        start, _, _ = dash.day_bounds(ctx.warehouse, _local_date(created_from))
        stmt = stmt.where(Order.created_at >= start)
    if created_to:
        _, end, _ = dash.day_bounds(ctx.warehouse, _local_date(created_to))
        stmt = stmt.where(Order.created_at < end)
    orders = list(db.scalars(stmt.order_by(Order.created_at.desc(), Order.id).offset(offset).limit(limit + 1)))
    has_more = len(orders) > limit
    rows = dash.order_rows(db, ctx.warehouse, orders[:limit])
    return {"orders": rows, "has_more": has_more, "offset": offset, "limit": limit}


def _local_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise bad_request("date_invalid", "Dates must be YYYY-MM-DD.") from None


@router.post("/orders", status_code=201)
def create_order(
    body: OrderCreate, ctx: OwnerContext = Depends(require_owner_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _check_worker(db, ctx, body.assigned_worker_id)
    order = order_svc.create_order(
        db,
        ctx.warehouse,
        external_order_number=body.external_order_number,
        notes=body.notes,
        lines=[li.to_input() for li in body.lines],
        created_by_user_id=ctx.user.id,
        assigned_worker_id=body.assigned_worker_id,
        customer=body.customer,
        actor=ctx.actor,
    )
    usage.track(db, ctx.warehouse.id, "orders.manual")
    db.commit()
    return order_detail(order.id, ctx, db)


@router.get("/orders/template.csv", response_class=PlainTextResponse)
def csv_template(ctx: OwnerContext = Depends(current_owner)) -> PlainTextResponse:
    return PlainTextResponse(
        csv_import.TEMPLATE_CSV,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="autorack-orders-template.csv"'},
    )


async def _read_upload(file: UploadFile) -> bytes:
    limit = get_settings().max_import_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise bad_request("file_too_large", f"CSV files are limited to {limit // (1024 * 1024)} MB.")
    return data


@router.post("/orders/import/preview")
async def import_preview(
    file: UploadFile = File(...), ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    memory_limiter.check(
        f"import:{ctx.warehouse.id}", get_settings().import_requests_per_minute, 60, "Too many imports. Wait a minute."
    )
    return csv_import.preview(db, ctx.warehouse, await _read_upload(file))


@router.post("/orders/import", status_code=201)
async def import_commit(
    file: UploadFile = File(...),
    skip_invalid_rows: bool = Form(False),
    ctx: OwnerContext = Depends(require_owner_access),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    memory_limiter.check(
        f"import:{ctx.warehouse.id}", get_settings().import_requests_per_minute, 60, "Too many imports. Wait a minute."
    )
    batch = csv_import.commit(
        db, ctx.warehouse, await _read_upload(file), file.filename, ctx.actor, ctx.user.id, skip_invalid_rows
    )
    usage.track(db, ctx.warehouse.id, "orders.import")
    db.commit()
    return _batch_dict(batch)


def _batch_dict(b: ImportBatch) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "filename": b.filename,
        "orders_created": b.orders_created,
        "lines_created": b.lines_created,
        "orders_skipped": b.orders_skipped,
        "warnings": b.warnings,
        "created_at": b.created_at.isoformat(),
    }


@router.get("/imports")
def list_imports(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(ImportBatch)
        .where(ImportBatch.warehouse_id == ctx.warehouse.id)
        .order_by(ImportBatch.created_at.desc())
        .limit(50)
    )
    return [_batch_dict(b) for b in rows]


@router.get("/orders/pick-sheets")
def pick_sheets(
    ids: str = Query(..., description="Comma-separated order ids"),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    try:
        wanted = [uuid.UUID(x) for x in ids.split(",") if x.strip()][:200]
    except ValueError:
        raise bad_request("ids_invalid", "Invalid order id.") from None
    orders = {
        o.id: o for o in db.scalars(select(Order).where(Order.warehouse_id == ctx.warehouse.id, Order.id.in_(wanted)))
    }
    out = []
    for oid in wanted:
        o = orders.get(oid)
        if not o:
            continue
        lines = order_svc.lines_for(db, o.id)
        # Sorted by location so the sheet is a walking route through the aisles.
        lines.sort(key=lambda li: (li.location or "~", li.line_no))
        out.append(
            {
                **order_svc.order_summary(o, lines),
                "qr_svg": qr_svg(order_svc.order_qr_payload(o)),
                "lines": [order_svc.line_dict(li) for li in lines],
            }
        )
    if out:
        usage.track(db, ctx.warehouse.id, "orders.pick_sheets", len(out))
        db.commit()
    return out


def _get(db: Session, ctx: OwnerContext, order_id: uuid.UUID, lock: bool = False) -> Order:
    return order_svc.get_order(db, ctx.warehouse.id, order_id, lock=lock)


def order_detail(order_id: uuid.UUID, ctx: OwnerContext, db: Session) -> dict[str, Any]:
    order = _get(db, ctx, order_id)
    lines = order_svc.lines_for(db, order.id)
    workers = dash.worker_names(db, ctx.warehouse.id)
    flags = list(db.scalars(select(OrderFlag).where(OrderFlag.order_id == order.id).order_by(OrderFlag.created_at)))
    photos = photos_by_flag(db, ctx.warehouse.id, [f.id for f in flags])
    errors_by_line: dict[uuid.UUID, int] = {}
    for lid in db.scalars(
        select(ScanEvent.intended_line_item_id).where(
            ScanEvent.order_id == order.id, ScanEvent.result == ScanResult.mismatch
        )
    ):
        if lid:
            errors_by_line[lid] = errors_by_line.get(lid, 0) + 1
    return {
        **order_svc.order_summary(order, lines),
        "assigned_worker": workers.get(order.assigned_worker_id) if order.assigned_worker_id else None,
        "shipped_by": workers.get(order.shipped_by_worker_id) if order.shipped_by_worker_id else None,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "lines": [{**order_svc.line_dict(li), "mismatches": errors_by_line.get(li.id, 0)} for li in lines],
        "flags": [flag_dict(f, workers, photos.get(f.id, [])) for f in flags],
        "qr_svg": qr_svg(order_svc.order_qr_payload(order)),
    }


def flag_dict(f: OrderFlag, workers: dict[uuid.UUID, str], photo_ids: list[str]) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "reason": f.reason.value,
        "note": f.note,
        "line_item_id": str(f.line_item_id) if f.line_item_id else None,
        "short_quantity": f.short_quantity,
        "short_reason": f.short_reason.value if f.short_reason else None,
        "worker": workers.get(f.worker_id) if f.worker_id else None,
        "created_at": f.created_at.isoformat(),
        "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
        "resolution": f.resolution,
        "resolution_note": f.resolution_note,
        "photos": photo_ids,
    }


def photos_by_flag(db: Session, warehouse_id: uuid.UUID, flag_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    if not flag_ids:
        return out
    for pid, fid in db.execute(
        select(Photo.id, Photo.flag_id)
        .where(Photo.warehouse_id == warehouse_id, Photo.flag_id.in_(flag_ids))
        .order_by(Photo.created_at)
    ):
        out.setdefault(fid, []).append(str(pid))
    return out


@router.get("/orders/{order_id}")
def get_order(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return order_detail(order_id, ctx, db)


@router.patch("/orders/{order_id}")
def update_order(
    order_id: uuid.UUID, body: OrderUpdate, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = _get(db, ctx, order_id, lock=True)
    order_svc.ensure_editable(order)
    changes: dict[str, Any] = {}
    if body.external_order_number is not None:
        number = order_svc.clean(body.external_order_number, 100)
        if number and order_svc.order_number_in_use(db, ctx.warehouse.id, number, exclude=order.id):
            raise conflict("order_number_taken", f"Order {number} already exists.")
        order.external_order_number = number
        changes["external_order_number"] = number
    if body.notes is not None:
        order.notes = order_svc.clean(body.notes, 2000)
        changes["notes"] = order.notes
    if body.customer is not None:
        order.customer = order_svc.clean(body.customer, 200)
        changes["customer"] = order.customer
    if body.clear_assignment:
        order.assigned_worker_id = None
        changes["assigned_worker_id"] = None
    elif body.assigned_worker_id:
        _check_worker(db, ctx, body.assigned_worker_id)
        order.assigned_worker_id = body.assigned_worker_id
        changes["assigned_worker_id"] = body.assigned_worker_id
    order_svc.bump(order)
    audit.record(
        db,
        ctx.actor,
        "order.updated",
        warehouse_id=ctx.warehouse.id,
        target_type="order",
        target_id=order.id,
        changes=changes,
    )
    db.commit()
    return order_detail(order.id, ctx, db)


@router.post("/orders/{order_id}/cancel")
def cancel_order(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = _get(db, ctx, order_id, lock=True)
    order_svc.cancel_order(db, order, ctx.actor)
    db.commit()
    return order_detail(order.id, ctx, db)


@router.post("/orders/{order_id}/lines", status_code=201)
def add_line(
    order_id: uuid.UUID, body: LineIn, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = _get(db, ctx, order_id, lock=True)
    order_svc.add_line(db, ctx.warehouse, order, body.to_input(), ctx.actor)
    db.commit()
    return order_detail(order.id, ctx, db)


def _get_line(db: Session, order: Order, line_id: uuid.UUID) -> OrderLineItem:
    line = db.scalar(select(OrderLineItem).where(OrderLineItem.id == line_id, OrderLineItem.order_id == order.id))
    if not line:
        raise not_found("Line not found")
    return line


@router.patch("/orders/{order_id}/lines/{line_id}")
def update_line(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LineUpdate,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = _get(db, ctx, order_id, lock=True)
    line = _get_line(db, order, line_id)
    order_svc.update_line(db, order, line, body.model_dump(exclude_unset=True), ctx.actor)
    db.commit()
    return order_detail(order.id, ctx, db)


@router.delete("/orders/{order_id}/lines/{line_id}")
def delete_line(
    order_id: uuid.UUID, line_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = _get(db, ctx, order_id, lock=True)
    line = _get_line(db, order, line_id)
    order_svc.delete_line(db, order, line, ctx.actor)
    db.commit()
    return order_detail(order.id, ctx, db)


@router.post("/orders/{order_id}/flags/{flag_id}/resolve")
def resolve_flag(
    order_id: uuid.UUID,
    flag_id: uuid.UUID,
    body: FlagResolve,
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = _get(db, ctx, order_id, lock=True)
    flag = db.scalar(select(OrderFlag).where(OrderFlag.id == flag_id, OrderFlag.order_id == order.id))
    if not flag:
        raise not_found("Flag not found")
    if not flag.resolved_at:
        is_short = flag.reason.value == "short_pick"
        if body.action != "resolve" and not is_short:
            raise bad_request("not_short_pick", "Only a short pick can be accepted or sent back.")
        if order.status == OrderStatus.cancelled:
            raise conflict("order_cancelled", "This order was cancelled.")
        flag.resolved_at = utcnow()
        flag.resolved_by_user_id = ctx.user.id
        flag.resolution_note = order_svc.clean(body.note, 500)
        if is_short and body.action == "reopen" and flag.line_item_id:
            # Back on the pick list: the missing units must be picked after all.
            line = db.get(OrderLineItem, flag.line_item_id)
            if line is not None:
                line.short_quantity = max(0, line.short_quantity - (flag.short_quantity or 0))
            flag.resolution = "reopened"
            order_svc.bump(order)
        elif is_short:
            flag.resolution = "accepted"
        else:
            flag.resolution = "resolved"
        db.flush()
        order_svc.recompute_status(db, order)
        audit.record(
            db,
            ctx.actor,
            "order.flag_resolved",
            warehouse_id=ctx.warehouse.id,
            target_type="order",
            target_id=order.id,
            flag_id=flag.id,
            resolution=flag.resolution,
        )
        usage.track(db, ctx.warehouse.id, "flags.resolve")
        db.commit()
    return order_detail(order.id, ctx, db)


@router.get("/orders/{order_id}/scans")
def order_scans(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    order = _get(db, ctx, order_id)
    workers = dash.worker_names(db, ctx.warehouse.id)
    voided = set(
        db.scalars(
            select(ScanEvent.voids_scan_id).where(ScanEvent.order_id == order.id, ScanEvent.voids_scan_id.is_not(None))
        )
    )
    scans = db.scalars(
        select(ScanEvent)
        .where(ScanEvent.order_id == order.id)
        .order_by(ScanEvent.client_scanned_at.desc(), ScanEvent.client_seq.desc())
        .limit(1000)
    )
    return [
        {
            "id": str(s.id),
            "result": s.result.value,
            "scanned_barcode": s.scanned_barcode,
            "line_item_id": str(s.line_item_id) if s.line_item_id else None,
            "intended_line_item_id": str(s.intended_line_item_id) if s.intended_line_item_id else None,
            "match_tier": s.match_tier,
            "worker": workers.get(s.worker_id),
            "client_result": s.client_result,
            "was_offline": s.was_offline,
            "voided": s.id in voided,
            "voids_scan_id": str(s.voids_scan_id) if s.voids_scan_id else None,
            "at": s.client_scanned_at.isoformat(),
            "received_at": s.received_at.isoformat(),
        }
        for s in scans
    ]


# ---------------------------------------------------------------------------
# Aliases: teaching the engine that two barcodes are the same product
# ---------------------------------------------------------------------------


class AliasCreate(BaseModel):
    scanned_barcode: str = Field(min_length=1, max_length=500)
    target_barcode: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=300)


def alias_dict(a: BarcodeAlias) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "alias": a.alias_key,
        "target": a.target_key,
        "note": a.note,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/aliases")
def list_aliases(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(BarcodeAlias)
        .where(BarcodeAlias.warehouse_id == ctx.warehouse.id)
        .order_by(BarcodeAlias.created_at.desc())
    )
    return [alias_dict(a) for a in rows]


@router.post("/aliases", status_code=201)
def create_alias(
    body: AliasCreate, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    alias_key = matching.normalized_key(body.scanned_barcode)
    target_key = matching.normalized_key(body.target_barcode)
    if not alias_key or not target_key:
        raise bad_request("barcode_required", "Both barcodes are required.")
    if alias_key == target_key:
        raise bad_request("alias_same", "Those are already the same barcode.")
    existing = db.scalar(
        select(BarcodeAlias).where(BarcodeAlias.warehouse_id == ctx.warehouse.id, BarcodeAlias.alias_key == alias_key)
    )
    if existing:
        raise conflict("alias_exists", f"{alias_key} is already taught as {existing.target_key}. Remove that first.")
    a = BarcodeAlias(
        warehouse_id=ctx.warehouse.id,
        alias_key=alias_key,
        target_key=target_key,
        note=order_svc.clean(body.note, 300),
        created_by_user_id=ctx.user.id,
    )
    db.add(a)
    db.flush()
    _bump_orders_with_barcode(db, ctx, target_key)
    usage.track(db, ctx.warehouse.id, "aliases.create")
    audit.record(
        db,
        ctx.actor,
        "alias.created",
        warehouse_id=ctx.warehouse.id,
        target_type="alias",
        target_id=a.id,
        alias=alias_key,
        target=target_key,
    )
    db.commit()
    return alias_dict(a)


@router.delete("/aliases/{alias_id}")
def delete_alias(
    alias_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, bool]:
    a = db.scalar(
        select(BarcodeAlias).where(BarcodeAlias.id == alias_id, BarcodeAlias.warehouse_id == ctx.warehouse.id)
    )
    if not a:
        raise not_found("Alias not found")
    _bump_orders_with_barcode(db, ctx, a.target_key)
    audit.record(
        db,
        ctx.actor,
        "alias.deleted",
        warehouse_id=ctx.warehouse.id,
        target_type="alias",
        target_id=a.id,
        alias=a.alias_key,
        target=a.target_key,
    )
    db.delete(a)
    db.commit()
    return {"ok": True}


def _bump_orders_with_barcode(db: Session, ctx: OwnerContext, key: str) -> None:
    """Open orders containing this barcode must refresh their cached index."""
    for order in db.scalars(
        select(Order).where(
            Order.warehouse_id == ctx.warehouse.id,
            Order.status.in_([OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]),
            Order.id.in_(
                select(OrderLineItem.order_id).where(
                    OrderLineItem.warehouse_id == ctx.warehouse.id, OrderLineItem.normalized_barcode == key
                )
            ),
        )
    ):
        order_svc.bump(order)


# ---------------------------------------------------------------------------
# Photos and shipment proof
# ---------------------------------------------------------------------------


def photo_response(photo: Photo) -> Response:
    return Response(
        content=photo.data,
        media_type=photo.content_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        },
    )


@router.get("/photos/{photo_id}", response_class=Response)
def get_photo(
    photo_id: uuid.UUID, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> Response:
    photo = db.scalar(select(Photo).where(Photo.id == photo_id, Photo.warehouse_id == ctx.warehouse.id))
    if not photo:
        raise not_found("Photo not found")
    return photo_response(photo)


@router.get("/photos")
def list_photos(
    limit: int = Query(60, ge=1, le=200),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Recent problem photos across the warehouse, newest first."""
    workers = dash.worker_names(db, ctx.warehouse.id)
    rows = db.execute(
        select(
            Photo.id, Photo.created_at, Photo.worker_id, Photo.order_id, OrderFlag.reason, Order.external_order_number
        )
        .join(OrderFlag, OrderFlag.id == Photo.flag_id)
        .join(Order, Order.id == Photo.order_id)
        .where(Photo.warehouse_id == ctx.warehouse.id)
        .order_by(Photo.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(pid),
            "at": at.isoformat(),
            "worker": workers.get(wid) if wid else None,
            "order_id": str(oid),
            "order_number": number,
            "reason": reason.value,
        }
        for pid, at, wid, oid, reason, number in rows
    ]


@router.get("/orders/{order_id}/proof")
def shipment_proof(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Everything that proves what went into this parcel: each unit's scan,
    who picked it, when, and the shipping label it left under."""
    detail = order_detail(order_id, ctx, db)
    workers = dash.worker_names(db, ctx.warehouse.id)
    voided = set(
        db.scalars(
            select(ScanEvent.voids_scan_id).where(ScanEvent.order_id == order_id, ScanEvent.voids_scan_id.is_not(None))
        )
    )
    picks = [
        {
            "line_item_id": str(s.line_item_id),
            "scanned_barcode": s.scanned_barcode,
            "worker": workers.get(s.worker_id),
            "at": s.client_scanned_at.isoformat(),
        }
        for s in db.scalars(
            select(ScanEvent)
            .where(
                ScanEvent.order_id == order_id,
                ScanEvent.warehouse_id == ctx.warehouse.id,
                ScanEvent.result == ScanResult.match,
            )
            .order_by(ScanEvent.client_scanned_at, ScanEvent.client_seq)
        )
        if s.id not in voided
    ]
    caught = (
        db.scalar(
            select(func.count())
            .select_from(ScanEvent)
            .where(ScanEvent.order_id == order_id, ScanEvent.result.in_(dash.ERROR_RESULTS))
        )
        or 0
    )
    usage.track(db, ctx.warehouse.id, "orders.proof")
    db.commit()
    detail.pop("qr_svg", None)
    return {
        **detail,
        "warehouse": ctx.warehouse.name,
        "picks": picks,
        "errors_caught": caught,
        "generated_at": utcnow().isoformat(),
    }
