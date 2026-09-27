"""What a 3PL bills each client for a month: its own rates times the work the
scan log shows was done for that client. Nothing here is estimated: every
count comes from orders, boxes, inserts and returns recorded on the floor."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Client,
    Order,
    OrderInsertCheck,
    OrderKind,
    OrderLineItem,
    OrderStatus,
    Package,
    Warehouse,
)
from .dashboard import day_bounds
from .monthly import month_range


@dataclass(frozen=True)
class Rate:
    key: str
    label: str
    unit: str


# In the order they appear on a statement. Amounts are cents.
RATES = (
    Rate("monthly_fee", "Monthly account fee", "month"),
    Rate("per_order", "Orders shipped", "order"),
    Rate("per_unit", "Units picked and verified", "unit"),
    Rate("per_extra_box", "Extra boxes (after the first per order)", "box"),
    Rate("per_insert", "Inserts packed", "insert"),
    Rate("per_return", "Returns checked in", "return"),
    Rate("per_receive_unit", "Units received", "unit"),
)
RATE_KEYS = {r.key for r in RATES}
MAX_RATE_CENTS = 10_000_000


def clean_rates(rates: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in rates.items():
        if k not in RATE_KEYS:
            raise ValueError(f"Unknown rate {k}.")
        n = int(v)
        if not 0 <= n <= MAX_RATE_CENTS:
            raise ValueError("Rates must be between $0 and $100,000.")
        if n:
            out[k] = n
    return out


def period(wh: Warehouse, year: int, month: int) -> tuple[datetime, datetime]:
    first, last = month_range(year, month)
    start, _, _ = day_bounds(wh, first)
    _, end, _ = day_bounds(wh, last)
    return start, end


def usage(db: Session, wh: Warehouse, client_id: uuid.UUID, start: datetime, end: datetime) -> dict[str, int]:
    """The billable work done for one client in [start, end)."""
    out_the_door = func.coalesce(Order.shipped_at, Order.completed_at)
    shipped_ids = select(Order.id).where(
        Order.warehouse_id == wh.id,
        Order.client_id == client_id,
        Order.kind == OrderKind.pick,
        Order.status.in_([OrderStatus.shipped, OrderStatus.completed]),
        out_the_door >= start,
        out_the_door < end,
    )
    orders = db.scalar(select(func.count()).select_from(shipped_ids.subquery())) or 0
    units = (
        db.scalar(
            select(func.sum(func.least(OrderLineItem.scanned_quantity, OrderLineItem.expected_quantity))).where(
                OrderLineItem.order_id.in_(shipped_ids)
            )
        )
        or 0
    )
    boxes_per_order = (
        select(Package.order_id, func.count().label("n"))
        .where(Package.order_id.in_(shipped_ids))
        .group_by(Package.order_id)
        .subquery()
    )
    extra_boxes = db.scalar(select(func.sum(boxes_per_order.c.n - 1))) or 0
    inserts = (
        db.scalar(select(func.count()).select_from(OrderInsertCheck).where(OrderInsertCheck.order_id.in_(shipped_ids)))
        or 0
    )

    def tally(kind: OrderKind) -> Any:
        return select(Order.id).where(
            Order.warehouse_id == wh.id,
            Order.client_id == client_id,
            Order.kind == kind,
            Order.completed_at >= start,
            Order.completed_at < end,
            Order.status != OrderStatus.cancelled,
        )

    returns = db.scalar(select(func.count()).select_from(tally(OrderKind.ret).subquery())) or 0
    received = (
        db.scalar(
            select(func.sum(OrderLineItem.scanned_quantity)).where(OrderLineItem.order_id.in_(tally(OrderKind.receive)))
        )
        or 0
    )
    return {
        "monthly_fee": 1,
        "per_order": int(orders),
        "per_unit": int(units),
        "per_extra_box": int(extra_boxes),
        "per_insert": int(inserts),
        "per_return": int(returns),
        "per_receive_unit": int(received),
    }


def statement(db: Session, wh: Warehouse, client: Client, year: int, month: int) -> dict[str, Any]:
    start, end = period(wh, year, month)
    counts = usage(db, wh, client.id, start, end)
    rates = client.rates or {}
    lines = []
    for r in RATES:
        rate = int(rates.get(r.key, 0))
        qty = counts[r.key]
        if r.key == "monthly_fee" and not rate:
            continue
        lines.append(
            {
                "key": r.key,
                "label": r.label,
                "unit": r.unit,
                "quantity": qty,
                "rate_cents": rate,
                "amount_cents": qty * rate,
            }
        )
    return {
        "client": {"id": str(client.id), "name": client.name, "code": client.code},
        "warehouse": wh.name,
        "month": f"{year:04d}-{month:02d}",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "lines": lines,
        "total_cents": sum(line["amount_cents"] for line in lines),
        "rates_set": bool(rates),
    }
