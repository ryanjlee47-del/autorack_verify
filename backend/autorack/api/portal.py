"""The 3PL client portal: a brand signs in with Google and sees its own
orders (status, tracking, the proof of what went in each box), its returns,
a monthly report and its billing statement. Read-only, and never another
client's data or the warehouse's internal records (who picked what,
problem reports)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import ClientContext, current_client
from ..downloads import attachment
from ..errors import bad_request, not_found
from ..models import (
    Order,
    OrderKind,
    OrderStatus,
    Photo,
    ScanEvent,
    ScanResult,
    utcnow,
)
from ..services import claim, client_billing, client_report, floor, monthly, tasks
from ..services import dashboard as dash
from ..services import orders as order_svc

router = APIRouter(prefix="/portal", tags=["portal"])

OPEN = [OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]


def _order(db: Session, ctx: ClientContext, order_id: uuid.UUID) -> Order:
    o = db.scalar(
        select(Order).where(
            Order.id == order_id, Order.warehouse_id == ctx.warehouse.id, Order.client_id == ctx.client.id
        )
    )
    if not o:
        raise not_found("Order not found.")
    return o


def _month(ctx: ClientContext, month: str | None) -> tuple[int, int]:
    today = utcnow().astimezone(dash.tz_of(ctx.warehouse)).date()
    if not month:
        return today.year, today.month
    try:
        return monthly.parse_month(month, today)
    except ValueError:
        raise bad_request("month_invalid", "Months look like 2026-09.") from None


def _out_at(o: Order) -> str | None:
    """When it left: the label scan, or picking done where labels aren't scanned."""
    when = o.shipped_at or (o.completed_at if o.status == OrderStatus.completed else None)
    return when.isoformat() if when else None


def _row(o: Order, r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "number": o.external_order_number,
        "customer": o.customer,
        "kind": o.kind.value,
        "status": o.status.value,
        "units_expected": r["units_expected"],
        "units_scanned": r["units_scanned"],
        "tracking_number": o.tracking_number,
        "carrier": o.carrier,
        "created_at": o.created_at.isoformat(),
        "shipped_at": _out_at(o),
        "rush": o.rush,
        "due_at": r.get("due_at"),
        "late": r.get("late", False),
    }


@router.get("/me")
def portal_me(ctx: ClientContext = Depends(current_client), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "user": {"email": ctx.user.email, "name": ctx.user.name},
        "client": {"id": str(ctx.client.id), "name": ctx.client.name},
        "warehouse": {
            "name": ctx.warehouse.name,
            "timezone": ctx.warehouse.timezone,
            "has_logo": client_report.logo_of(db, ctx.warehouse.id) is not None,
        },
    }


@router.get("/logo", response_class=Response)
def portal_logo(ctx: ClientContext = Depends(current_client), db: Session = Depends(get_db)) -> Response:
    """The warehouse's own logo: the portal is theirs, not ours."""
    from .warehouse import logo_response

    return logo_response(client_report.logo_of(db, ctx.warehouse.id))


@router.get("/accuracy.pdf", response_class=Response)
def portal_accuracy_pdf(
    month: str | None = Query(None),
    ctx: ClientContext = Depends(current_client),
    db: Session = Depends(get_db),
) -> Response:
    y, m = _month(ctx, month)
    pdf, d = client_report.pdf_for(db, ctx.warehouse, ctx.client, y, m)
    return Response(
        pdf, media_type="application/pdf", headers={"Content-Disposition": attachment(client_report.filename(d))}
    )


@router.get("/orders")
def portal_orders(
    status: str = Query("all", description="open | shipped | all"),
    kind: str = Query("pick", description="pick | return"),
    q: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ClientContext = Depends(current_client),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Order).where(
        Order.warehouse_id == ctx.warehouse.id,
        Order.client_id == ctx.client.id,
        Order.kind == (OrderKind.ret if kind == "return" else OrderKind.pick),
    )
    if status == "open":
        stmt = stmt.where(Order.status.in_([*OPEN, OrderStatus.completed]))
    elif status == "shipped":
        stmt = stmt.where(Order.status == OrderStatus.shipped)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Order.external_order_number.ilike(like),
                Order.customer.ilike(like),
                Order.tracking_number.ilike(like.replace(" ", "")),
            )
        )
    orders = list(db.scalars(stmt.order_by(Order.created_at.desc(), Order.id).offset(offset).limit(limit + 1)))
    page = orders[:limit]
    rows = dash.order_rows(db, ctx.warehouse, page)
    return {"orders": [_row(o, r) for o, r in zip(page, rows, strict=True)], "has_more": len(orders) > limit}


@router.get("/orders/{order_id}")
def portal_order(
    order_id: uuid.UUID, ctx: ClientContext = Depends(current_client), db: Session = Depends(get_db)
) -> dict[str, Any]:
    o = _order(db, ctx, order_id)
    lines = order_svc.lines_for(db, o.id)
    by_line = {li.id: li for li in lines}
    voided = select(ScanEvent.voids_scan_id).where(ScanEvent.order_id == o.id, ScanEvent.voids_scan_id.is_not(None))
    scans = db.scalars(
        select(ScanEvent)
        .where(ScanEvent.order_id == o.id, ScanEvent.result == ScanResult.match, ScanEvent.id.not_in(voided))
        .order_by(ScanEvent.client_scanned_at, ScanEvent.client_seq)
    )
    caught = (
        db.scalar(
            select(func.count())
            .select_from(ScanEvent)
            .where(ScanEvent.order_id == o.id, ScanEvent.result.in_(dash.ERROR_RESULTS))
        )
        or 0
    )
    done = floor.insert_checks(db, o.id)
    return {
        **_row(o, {**order_svc.order_summary(o, lines), **order_svc.due_info(o, ctx.warehouse)}),
        "errors_caught": int(caught),
        "lines": [
            {
                "description": li.sku_description,
                "sku": li.sku,
                "barcode": li.expected_barcode,
                "ordered": li.expected_quantity,
                "verified": min(li.scanned_quantity, li.expected_quantity),
                "short": li.short_quantity,
            }
            for li in lines
        ],
        "units": [
            {
                "item": (by_line[s.line_item_id].sku_description or by_line[s.line_item_id].sku or "")
                if s.line_item_id in by_line
                else "",
                "barcode": s.scanned_barcode,
                "quantity": s.quantity,
                "confirmed": s.confirmed,
                "at": s.client_scanned_at.isoformat(),
                "lot": s.lot,
                "serial": s.serial,
                "expiry": s.expiry.isoformat() if s.expiry else None,
            }
            for s in scans
        ],
        "boxes": [
            {k: b[k] for k in ("box", "tracking_number", "carrier", "photos", "at")}
            for b in order_svc.package_dicts(db, o)
        ],
        "pack_photos": [str(p) for p in order_svc.pack_photo_ids(db, o.id)],
        "inserts": [{"name": i.name, "done": i.id in done} for i in floor.inserts_for(db, o, lines)],
        "variance": tasks.variance(db, o) if o.kind == OrderKind.ret else None,
        "timezone": ctx.warehouse.timezone,
    }


@router.get("/photos/{photo_id}", response_class=Response)
def portal_photo(
    photo_id: uuid.UUID, ctx: ClientContext = Depends(current_client), db: Session = Depends(get_db)
) -> Response:
    """Packed-box photos of this client's orders only (never problem photos)."""
    photo = db.scalar(
        select(Photo)
        .join(Order, Order.id == Photo.order_id)
        .where(
            Photo.id == photo_id,
            Photo.kind == "pack",
            Order.warehouse_id == ctx.warehouse.id,
            Order.client_id == ctx.client.id,
        )
    )
    if not photo:
        raise not_found("Photo not found.")
    return Response(
        content=photo.data,
        media_type=photo.content_type,
        headers={"Cache-Control": "private, max-age=3600", "Content-Security-Policy": "default-src 'none'"},
    )


@router.get("/orders/{order_id}/claim.pdf", response_class=Response)
def portal_claim(
    order_id: uuid.UUID, ctx: ClientContext = Depends(current_client), db: Session = Depends(get_db)
) -> Response:
    o = _order(db, ctx, order_id)
    if o.kind != OrderKind.pick:
        raise bad_request("not_for_task", "Evidence packs are for shipped orders.")
    return Response(
        claim.build(db, ctx.warehouse, o, internal=False),
        media_type="application/pdf",
        headers={"Content-Disposition": attachment(claim.filename(o))},
    )


@router.get("/report")
def portal_report(
    month: str | None = Query(None),
    ctx: ClientContext = Depends(current_client),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """This client's month: what shipped, how accurately, how fast, what came back."""
    y, m = _month(ctx, month)
    d = client_report.numbers(db, ctx.warehouse, ctx.client, y, m)
    d.pop("recent", None)
    return d


@router.get("/statement")
def portal_statement(
    month: str | None = Query(None),
    ctx: ClientContext = Depends(current_client),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    y, m = _month(ctx, month)
    return client_billing.statement(db, ctx.warehouse, ctx.client, y, m)
