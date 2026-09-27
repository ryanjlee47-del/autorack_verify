"""Pages anyone with the link can open: the proof of shipment an owner
shares with their customer (or their customer's customer).

What it shows is chosen for a stranger: what was ordered, what was
verified by scan and when, lot/serial numbers, the tracking number, and how
many wrong items were caught before packing, and the photo of the packed
box. Not who picked it, not internal notes, not problem reports or their
photos.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, client_ip, require_manager
from ..errors import bad_request, not_found
from ..models import Order as OrderModel
from ..models import OrderKind, OrderLineItem, OrderStatus, Photo, ScanEvent, ScanResult, Warehouse, utcnow
from ..services import audit, usage
from ..services import dashboard as dash
from ..services import orders as order_svc
from ..services.ratelimit import memory_limiter

router = APIRouter(tags=["share"])
public_router = APIRouter(prefix="/public", tags=["public"])


def share_url(token: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/proof.html#t={token}"


@router.post("/orders/{order_id}/share")
def share_proof(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = order_svc.get_order(db, ctx.warehouse.id, order_id, lock=True)
    if order.kind != OrderKind.pick or order.status not in (OrderStatus.completed, OrderStatus.shipped):
        raise bad_request("not_shareable", "Only a picked or shipped order has a proof to share.")
    if not order.share_token:
        order.share_token = secrets.token_urlsafe(24)
        order.shared_at = utcnow()
        audit.record(
            db, ctx.actor, "order.proof_shared", warehouse_id=ctx.warehouse.id, target_type="order", target_id=order.id
        )
        usage.track(db, ctx.warehouse.id, "orders.proof_shared")
        db.commit()
    return {"url": share_url(order.share_token), "shared_at": order.shared_at.isoformat() if order.shared_at else None}


@router.delete("/orders/{order_id}/share", status_code=204)
def unshare_proof(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> None:
    order = order_svc.get_order(db, ctx.warehouse.id, order_id, lock=True)
    if order.share_token:
        order.share_token = None
        order.shared_at = None
        audit.record(
            db,
            ctx.actor,
            "order.proof_unshared",
            warehouse_id=ctx.warehouse.id,
            target_type="order",
            target_id=order.id,
        )
        db.commit()


def _shared(db: Session, token: str) -> tuple[OrderModel, Warehouse]:
    if not 20 <= len(token) <= 64:
        raise not_found("This link isn't valid (it may have been turned off).")
    order = db.scalar(select(OrderModel).where(OrderModel.share_token == token))
    if not order or order.status == OrderStatus.cancelled:
        raise not_found("This link isn't valid (it may have been turned off).")
    wh = db.get(Warehouse, order.warehouse_id)
    if wh is None or wh.closed_at:
        raise not_found("This link isn't valid (it may have been turned off).")
    return order, wh


@public_router.get("/proof/{token}/photos/{photo_id}", response_class=Response)
def public_pack_photo(token: str, photo_id: uuid.UUID, request: Request, db: Session = Depends(get_db)) -> Response:
    """The packed-box photo, only through the order's share link."""
    memory_limiter.check(f"proof:{client_ip(request)}", 60, 60, "Too many requests. Try again in a minute.")
    order, _ = _shared(db, token)
    photo = db.scalar(select(Photo).where(Photo.id == photo_id, Photo.order_id == order.id, Photo.kind == "pack"))
    if not photo:
        raise not_found("Photo not found.")
    return Response(
        content=photo.data,
        media_type=photo.content_type,
        headers={"Cache-Control": "private, max-age=3600", "Content-Security-Policy": "default-src 'none'"},
    )


@public_router.get("/proof/{token}")
def public_proof(token: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    memory_limiter.check(f"proof:{client_ip(request)}", 60, 60, "Too many requests. Try again in a minute.")
    order, wh = _shared(db, token)
    lines = order_svc.lines_for(db, order.id)
    by_line: dict[uuid.UUID, OrderLineItem] = {li.id: li for li in lines}
    voided = select(ScanEvent.voids_scan_id).where(ScanEvent.order_id == order.id, ScanEvent.voids_scan_id.is_not(None))
    scans = list(
        db.scalars(
            select(ScanEvent)
            .where(ScanEvent.order_id == order.id, ScanEvent.result == ScanResult.match, ScanEvent.id.not_in(voided))
            .order_by(ScanEvent.client_scanned_at, ScanEvent.client_seq)
        )
    )
    caught = (
        db.scalar(
            select(func.count())
            .select_from(ScanEvent)
            .where(ScanEvent.order_id == order.id, ScanEvent.result.in_(dash.ERROR_RESULTS))
        )
        or 0
    )
    tz = dash.tz_of(wh)
    return {
        "warehouse": wh.name,
        "order_number": order.external_order_number,
        "customer": order.customer,
        "status": order.status.value,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "tracking_number": order.tracking_number,
        "carrier": order.carrier,
        "timezone": tz.key,
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
        "pack_photos": [str(pid) for pid in order_svc.pack_photo_ids(db, order.id)],
        "generated_at": utcnow().isoformat(),
    }
