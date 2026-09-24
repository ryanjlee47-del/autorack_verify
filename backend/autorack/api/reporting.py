"""Dashboard data, CSV exports, and billing."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ..db import get_db
from ..deps import OwnerContext, current_owner, require_owner_role
from ..models import Order, OrderLineItem, ScanEvent, Worker, utcnow
from ..services import billing
from ..services import dashboard as dash

router = APIRouter(tags=["reporting"])


@router.get("/dashboard/summary")
def summary(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
    return dash.summary(db, ctx.warehouse)


@router.get("/dashboard/live")
def live(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
    return dash.live(db, ctx.warehouse)


@router.get("/dashboard/workers")
def workers(
    days: int = Query(7, ge=1, le=365), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return dash.worker_stats(db, ctx.warehouse, days)


@router.get("/dashboard/skus")
def skus(
    days: int = Query(30, ge=1, le=365), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return dash.sku_insights(db, ctx.warehouse, days)


@router.get("/dashboard/trend")
def trend(
    days: int = Query(30, ge=7, le=180), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return dash.trend(db, ctx.warehouse, days)


@router.get("/dashboard/problems")
def problems(
    limit: int = Query(50, ge=1, le=200), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    return dash.recent_problems(db, ctx.warehouse, limit=limit)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def safe_cell(v: Any) -> str:
    """Neutralize spreadsheet formula injection. A barcode payload is
    whatever a label said; '=HYPERLINK(...)' must stay text in Excel."""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _csv_stream(header: list[str], rows: Iterator[list[Any]]) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    yield buf.getvalue()
    for row in rows:
        buf.seek(0)
        buf.truncate()
        w.writerow([safe_cell(c) for c in row])
        yield buf.getvalue()


def _download(name: str, header: list[str], rows: Iterator[list[Any]]) -> StreamingResponse:
    return StreamingResponse(
        _csv_stream(header, rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/exports/scans.csv")
def export_scans(
    days: int = Query(30, ge=1, le=3650), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> StreamingResponse:
    since = utcnow() - timedelta(days=days)
    tz = dash.tz_of(ctx.warehouse)
    line = aliased(OrderLineItem)
    intended = aliased(OrderLineItem)
    stmt = (
        select(
            ScanEvent,
            Worker.name,
            Order.external_order_number,
            line.expected_barcode,
            line.sku,
            intended.expected_barcode,
            intended.sku,
        )
        .join(Worker, Worker.id == ScanEvent.worker_id)
        .join(Order, Order.id == ScanEvent.order_id)
        .outerjoin(line, line.id == ScanEvent.line_item_id)
        .outerjoin(intended, intended.id == ScanEvent.intended_line_item_id)
        .where(ScanEvent.warehouse_id == ctx.warehouse.id, ScanEvent.client_scanned_at >= since)
        .order_by(ScanEvent.client_scanned_at)
        .execution_options(yield_per=1000)
    )

    def rows() -> Iterator[list[Any]]:
        for se, wname, number, lbar, lsku, ibar, isku in db.execute(stmt):
            yield [
                se.client_scanned_at.astimezone(tz).isoformat(timespec="seconds"),
                se.received_at.astimezone(tz).isoformat(timespec="seconds"),
                number,
                wname,
                se.result.value,
                se.scanned_barcode,
                lbar,
                lsku,
                ibar,
                isku,
                se.match_tier,
                "yes" if se.was_offline else "no",
                str(se.id),
                str(se.voids_scan_id or ""),
            ]

    header = [
        "scanned_at",
        "synced_at",
        "order_number",
        "worker",
        "result",
        "scanned_barcode",
        "matched_barcode",
        "matched_sku",
        "intended_barcode",
        "intended_sku",
        "match_tier",
        "offline",
        "scan_id",
        "undoes_scan_id",
    ]
    return _download(f"autorack-scans-{utcnow():%Y%m%d}.csv", header, rows())


@router.get("/exports/orders.csv")
def export_orders(
    days: int = Query(30, ge=1, le=3650), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> StreamingResponse:
    since = utcnow() - timedelta(days=days)
    tz = dash.tz_of(ctx.warehouse)
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.warehouse_id == ctx.warehouse.id, Order.created_at >= since)
            .order_by(Order.created_at)
        )
    )

    def rows() -> Iterator[list[Any]]:
        for i in range(0, len(orders), 500):
            for r in dash.order_rows(db, ctx.warehouse, orders[i : i + 500]):
                yield [
                    r["external_order_number"],
                    r["status"],
                    r["source"],
                    r["line_count"],
                    r["units_expected"],
                    r["units_scanned"],
                    r["errors_caught"],
                    dash.fmt_local(r["created_at"], tz),
                    dash.fmt_local(r["completed_at"], tz),
                    r["id"],
                ]

    header = [
        "order_number",
        "status",
        "source",
        "lines",
        "units_expected",
        "units_scanned",
        "errors_caught",
        "created_at",
        "completed_at",
        "order_id",
    ]
    return _download(f"autorack-orders-{utcnow():%Y%m%d}.csv", header, rows())


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


@router.get("/billing")
def billing_info(ctx: OwnerContext = Depends(current_owner)) -> dict[str, Any]:
    return billing.billing_info(ctx.warehouse)


@router.post("/billing/checkout")
def checkout(ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)) -> dict[str, str]:
    return {"url": billing.create_checkout(db, ctx.warehouse, ctx.user, ctx.actor)}


@router.post("/billing/portal")
def portal(ctx: OwnerContext = Depends(require_owner_role)) -> dict[str, str]:
    return {"url": billing.create_portal(ctx.warehouse)}


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    payload = await request.body()
    return billing.handle_webhook(db, payload, request.headers.get("stripe-signature"))
