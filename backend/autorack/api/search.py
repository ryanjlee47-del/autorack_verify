"""One search box for the whole dashboard: orders (number, customer,
tracking, barcode, SKU, lot, serial), products, workers and clients."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import OwnerContext, current_owner
from ..models import Client, Order, OrderLineItem, Package, Product, ScanEvent, Worker
from ..services import orders as order_svc

router = APIRouter(tags=["search"])

LIMIT = 6


@router.get("/search")
def search(
    q: str = Query(..., min_length=1, max_length=100),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    wid = ctx.warehouse.id
    text = q.strip()
    like = f"%{text}%"
    tracking = order_svc.normalize_tracking(text)
    order_id = order_svc.parse_order_qr(text)
    conds = [
        Order.external_order_number.ilike(like),
        Order.customer.ilike(like),
        Order.tracking_number == tracking,
        Order.id.in_(select(Package.order_id).where(Package.warehouse_id == wid, Package.tracking_number == tracking)),
        Order.id.in_(
            select(OrderLineItem.order_id).where(
                OrderLineItem.warehouse_id == wid,
                or_(OrderLineItem.expected_barcode == text, func.upper(OrderLineItem.sku) == text.upper()),
            )
        ),
        Order.id.in_(
            select(ScanEvent.order_id).where(
                ScanEvent.warehouse_id == wid,
                or_(func.upper(ScanEvent.lot) == text.upper(), ScanEvent.serial == text),
            )
        ),
    ]
    if order_id:
        conds.append(Order.id == order_id)
    orders = db.scalars(
        select(Order).where(Order.warehouse_id == wid, or_(*conds)).order_by(Order.created_at.desc()).limit(LIMIT)
    )
    products = db.scalars(
        select(Product)
        .where(
            Product.warehouse_id == wid,
            Product.active.is_(True),
            or_(Product.name.ilike(like), Product.sku.ilike(like), Product.barcode == text),
        )
        .order_by(Product.name)
        .limit(LIMIT)
    )
    workers = db.scalars(
        select(Worker).where(Worker.warehouse_id == wid, Worker.name.ilike(like)).order_by(Worker.name).limit(LIMIT)
    )
    clients = db.scalars(
        select(Client)
        .where(Client.warehouse_id == wid, or_(Client.name.ilike(like), Client.code.ilike(like)))
        .order_by(Client.name)
        .limit(LIMIT)
    )
    return {
        "orders": [
            {
                "id": str(o.id),
                "number": o.external_order_number,
                "customer": o.customer,
                "kind": o.kind.value,
                "status": o.status.value,
                "tracking_number": o.tracking_number,
            }
            for o in orders
        ],
        "products": [{"id": str(p.id), "name": p.name, "sku": p.sku, "barcode": p.barcode} for p in products],
        "workers": [{"id": str(w.id), "name": w.name, "active": w.active} for w in workers],
        "clients": [{"id": str(c.id), "name": c.name} for c in clients],
    }
