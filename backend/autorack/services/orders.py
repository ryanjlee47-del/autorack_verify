"""Orders and line items: creation, editing, status, and the match index."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import matching
from ..errors import bad_request, conflict, not_found
from ..models import (
    BarcodeAlias,
    Order,
    OrderFlag,
    OrderLineItem,
    OrderSource,
    OrderStatus,
    ScanEvent,
    Warehouse,
    utcnow,
)
from . import audit
from .audit import Actor

MAX_LINES_PER_ORDER = 1000


@dataclass
class LineInput:
    barcode: str
    quantity: int = 1
    sku: str | None = None
    description: str | None = None
    location: str | None = None


def clean(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s[:limit] if s else None


def validate_barcode(raw: str) -> str:
    # Keep the owner's exact string (tier 0 matches it byte for byte), minus
    # surrounding whitespace, which never belongs to a barcode.
    value = raw.strip()
    if not value or not matching.normalized_key(value):
        raise bad_request("barcode_required", "Every line needs a barcode.")
    if len(value) > 200:
        raise bad_request("barcode_too_long", "Barcodes are limited to 200 characters.")
    return value


def get_order(db: Session, warehouse_id: uuid.UUID, order_id: uuid.UUID, *, lock: bool = False) -> Order:
    stmt = select(Order).where(Order.id == order_id, Order.warehouse_id == warehouse_id)
    if lock:
        stmt = stmt.with_for_update()
    order = db.scalar(stmt)
    if not order:
        raise not_found("Order not found")
    return order


def lines_for(db: Session, order_id: uuid.UUID) -> list[OrderLineItem]:
    return list(
        db.scalars(select(OrderLineItem).where(OrderLineItem.order_id == order_id).order_by(OrderLineItem.line_no))
    )


def merge_line_inputs(lines: list[LineInput]) -> list[LineInput]:
    """Collapse lines that are the same barcode (after normalization) into
    one line with the summed quantity. Two lines for one barcode would make
    every scan of it ambiguous."""
    merged: dict[str, LineInput] = {}
    for li in lines:
        key = matching.normalized_key(li.barcode)
        if key in merged:
            merged[key].quantity += li.quantity
            for attr in ("sku", "description", "location"):
                if not getattr(merged[key], attr) and getattr(li, attr):
                    setattr(merged[key], attr, getattr(li, attr))
        else:
            merged[key] = LineInput(li.barcode, li.quantity, li.sku, li.description, li.location)
    return list(merged.values())


def create_order(
    db: Session,
    wh: Warehouse,
    *,
    external_order_number: str | None,
    lines: list[LineInput],
    notes: str | None = None,
    source: OrderSource = OrderSource.manual,
    import_batch_id: uuid.UUID | None = None,
    created_by_user_id: uuid.UUID | None = None,
    assigned_worker_id: uuid.UUID | None = None,
    customer: str | None = None,
    actor: Actor | None = None,
) -> Order:
    number = clean(external_order_number, 100)
    if not lines:
        raise bad_request("lines_required", "An order needs at least one line.")
    if len(lines) > MAX_LINES_PER_ORDER:
        raise bad_request("too_many_lines", f"Orders are limited to {MAX_LINES_PER_ORDER} lines.")
    if number and order_number_in_use(db, wh.id, number):
        raise conflict("order_number_taken", f"Order {number} already exists.")
    order = Order(
        warehouse_id=wh.id,
        external_order_number=number,
        customer=clean(customer, 200),
        notes=clean(notes, 2000),
        source=source,
        import_batch_id=import_batch_id,
        created_by_user_id=created_by_user_id,
        assigned_worker_id=assigned_worker_id,
    )
    db.add(order)
    db.flush()
    for i, li in enumerate(merge_line_inputs(lines), start=1):
        db.add(_new_line(wh.id, order.id, i, li))
    db.flush()
    if actor:
        audit.record(
            db,
            actor,
            "order.created",
            warehouse_id=wh.id,
            target_type="order",
            target_id=order.id,
            number=number,
            lines=len(lines),
            source=source.value,
        )
    return order


def _new_line(warehouse_id: uuid.UUID, order_id: uuid.UUID, line_no: int, li: LineInput) -> OrderLineItem:
    barcode = validate_barcode(li.barcode)
    if li.quantity < 1 or li.quantity > 100_000:
        raise bad_request("quantity_invalid", "Quantity must be between 1 and 100,000.")
    return OrderLineItem(
        warehouse_id=warehouse_id,
        order_id=order_id,
        line_no=line_no,
        expected_barcode=barcode,
        normalized_barcode=matching.normalized_key(barcode),
        expected_quantity=li.quantity,
        scanned_quantity=0,
        sku=clean(li.sku, 100),
        sku_description=clean(li.description, 500),
        location=clean(li.location, 100),
    )


def order_number_in_use(db: Session, warehouse_id: uuid.UUID, number: str, exclude: uuid.UUID | None = None) -> bool:
    stmt = select(Order.id).where(
        Order.warehouse_id == warehouse_id,
        Order.external_order_number == number,
        Order.status != OrderStatus.cancelled,
    )
    if exclude:
        stmt = stmt.where(Order.id != exclude)
    return db.scalar(stmt) is not None


def ensure_editable(order: Order) -> None:
    if order.status == OrderStatus.cancelled:
        raise conflict("order_cancelled", "This order was cancelled.")
    if order.status == OrderStatus.shipped:
        raise conflict("order_shipped", "This order has shipped, so it can't be changed.")


def add_line(db: Session, wh: Warehouse, order: Order, li: LineInput, actor: Actor) -> OrderLineItem:
    ensure_editable(order)
    existing = lines_for(db, order.id)
    if len(existing) >= MAX_LINES_PER_ORDER:
        raise bad_request("too_many_lines", f"Orders are limited to {MAX_LINES_PER_ORDER} lines.")
    key = matching.normalized_key(validate_barcode(li.barcode))
    for line in existing:
        if line.normalized_barcode == key:
            raise conflict("duplicate_line", "That barcode is already on this order. Edit its quantity instead.")
    line = _new_line(wh.id, order.id, max((x.line_no for x in existing), default=0) + 1, li)
    db.add(line)
    db.flush()
    bump(order)
    audit.record(
        db, actor, "order.line_added", warehouse_id=wh.id, target_type="order", target_id=order.id, line_id=line.id
    )
    recompute_status(db, order)
    return line


def update_line(db: Session, order: Order, line: OrderLineItem, changes: dict[str, Any], actor: Actor) -> OrderLineItem:
    ensure_editable(order)
    has_scans = line_has_scans(db, line.id)
    if "barcode" in changes and changes["barcode"] is not None:
        barcode = validate_barcode(changes["barcode"])
        if barcode != line.expected_barcode:
            if has_scans:
                raise conflict(
                    "line_has_scans",
                    "This line already has scans, so its barcode can't change. Add a new line instead.",
                )
            key = matching.normalized_key(barcode)
            for other in lines_for(db, order.id):
                if other.id != line.id and other.normalized_barcode == key:
                    raise conflict("duplicate_line", "That barcode is already on this order.")
            line.expected_barcode = barcode
            line.normalized_barcode = key
    if "quantity" in changes and changes["quantity"] is not None:
        q = int(changes["quantity"])
        if q < 1 or q > 100_000:
            raise bad_request("quantity_invalid", "Quantity must be between 1 and 100,000.")
        line.expected_quantity = q
    for field, attr, limit in (
        ("sku", "sku", 100),
        ("description", "sku_description", 500),
        ("location", "location", 100),
    ):
        if field in changes:
            setattr(line, attr, clean(changes[field], limit))
    bump(order)
    audit.record(
        db,
        actor,
        "order.line_updated",
        warehouse_id=order.warehouse_id,
        target_type="order",
        target_id=order.id,
        line_id=line.id,
        changes={k: v for k, v in changes.items() if v is not None},
    )
    recompute_status(db, order)
    return line


def delete_line(db: Session, order: Order, line: OrderLineItem, actor: Actor) -> None:
    ensure_editable(order)
    if line_has_scans(db, line.id) or db.scalar(select(OrderFlag.id).where(OrderFlag.line_item_id == line.id)):
        raise conflict("line_has_scans", "This line has scans or flags recorded against it, so it can't be deleted.")
    if len(lines_for(db, order.id)) <= 1:
        raise conflict("last_line", "An order needs at least one line. Cancel the order instead.")
    db.delete(line)
    db.flush()
    bump(order)
    audit.record(
        db,
        actor,
        "order.line_deleted",
        warehouse_id=order.warehouse_id,
        target_type="order",
        target_id=order.id,
        barcode=line.expected_barcode,
    )
    recompute_status(db, order)


def line_has_scans(db: Session, line_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(ScanEvent.id).where(
                (ScanEvent.line_item_id == line_id) | (ScanEvent.intended_line_item_id == line_id)
            )
        )
        is not None
    )


def cancel_order(db: Session, order: Order, actor: Actor) -> None:
    if order.status == OrderStatus.cancelled:
        return
    if order.status == OrderStatus.shipped:
        raise conflict("order_shipped", "This order has already shipped.")
    order.status = OrderStatus.cancelled
    order.cancelled_at = utcnow()
    bump(order)
    audit.record(db, actor, "order.cancelled", warehouse_id=order.warehouse_id, target_type="order", target_id=order.id)


def bump(order: Order) -> None:
    order.version = (order.version or 1) + 1
    order.updated_at = utcnow()


def open_flag_count(db: Session, order_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(OrderFlag)
            .where(OrderFlag.order_id == order_id, OrderFlag.resolved_at.is_(None))
        )
        or 0
    )


def recompute_status(db: Session, order: Order, lines: list[OrderLineItem] | None = None) -> None:
    """Derive the order's status from its lines and open flags.

    pending -> in_progress on the first scan; completed when every line has
    its full quantity (or the rest was reported short); flagged while any
    worker flag is unresolved (it wins over completed: a flagged order needs a
    human before it ships). Cancelled and shipped are final.
    """
    if order.status in (OrderStatus.cancelled, OrderStatus.shipped):
        return
    lines = lines if lines is not None else lines_for(db, order.id)
    all_done = bool(lines) and all(line_done(li) for li in lines)
    before = order.status
    if open_flag_count(db, order.id):
        order.status = OrderStatus.flagged
    elif all_done:
        order.status = OrderStatus.completed
    elif order.started_at is not None:
        order.status = OrderStatus.in_progress
    else:
        order.status = OrderStatus.pending
    if order.status == OrderStatus.completed:
        order.completed_at = order.completed_at or utcnow()
    elif not all_done:
        order.completed_at = None
    if before != order.status:
        bump(order)


def line_done(li: OrderLineItem) -> bool:
    return li.scanned_quantity + li.short_quantity >= li.expected_quantity


def line_remaining(li: OrderLineItem) -> int:
    return max(0, li.expected_quantity - li.scanned_quantity - li.short_quantity)


# ---------------------------------------------------------------------------
# Shipping labels
# ---------------------------------------------------------------------------

_CARRIER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("UPS", re.compile(r"^1Z[0-9A-Z]{16}$")),
    # USPS labels often carry a 420+ZIP routing prefix before the number.
    ("USPS", re.compile(r"^(420\d{5}(\d{4})?)?9[2-5]\d{20}$|^[A-Z]{2}\d{9}US$")),
    ("FedEx", re.compile(r"^(\d{12}|\d{15}|\d{20}|96\d{20}|\d{34})$")),
    ("DHL", re.compile(r"^(JD\d{18}|\d{10})$")),
    ("Amazon", re.compile(r"^TBA\d{12}$")),
]


def normalize_tracking(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw or "").upper()[:100]


def guess_carrier(tracking: str) -> str | None:
    t = normalize_tracking(tracking)
    for name, pattern in _CARRIER_PATTERNS:
        if pattern.match(t):
            return name
    return None


def tracking_in_use(db: Session, warehouse_id: uuid.UUID, tracking: str, exclude: uuid.UUID) -> Order | None:
    return db.scalar(
        select(Order).where(
            Order.warehouse_id == warehouse_id,
            Order.tracking_number == tracking,
            Order.id != exclude,
        )
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def aliases_for(db: Session, warehouse_id: uuid.UUID, target_keys: set[str]) -> list[BarcodeAlias]:
    if not target_keys:
        return []
    return list(
        db.scalars(
            select(BarcodeAlias).where(
                BarcodeAlias.warehouse_id == warehouse_id, BarcodeAlias.target_key.in_(target_keys)
            )
        )
    )


def build_index(db: Session, wh: Warehouse, lines: list[OrderLineItem]) -> matching.MatchIndex:
    index = matching.MatchIndex(loose_match_enabled=wh.loose_match_enabled, suffix_len=wh.suffix_len)
    by_key: dict[str, list[OrderLineItem]] = {}
    for line in lines:
        index.add_line(str(line.id), line.expected_barcode)
        by_key.setdefault(line.normalized_barcode, []).append(line)
    for alias in aliases_for(db, wh.id, set(by_key)):
        for line in by_key.get(alias.target_key, []):
            index.add_alias(alias.alias_key, str(line.id))
    report = matching.analyze_collisions([(str(li.id), li.expected_barcode) for li in lines], suffix_len=wh.suffix_len)
    matching.apply_collision_report(index, report)
    return index


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def line_dict(line: OrderLineItem) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "line_no": line.line_no,
        "expected_barcode": line.expected_barcode,
        "expected_quantity": line.expected_quantity,
        "scanned_quantity": line.scanned_quantity,
        "short_quantity": line.short_quantity,
        "sku": line.sku,
        "description": line.sku_description,
        "location": line.location,
    }


def order_summary(order: Order, lines: list[OrderLineItem]) -> dict[str, Any]:
    return {
        "id": str(order.id),
        "external_order_number": order.external_order_number,
        "customer": order.customer,
        "status": order.status.value,
        "source": order.source.value,
        "version": order.version,
        "notes": order.notes,
        "assigned_worker_id": str(order.assigned_worker_id) if order.assigned_worker_id else None,
        "created_at": order.created_at.isoformat(),
        "started_at": order.started_at.isoformat() if order.started_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "tracking_number": order.tracking_number,
        "carrier": order.carrier,
        "line_count": len(lines),
        "units_expected": sum(li.expected_quantity for li in lines),
        "units_scanned": sum(min(li.scanned_quantity, li.expected_quantity) for li in lines),
        "units_short": sum(li.short_quantity for li in lines),
    }


def offline_payload(db: Session, wh: Warehouse, order: Order) -> dict[str, Any]:
    """Everything a phone needs to verify this order with no network."""
    lines = lines_for(db, order.id)
    index = build_index(db, wh, lines)
    return {
        **order_summary(order, lines),
        "lines": [line_dict(li) for li in lines],
        "match": {
            "loose_match_enabled": wh.loose_match_enabled,
            "suffix_len": wh.suffix_len,
            "index": [{"line_id": lid, "tier": tier, "key": key} for lid, tier, key in index.rows()],
            "disabled_keys": index.disabled_rows(),
        },
        "open_flags": open_flag_count(db, order.id),
        "require_ship_scan": wh.require_ship_scan,
        "fetched_at": utcnow().isoformat(),
    }


def order_qr_payload(order: Order) -> str:
    return f"AUTORACK:ORDER:{order.id}"


def parse_order_qr(text: str) -> uuid.UUID | None:
    prefix = "AUTORACK:ORDER:"
    t = text.strip()
    if t.upper().startswith(prefix):
        try:
            return uuid.UUID(t[len(prefix) :])
        except ValueError:
            return None
    return None
