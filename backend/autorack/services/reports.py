"""Date-range reports: by customer, by SKU, by worker, by day.

The same numbers the dashboard shows for "today", over any range, grouped the
way a warehouse gets asked about them: "how did we do for Acme last month?",
"which products keep going out wrong?", "who needs retraining?".

Scan-based numbers (units, mistakes caught) use the time the scan happened on
the phone; order-based numbers (completed, shipped) use completion and ship
times. Ranges are whole local days, inclusive.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import Session

from ..models import (
    Order,
    OrderFlag,
    OrderLineItem,
    OrderStatus,
    ScanEvent,
    ScanResult,
    Warehouse,
    Worker,
    utcnow,
)
from .dashboard import day_bounds, tz_of

MAX_RANGE_DAYS = 400
NO_CUSTOMER = "(no customer)"


def _sum(cond: Any) -> Any:
    return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)


def _accuracy(matches: int, errors: int) -> float | None:
    attempts = matches + errors
    return round(matches / attempts, 4) if attempts else None


def build(db: Session, wh: Warehouse, start_day: date, end_day: date) -> dict[str, Any]:
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    end_day = min(end_day, start_day + timedelta(days=MAX_RANGE_DAYS - 1))
    start, _, _ = day_bounds(wh, start_day)
    _, end, _ = day_bounds(wh, end_day)
    cost = wh.cost_per_error_cents

    scan_window = (
        ScanEvent.warehouse_id == wh.id,
        ScanEvent.client_scanned_at >= start,
        ScanEvent.client_scanned_at < end,
    )
    is_match = ScanEvent.result == ScanResult.match
    is_void = ScanEvent.result == ScanResult.void
    is_error = ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick])

    # --- totals -------------------------------------------------------------
    m, v, e, rv = db.execute(
        select(_sum(is_match), _sum(is_void), _sum(is_error), _sum(ScanEvent.result == ScanResult.review)).where(
            *scan_window
        )
    ).one()
    completed = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.warehouse_id == wh.id,
                Order.status.in_([OrderStatus.completed, OrderStatus.shipped]),
                Order.completed_at >= start,
                Order.completed_at < end,
            )
        )
        or 0
    )
    shipped = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.warehouse_id == wh.id, Order.shipped_at >= start, Order.shipped_at < end)
        )
        or 0
    )
    flag_window = (OrderFlag.warehouse_id == wh.id, OrderFlag.created_at >= start, OrderFlag.created_at < end)
    flags, short_units = db.execute(
        select(func.count(), func.coalesce(func.sum(OrderFlag.short_quantity), 0)).where(*flag_window)
    ).one()
    totals = {
        "orders_completed": completed,
        "orders_shipped": shipped,
        "units_picked": int(m) - int(v),
        "errors_caught": int(e),
        "reviews": int(rv),
        "accuracy": _accuracy(int(m), int(e)),
        "money_saved_cents": int(e) * cost,
        "problems_reported": int(flags),
        "units_short": int(short_units),
    }

    # --- by customer ----------------------------------------------------------
    cust = func.coalesce(func.nullif(func.trim(Order.customer), ""), NO_CUSTOMER)
    by_customer: dict[str, dict[str, Any]] = {}
    for name, units, voids, errors, orders in db.execute(
        select(cust, _sum(is_match), _sum(is_void), _sum(is_error), func.count(func.distinct(ScanEvent.order_id)))
        .join(Order, Order.id == ScanEvent.order_id)
        .where(*scan_window)
        .group_by(cust)
    ):
        by_customer[name] = {
            "customer": name,
            "orders_touched": int(orders),
            "units_picked": int(units) - int(voids),
            "errors_caught": int(errors),
            "accuracy": _accuracy(int(units), int(errors)),
            "orders_shipped": 0,
            "units_short": 0,
        }
    for name, n in db.execute(
        select(cust, func.count())
        .where(Order.warehouse_id == wh.id, Order.shipped_at >= start, Order.shipped_at < end)
        .group_by(cust)
    ):
        by_customer.setdefault(name, _blank_customer(name))["orders_shipped"] = int(n)
    for name, n in db.execute(
        select(cust, func.coalesce(func.sum(OrderFlag.short_quantity), 0))
        .join(Order, Order.id == OrderFlag.order_id)
        .where(*flag_window, OrderFlag.short_quantity.is_not(None))
        .group_by(cust)
    ):
        by_customer.setdefault(name, _blank_customer(name))["units_short"] = int(n)
    customers = sorted(by_customer.values(), key=lambda r: (-r["units_picked"], r["customer"]))
    for r in customers:
        r["money_saved_cents"] = r["errors_caught"] * cost

    # --- by SKU -------------------------------------------------------------------
    counted = OrderLineItem
    sku_rows: dict[str, dict[str, Any]] = {}
    for key, sku, desc, units, voids in db.execute(
        select(
            counted.normalized_barcode,
            func.max(counted.sku),
            func.max(counted.sku_description),
            _sum(is_match),
            _sum(is_void),
        )
        .join(ScanEvent, ScanEvent.line_item_id == counted.id)
        .where(*scan_window)
        .group_by(counted.normalized_barcode)
    ):
        sku_rows[key] = _blank_sku(key, sku, desc)
        sku_rows[key]["units_picked"] = int(units) - int(voids)
    for key, sku, desc, n in db.execute(
        select(counted.normalized_barcode, func.max(counted.sku), func.max(counted.sku_description), func.count())
        .join(ScanEvent, ScanEvent.intended_line_item_id == counted.id)
        .where(*scan_window, ScanEvent.result == ScanResult.mismatch)
        .group_by(counted.normalized_barcode)
    ):
        sku_rows.setdefault(key, _blank_sku(key, sku, desc))["mispicks"] = int(n)
    for key, sku, desc, n in db.execute(
        select(
            counted.normalized_barcode,
            func.max(counted.sku),
            func.max(counted.sku_description),
            func.coalesce(func.sum(OrderFlag.short_quantity), 0),
        )
        .join(OrderFlag, OrderFlag.line_item_id == counted.id)
        .where(*flag_window, OrderFlag.short_quantity.is_not(None))
        .group_by(counted.normalized_barcode)
    ):
        sku_rows.setdefault(key, _blank_sku(key, sku, desc))["units_short"] = int(n)
    skus = sorted(sku_rows.values(), key=lambda r: (-r["mispicks"], -r["units_short"], -r["units_picked"]))

    # --- by worker ------------------------------------------------------------------
    names = {wid: name for wid, name in db.execute(select(Worker.id, Worker.name).where(Worker.warehouse_id == wh.id))}
    workers = []
    shorts_by_worker = {
        wid: int(n)
        for wid, n in db.execute(
            select(OrderFlag.worker_id, func.count())
            .where(*flag_window, OrderFlag.short_quantity.is_not(None))
            .group_by(OrderFlag.worker_id)
        )
    }
    for wid, units, voids, errors, reviews, orders in db.execute(
        select(
            ScanEvent.worker_id,
            _sum(is_match),
            _sum(is_void),
            _sum(is_error),
            _sum(ScanEvent.result == ScanResult.review),
            func.count(func.distinct(ScanEvent.order_id)),
        )
        .where(*scan_window)
        .group_by(ScanEvent.worker_id)
    ):
        workers.append(
            {
                "worker_id": str(wid),
                "name": names.get(wid, "?"),
                "orders": int(orders),
                "units_picked": int(units) - int(voids),
                "errors": int(errors),
                "reviews": int(reviews),
                "short_reports": shorts_by_worker.get(wid, 0),
                "accuracy": _accuracy(int(units), int(errors)),
            }
        )
    workers.sort(key=lambda r: (-r["units_picked"], r["name"]))

    # --- by day -------------------------------------------------------------------------
    tz = tz_of(wh)
    local_day = cast(func.timezone(tz.key, ScanEvent.client_scanned_at), Date)
    buckets = {
        d: (int(u) - int(vv), int(er))
        for d, u, vv, er in db.execute(
            select(local_day, _sum(is_match), _sum(is_void), _sum(is_error)).where(*scan_window).group_by(local_day)
        )
    }
    days = []
    d = start_day
    while d <= end_day:
        units, errors = buckets.get(d, (0, 0))
        days.append({"date": d.isoformat(), "units_picked": units, "errors_caught": errors})
        d += timedelta(days=1)

    return {
        "warehouse": wh.name,
        "from": start_day.isoformat(),
        "to": end_day.isoformat(),
        "timezone": wh.timezone,
        "cost_per_error_cents": cost,
        "totals": totals,
        "customers": customers,
        "skus": skus[:200],
        "workers": workers,
        "days": days,
        "generated_at": utcnow().isoformat(),
    }


def _blank_customer(name: str) -> dict[str, Any]:
    return {
        "customer": name,
        "orders_touched": 0,
        "units_picked": 0,
        "errors_caught": 0,
        "accuracy": None,
        "orders_shipped": 0,
        "units_short": 0,
    }


def _blank_sku(key: str, sku: str | None, desc: str | None) -> dict[str, Any]:
    return {"barcode": key, "sku": sku, "description": desc, "units_picked": 0, "mispicks": 0, "units_short": 0}


# ---------------------------------------------------------------------------
# Floor board (shift leaderboard)
# ---------------------------------------------------------------------------


def board(db: Session, wh: Warehouse) -> dict[str, Any]:
    """Today's shift, per worker: for a TV on the floor or a quick look.

    Ranked by units picked; accuracy is shown next to it so speed alone
    never wins. Off unless the warehouse turns it on.
    """
    start, end, day = day_bounds(wh)
    is_match = ScanEvent.result == ScanResult.match
    is_void = ScanEvent.result == ScanResult.void
    is_error = ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick])
    names = {wid: name for wid, name in db.execute(select(Worker.id, Worker.name).where(Worker.warehouse_id == wh.id))}
    done_orders: dict[uuid.UUID, int] = {
        wid: int(n)
        for wid, n in db.execute(
            select(ScanEvent.worker_id, func.count(func.distinct(ScanEvent.order_id)))
            .join(Order, Order.id == ScanEvent.order_id)
            .where(
                ScanEvent.warehouse_id == wh.id,
                ScanEvent.client_scanned_at >= start,
                ScanEvent.client_scanned_at < end,
                is_match,
                Order.status.in_([OrderStatus.completed, OrderStatus.shipped]),
            )
            .group_by(ScanEvent.worker_id)
        )
    }
    rows = []
    for wid, units, voids, errors, first, last in db.execute(
        select(
            ScanEvent.worker_id,
            _sum(is_match),
            _sum(is_void),
            _sum(is_error),
            func.min(ScanEvent.client_scanned_at),
            func.max(ScanEvent.client_scanned_at),
        )
        .where(ScanEvent.warehouse_id == wh.id, ScanEvent.client_scanned_at >= start, ScanEvent.client_scanned_at < end)
        .group_by(ScanEvent.worker_id)
    ):
        picked = int(units) - int(voids)
        hours = max((last - first).total_seconds() / 3600, 0.25) if first and last else None
        rows.append(
            {
                "name": names.get(wid, "?"),
                "units_picked": picked,
                "orders_completed": done_orders.get(wid, 0),
                "errors_caught": int(errors),
                "accuracy": _accuracy(int(units), int(errors)),
                "units_per_hour": round(picked / hours, 1) if hours else None,
                "last_scan_at": last.isoformat() if last else None,
            }
        )
    rows.sort(key=lambda r: (-r["units_picked"], r["name"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    open_orders = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.warehouse_id == wh.id,
                Order.status.in_([OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]),
            )
        )
        or 0
    )
    total_units = sum(r["units_picked"] for r in rows)
    total_errors = sum(r["errors_caught"] for r in rows)
    return {
        "date": day.isoformat(),
        "warehouse": wh.name,
        "workers": rows,
        "totals": {
            "units_picked": total_units,
            "orders_completed": sum(r["orders_completed"] for r in rows),
            "errors_caught": total_errors,
            "open_orders": open_orders,
            "money_saved_cents": total_errors * wh.cost_per_error_cents,
        },
        "generated_at": utcnow().isoformat(),
    }
