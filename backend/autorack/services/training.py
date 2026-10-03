"""Practice rounds: new workers learn the scanner on real products without
touching real orders, stock or anyone's numbers.

The practice order is built exactly like a real one (same catalog links,
same barcode matching rules) inside a savepoint that is rolled back, so
nothing is stored. The phone checks every practice scan itself, as it does
offline, and sends nothing but the round's summary at the end.
"""

from __future__ import annotations

import csv
import random  # which items to practise on: nothing secret
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import OrderLineItem, Product, TrainingRun, Warehouse, Worker, utcnow
from . import orders as order_svc
from .onboarding import SAMPLE_CSV

ITEMS = 4
MAX_UNITS = 3


def _pool(db: Session, wh: Warehouse) -> list[order_svc.LineInput]:
    """Things this warehouse really stocks: the catalog, else what recent
    orders asked for, else the printable sample barcodes."""
    products = list(
        db.scalars(
            select(Product)
            .where(
                Product.warehouse_id == wh.id,
                Product.active.is_(True),
                Product.barcode.is_not(None),
                Product.no_barcode.is_(False),
            )
            .order_by(func.random())
            .limit(40)
        )
    )
    if products:
        return [
            order_svc.LineInput(barcode=p.barcode or "", sku=p.sku, description=p.name, location=p.location)
            for p in products
        ]
    recent = db.execute(
        select(
            OrderLineItem.expected_barcode,
            func.max(OrderLineItem.sku),
            func.max(OrderLineItem.sku_description),
            func.max(OrderLineItem.location),
        )
        .where(OrderLineItem.warehouse_id == wh.id, OrderLineItem.expected_barcode != "")
        .group_by(OrderLineItem.expected_barcode)
        .order_by(func.random())
        .limit(40)
    ).all()
    if recent:
        return [order_svc.LineInput(barcode=b, sku=s, description=d, location=loc) for b, s, d, loc in recent]
    with SAMPLE_CSV.open(newline="") as fh:
        rows = {r["barcode"]: r for r in csv.DictReader(fh)}
    return [
        order_svc.LineInput(barcode=b, sku=r.get("sku"), description=r.get("description"), location=r.get("location"))
        for b, r in rows.items()
    ]


def practice_order(db: Session, wh: Warehouse, worker: Worker) -> dict[str, Any]:
    pool = _pool(db, wh)
    picks = random.sample(pool, min(ITEMS, len(pool)))
    for line in picks:
        line.quantity = random.randint(1, MAX_UNITS)  # noqa: S311
    savepoint = db.begin_nested()
    try:
        order = order_svc.create_order(
            db,
            wh,
            external_order_number=f"PRACTICE-{random.randint(100, 999)}",  # noqa: S311
            lines=picks,
            customer="Practice",
            notes=None,
        )
        payload = order_svc.offline_payload(db, wh, order)
    finally:
        savepoint.rollback()
    return {
        **payload,
        # Fresh ids: nothing the phone holds can be mistaken for a real order.
        "id": f"practice-{uuid.uuid4()}",
        "practice": True,
        "require_ship_scan": False,
        "require_pack_photo": False,
        "inserts": [],
        "notes": None,
        "worker": worker.name,
    }


def record(
    db: Session, wh: Warehouse, worker: Worker, units: int, scans: int, mistakes: int, seconds: int
) -> TrainingRun:
    run = TrainingRun(
        warehouse_id=wh.id,
        worker_id=worker.id,
        units=units,
        scans=scans,
        mistakes=mistakes,
        seconds=seconds,
    )
    db.add(run)
    db.flush()
    return run


def accuracy(units: int, mistakes: int) -> float | None:
    return units / (units + mistakes) if units + mistakes else None


def summary_by_worker(db: Session, warehouse_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
    """Per worker: rounds done, the last one, and how the last went."""
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for run in db.scalars(
        select(TrainingRun)
        .where(TrainingRun.warehouse_id == warehouse_id, TrainingRun.created_at >= utcnow() - timedelta(days=365))
        .order_by(TrainingRun.created_at)
    ):
        s = out.setdefault(run.worker_id, {"rounds": 0})
        s["rounds"] += 1
        s["last_at"] = run.created_at.isoformat()
        s["last_accuracy"] = accuracy(run.units, run.mistakes)
        s["last_seconds"] = run.seconds
    return out
