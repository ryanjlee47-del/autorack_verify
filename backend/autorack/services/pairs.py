"""Confused pairs: which two products keep getting swapped, and where each
one sits, so the owner can fix the cause (move a bin, relabel a shelf, add a
photo) instead of re-reading "most mistakes: Blue mug".

Built from wrong-item scans: the line the worker was picking says what they
wanted; the scanned barcode, looked up in the catalog (then in past orders),
says what they grabbed.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Order,
    OrderKind,
    OrderLineItem,
    Product,
    ProductBarcode,
    ScanEvent,
    ScanResult,
    Warehouse,
)


@dataclass
class Item:
    key: str
    name: str | None
    sku: str | None
    barcode: str
    location: str | None
    known: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sku": self.sku,
            "barcode": self.barcode,
            "location": self.location,
            "known": self.known,
        }


@dataclass
class Pair:
    a: Item
    b: Item
    count: int = 0
    last_at: datetime | None = None
    orders: set[uuid.UUID] = field(default_factory=set)


def _words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def _aisle(loc: str) -> str:
    """'A-03-2' -> 'A-03': the shelf, without the slot."""
    parts = re.split(r"[-./ ]+", loc.strip().upper())
    return "-".join(parts[:-1]) if len(parts) > 1 else parts[0]


def advice(a: Item, b: Item) -> tuple[str, str]:
    """(kind, sentence): the likeliest cause, and what to do about it."""
    if not b.known or not a.known:
        unknown = b if not b.known else a
        return (
            "unknown_barcode",
            f"Barcode {unknown.barcode} isn't in your catalog. If it's a case, inner pack or vendor label for "
            "the right product, add it as an extra barcode; if not, find where it's shelved.",
        )
    if a.location and b.location:
        if a.location.strip().upper() == b.location.strip().upper():
            return "same_bin", f"Both live in {a.location}. Give each its own bin."
        if _aisle(a.location) == _aisle(b.location):
            return (
                "neighbours",
                f"They sit side by side ({a.location} and {b.location}). "
                "Move one, or put a picture label on the shelf.",
            )
    wa, wb = _words(a.name), _words(b.name)
    if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.5:
        return "look_alike", "Look-alike items. Add product photos (the phone shows them while picking) or relabel."
    return "check_labels", "Check both shelf labels match what's in the bin."


def confused_pairs(
    db: Session,
    wh: Warehouse,
    since: datetime,
    until: datetime,
    *,
    client_id: uuid.UUID | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            ScanEvent.normalized_barcode,
            ScanEvent.scanned_barcode,
            ScanEvent.client_scanned_at,
            ScanEvent.order_id,
            OrderLineItem,
        )
        .join(OrderLineItem, OrderLineItem.id == ScanEvent.intended_line_item_id)
        .join(Order, Order.id == ScanEvent.order_id)
        .where(
            ScanEvent.warehouse_id == wh.id,
            ScanEvent.result == ScanResult.mismatch,
            ScanEvent.client_scanned_at >= since,
            ScanEvent.client_scanned_at < until,
            ScanEvent.normalized_barcode != "",
            Order.kind == OrderKind.pick,
        )
    )
    if client_id is not None:
        stmt = stmt.where(Order.client_id == client_id)
    rows = db.execute(stmt).all()
    if not rows:
        return []

    # What each scanned barcode is: the catalog first, then past order lines.
    keys = {nb for nb, *_ in rows}
    by_key: dict[str, Item] = {}
    for p in db.scalars(select(Product).where(Product.warehouse_id == wh.id, Product.normalized_barcode.in_(keys))):
        by_key[p.normalized_barcode or ""] = Item(f"p:{p.id}", p.name, p.sku, p.barcode or "", p.location)
    for pb, p in db.execute(
        select(ProductBarcode, Product)
        .join(Product, Product.id == ProductBarcode.product_id)
        .where(ProductBarcode.warehouse_id == wh.id, ProductBarcode.normalized_barcode.in_(keys))
    ):
        by_key.setdefault(pb.normalized_barcode, Item(f"p:{p.id}", p.name, p.sku, p.barcode or pb.barcode, p.location))
    missing = keys - by_key.keys()
    if missing:
        for li in db.scalars(
            select(OrderLineItem)
            .where(OrderLineItem.warehouse_id == wh.id, OrderLineItem.normalized_barcode.in_(missing))
            .order_by(OrderLineItem.id)
        ):
            by_key.setdefault(
                li.normalized_barcode,
                Item(
                    f"p:{li.product_id}" if li.product_id else f"b:{li.normalized_barcode}",
                    li.sku_description,
                    li.sku,
                    li.expected_barcode,
                    li.location,
                ),
            )

    pairs: dict[frozenset[str], Pair] = {}
    for nb, raw, at, order_id, line in rows:
        wanted = Item(
            f"p:{line.product_id}" if line.product_id else f"b:{line.normalized_barcode}",
            line.sku_description,
            line.sku,
            line.expected_barcode,
            line.location,
        )
        grabbed = by_key.get(nb) or Item(f"b:{nb}", None, None, raw, None, known=False)
        if grabbed.key == wanted.key:
            continue
        k = frozenset((wanted.key, grabbed.key))
        pair = pairs.get(k)
        if pair is None:
            pair = pairs[k] = Pair(wanted, grabbed)
        pair.count += 1
        pair.orders.add(order_id)
        pair.last_at = max(pair.last_at, at) if pair.last_at else at

    ranked = sorted(pairs.values(), key=lambda p: (-p.count, -(p.last_at.timestamp() if p.last_at else 0)))
    out = []
    for p in ranked[:limit]:
        kind, tip = advice(p.a, p.b)
        out.append(
            {
                "wanted": p.a.as_dict(),
                "scanned": p.b.as_dict(),
                "times": p.count,
                "orders": len(p.orders),
                "last_at": p.last_at.isoformat() if p.last_at else None,
                "cause": kind,
                "advice": tip,
            }
        )
    return out
