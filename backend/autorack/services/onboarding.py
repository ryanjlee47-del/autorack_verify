"""First-run checklist and one-click sample orders.

A new warehouse should be able to scan its first order within minutes of
signing up, before anyone exports a real pick list. The checklist is derived
from real data (nothing to keep in sync), and the sample orders use the same
products as the printable test barcodes in samples/.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..errors import conflict
from ..models import Device, Order, OrderSource, OrderStatus, ScanEvent, Warehouse, Worker
from . import csv_import, usage
from .audit import Actor

SAMPLE_CSV = Path(__file__).resolve().parents[1] / "data" / "sample-orders.csv"


def _any(db: Session, stmt: Any) -> bool:
    return bool(db.scalar(select(exists(stmt))))


def checklist(db: Session, wh: Warehouse) -> dict[str, Any]:
    has_device = _any(db, select(Device.id).where(Device.warehouse_id == wh.id, Device.revoked_at.is_(None)))
    has_worker = _any(db, select(Worker.id).where(Worker.warehouse_id == wh.id, Worker.active.is_(True)))
    has_orders = _any(db, select(Order.id).where(Order.warehouse_id == wh.id))
    has_real_orders = _any(db, select(Order.id).where(Order.warehouse_id == wh.id, Order.source != OrderSource.sample))
    has_scan = _any(db, select(ScanEvent.id).where(ScanEvent.warehouse_id == wh.id))
    has_complete = _any(
        db,
        select(Order.id).where(
            Order.warehouse_id == wh.id, Order.status.in_([OrderStatus.completed, OrderStatus.shipped])
        ),
    )
    has_samples = _any(db, select(Order.id).where(Order.warehouse_id == wh.id, Order.source == OrderSource.sample))
    steps = [
        {"key": "worker", "label": "Add a worker and give them their PIN", "done": has_worker, "href": "#/workers"},
        {"key": "phone", "label": "Link a phone with the setup QR code", "done": has_device, "href": "#/devices"},
        {"key": "orders", "label": "Import orders, or load the sample orders", "done": has_orders, "href": "#/orders"},
        {"key": "scan", "label": "Scan the first item on a phone", "done": has_scan, "href": "/w/"},
        {"key": "complete", "label": "Finish picking a whole order", "done": has_complete, "href": "#/orders"},
        {"key": "real", "label": "Import your own pick list", "done": has_real_orders, "href": "#/orders"},
    ]
    done = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "done": done,
        "total": len(steps),
        "complete": done == len(steps),
        "dismissed": wh.onboarding_dismissed,
        "sample_loaded": has_samples,
        "sample_barcodes_url": "/assets/samples/sample-barcodes.pdf",
    }


def load_sample(db: Session, wh: Warehouse, actor: Actor, user_id: uuid.UUID | None) -> dict[str, Any]:
    if _any(db, select(Order.id).where(Order.warehouse_id == wh.id, Order.source == OrderSource.sample)):
        raise conflict("sample_loaded", "The sample orders are already loaded.")
    batch = csv_import.commit(
        db, wh, SAMPLE_CSV.read_bytes(), "sample-orders.csv", actor, user_id, source=OrderSource.sample
    )
    usage.track(db, wh.id, "orders.sample")
    db.commit()
    return {"orders_created": batch.orders_created, "lines_created": batch.lines_created}
