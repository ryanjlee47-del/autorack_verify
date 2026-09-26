"""Owner dashboard numbers.

The dashboard answers three questions at a glance:
  1. How many orders are in progress / completed today?
  2. How many mistakes were caught (the product's value, made visible)?
  3. Is any worker's error rate unusually high (training gap, bad labels)?

"Today" is the warehouse's local day. Time-bucketed stats use the time the
scan happened on the phone (clamped server-side against future skew), not the
time it synced, so an offline morning shows up in the morning.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, and_, case, cast, func, select
from sqlalchemy.orm import Session

from ..models import (
    Order,
    OrderFlag,
    OrderLineItem,
    OrderStatus,
    Photo,
    ScanEvent,
    ScanResult,
    Warehouse,
    Worker,
    utcnow,
)

ERROR_RESULTS = (ScanResult.mismatch, ScanResult.over_pick)
OUTLIER_MIN_SCANS = 30


def tz_of(wh: Warehouse) -> ZoneInfo:
    try:
        return ZoneInfo(wh.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def fmt_local(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(tz).isoformat(timespec="seconds")


def worker_names(db: Session, warehouse_id: uuid.UUID) -> dict[uuid.UUID, str]:
    return {
        wid: name for wid, name in db.execute(select(Worker.id, Worker.name).where(Worker.warehouse_id == warehouse_id))
    }


def day_bounds(wh: Warehouse, day: date | None = None) -> tuple[datetime, datetime, date]:
    tz = tz_of(wh)
    day = day or utcnow().astimezone(tz).date()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end, day


def _result_counts(db: Session, wh_id: uuid.UUID, since: datetime | None, until: datetime | None) -> dict[str, int]:
    stmt = select(ScanEvent.result, func.count()).where(ScanEvent.warehouse_id == wh_id)
    if since:
        stmt = stmt.where(ScanEvent.client_scanned_at >= since)
    if until:
        stmt = stmt.where(ScanEvent.client_scanned_at < until)
    counts = {r.value: 0 for r in ScanResult}
    for result, n in db.execute(stmt.group_by(ScanEvent.result)):
        counts[result.value] = n
    return counts


def _accuracy(c: dict[str, int]) -> float | None:
    attempts = c["match"] + c["mismatch"] + c["over_pick"]
    return round(c["match"] / attempts, 4) if attempts else None


def summary(db: Session, wh: Warehouse, day: date | None = None) -> dict[str, Any]:
    start, end, day = day_bounds(wh, day)
    today = _result_counts(db, wh.id, start, end)
    all_time = _result_counts(db, wh.id, None, None)

    status_counts = dict.fromkeys((s.value for s in OrderStatus), 0)
    for st, n in db.execute(
        select(Order.status, func.count()).where(Order.warehouse_id == wh.id).group_by(Order.status)
    ):
        status_counts[st.value] = n
    completed_today = (
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
    shipped_today = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.warehouse_id == wh.id, Order.shipped_at >= start, Order.shipped_at < end)
        )
        or 0
    )
    open_flags = (
        db.scalar(
            select(func.count())
            .select_from(OrderFlag)
            .join(Order, Order.id == OrderFlag.order_id)
            .where(
                OrderFlag.warehouse_id == wh.id,
                OrderFlag.resolved_at.is_(None),
                Order.status != OrderStatus.cancelled,
            )
        )
        or 0
    )
    active_workers = (
        db.scalar(
            select(func.count(func.distinct(ScanEvent.worker_id))).where(
                ScanEvent.warehouse_id == wh.id, ScanEvent.client_scanned_at >= start, ScanEvent.client_scanned_at < end
            )
        )
        or 0
    )

    return {
        "date": day.isoformat(),
        "timezone": wh.timezone,
        "orders": {
            "pending": status_counts["pending"],
            "in_progress": status_counts["in_progress"],
            "flagged": status_counts["flagged"],
            "completed_today": completed_today,
            "completed_all_time": status_counts["completed"] + status_counts["shipped"],
            "ready_to_ship": status_counts["completed"],
            "shipped_today": shipped_today,
            "open_problems": open_flags,
        },
        "today": {
            "scans": sum(v for k, v in today.items() if k != "void"),
            "units_picked": today["match"] - today["void"],
            "mismatches": today["mismatch"],
            "over_picks": today["over_pick"],
            "reviews": today["review"],
            "errors_caught": today["mismatch"] + today["over_pick"],
            "accuracy": _accuracy(today),
            "active_workers": active_workers,
            "money_saved_cents": (today["mismatch"] + today["over_pick"]) * wh.cost_per_error_cents,
        },
        "cost_per_error_cents": wh.cost_per_error_cents,
        "all_time": {
            "money_saved_cents": (all_time["mismatch"] + all_time["over_pick"]) * wh.cost_per_error_cents,
            "errors_caught": all_time["mismatch"] + all_time["over_pick"],
            "units_verified": all_time["match"] - all_time["void"],
            "accuracy": _accuracy(all_time),
        },
    }


def live(db: Session, wh: Warehouse) -> dict[str, Any]:
    """The floor right now: open orders with progress, and recent problems."""
    now = utcnow()
    recent_cutoff = now - timedelta(hours=2)
    orders = list(
        db.scalars(
            select(Order)
            .where(
                Order.warehouse_id == wh.id,
                (Order.status.in_([OrderStatus.in_progress, OrderStatus.flagged]))
                | and_(
                    Order.status.in_([OrderStatus.completed, OrderStatus.shipped]),
                    Order.completed_at >= recent_cutoff,
                ),
            )
            .order_by(Order.updated_at.desc())
            .limit(100)
        )
    )
    rows = order_rows(db, wh, orders)
    problems = recent_problems(db, wh, limit=25)
    return {"orders": rows, "problems": problems, "flags": open_flags(db, wh), "generated_at": now.isoformat()}


def open_flags(db: Session, wh: Warehouse, limit: int = 50) -> list[dict[str, Any]]:
    """Unresolved worker reports: the queue a manager or supervisor works."""
    rows = list(
        db.execute(
            select(OrderFlag, Order.external_order_number, OrderLineItem)
            .join(Order, Order.id == OrderFlag.order_id)
            .outerjoin(OrderLineItem, OrderLineItem.id == OrderFlag.line_item_id)
            .where(
                OrderFlag.warehouse_id == wh.id,
                OrderFlag.resolved_at.is_(None),
                Order.status != OrderStatus.cancelled,
            )
            .order_by(OrderFlag.created_at.desc())
            .limit(limit)
        )
    )
    photos: dict[uuid.UUID, list[str]] = {}
    if rows:
        for pid, fid in db.execute(
            select(Photo.id, Photo.flag_id).where(
                Photo.warehouse_id == wh.id, Photo.flag_id.in_([f.id for f, _, _ in rows])
            )
        ):
            photos.setdefault(fid, []).append(str(pid))
    names = worker_names(db, wh.id)
    return [
        {
            "id": str(f.id),
            "order_id": str(f.order_id),
            "order_number": number,
            "reason": f.reason.value,
            "note": f.note,
            "short_quantity": f.short_quantity,
            "short_reason": f.short_reason.value if f.short_reason else None,
            "expected_quantity": line.expected_quantity if line else None,
            "sku": line.sku if line else None,
            "description": line.sku_description if line else None,
            "location": line.location if line else None,
            "worker": names.get(f.worker_id) if f.worker_id else None,
            "created_at": f.created_at.isoformat(),
            "photos": photos.get(f.id, []),
        }
        for f, number, line in rows
    ]


def order_rows(db: Session, wh: Warehouse, orders: list[Order]) -> list[dict[str, Any]]:
    """List-view rows for orders, with progress and error counts, in 3 queries."""
    if not orders:
        return []
    ids = [o.id for o in orders]
    progress = {
        oid: (n, exp, got)
        for oid, n, exp, got in db.execute(
            select(
                OrderLineItem.order_id,
                func.count(),
                func.sum(OrderLineItem.expected_quantity),
                func.sum(func.least(OrderLineItem.scanned_quantity, OrderLineItem.expected_quantity)),
            )
            .where(OrderLineItem.order_id.in_(ids))
            .group_by(OrderLineItem.order_id)
        )
    }
    scan_stats = {
        oid: (errs, last, workers)
        for oid, errs, last, workers in db.execute(
            select(
                ScanEvent.order_id,
                func.sum(case((ScanEvent.result.in_(ERROR_RESULTS), 1), else_=0)),
                func.max(ScanEvent.client_scanned_at),
                func.count(func.distinct(ScanEvent.worker_id)),
            )
            .where(ScanEvent.order_id.in_(ids), ScanEvent.warehouse_id == wh.id)
            .group_by(ScanEvent.order_id)
        )
    }
    names = worker_names(db, wh.id)
    out = []
    for o in orders:
        n, exp, got = progress.get(o.id, (0, 0, 0))
        errs, last, workers = scan_stats.get(o.id, (0, None, 0))
        out.append(
            {
                "id": str(o.id),
                "external_order_number": o.external_order_number,
                "customer": o.customer,
                "status": o.status.value,
                "source": o.source.value,
                "tracking_number": o.tracking_number,
                "line_count": n or 0,
                "units_expected": int(exp or 0),
                "units_scanned": int(got or 0),
                "errors_caught": int(errs or 0),
                "workers": int(workers or 0),
                "assigned_worker": names.get(o.assigned_worker_id) if o.assigned_worker_id else None,
                "assigned_worker_id": str(o.assigned_worker_id) if o.assigned_worker_id else None,
                "last_scan_at": last.isoformat() if last else None,
                "created_at": o.created_at.isoformat(),
                "completed_at": o.completed_at.isoformat() if o.completed_at else None,
            }
        )
    return out


def recent_problems(
    db: Session, wh: Warehouse, limit: int = 25, order_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    intended = OrderLineItem
    stmt = (
        select(
            ScanEvent,
            Worker.name,
            Order.external_order_number,
            intended.sku,
            intended.sku_description,
            intended.expected_barcode,
        )
        .join(Worker, Worker.id == ScanEvent.worker_id)
        .join(Order, Order.id == ScanEvent.order_id)
        .outerjoin(intended, intended.id == ScanEvent.intended_line_item_id)
        .where(
            ScanEvent.warehouse_id == wh.id,
            ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick, ScanResult.review]),
        )
        .order_by(ScanEvent.client_scanned_at.desc())
        .limit(limit)
    )
    if order_id:
        stmt = stmt.where(ScanEvent.order_id == order_id)
    return [
        {
            "scan_id": str(se.id),
            "result": se.result.value,
            "order_id": str(se.order_id),
            "order_number": number,
            "worker": wname,
            "scanned_barcode": se.scanned_barcode,
            "intended_sku": sku,
            "intended_description": desc,
            "intended_barcode": ibar,
            "at": se.client_scanned_at.isoformat(),
            "was_offline": se.was_offline,
        }
        for se, wname, number, sku, desc, ibar in db.execute(stmt)
    ]


def worker_stats(db: Session, wh: Warehouse, days: int = 7) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    agg = {
        wid: dict(scans=scans, matches=m, mismatches=mm, over_picks=op, reviews=rv, voids=vd, orders=orders, last=last)
        for wid, scans, m, mm, op, rv, vd, orders, last in db.execute(
            select(
                ScanEvent.worker_id,
                func.sum(case((ScanEvent.result != ScanResult.void, 1), else_=0)),
                func.sum(case((ScanEvent.result == ScanResult.match, 1), else_=0)),
                func.sum(case((ScanEvent.result == ScanResult.mismatch, 1), else_=0)),
                func.sum(case((ScanEvent.result == ScanResult.over_pick, 1), else_=0)),
                func.sum(case((ScanEvent.result == ScanResult.review, 1), else_=0)),
                func.sum(case((ScanEvent.result == ScanResult.void, 1), else_=0)),
                func.count(func.distinct(ScanEvent.order_id)),
                func.max(ScanEvent.client_scanned_at),
            )
            .where(ScanEvent.warehouse_id == wh.id, ScanEvent.client_scanned_at >= since)
            .group_by(ScanEvent.worker_id)
        )
    }
    flags: dict[uuid.UUID | None, int] = {
        wid: n
        for wid, n in db.execute(
            select(OrderFlag.worker_id, func.count())
            .where(OrderFlag.warehouse_id == wh.id, OrderFlag.created_at >= since)
            .group_by(OrderFlag.worker_id)
        )
    }
    total_scans = sum(int(a["scans"] or 0) for a in agg.values())
    total_errors = sum(int(a["mismatches"] or 0) + int(a["over_picks"] or 0) for a in agg.values())
    wh_rate = total_errors / total_scans if total_scans else 0.0

    rows = []
    for w in db.scalars(select(Worker).where(Worker.warehouse_id == wh.id).order_by(Worker.name)):
        a = agg.get(w.id)
        if not a and not w.active:
            continue
        scans = int(a["scans"] or 0) if a else 0
        errors = (int(a["mismatches"] or 0) + int(a["over_picks"] or 0)) if a else 0
        rate = errors / scans if scans else None
        # Worth a look: enough volume to mean something, and clearly worse
        # than the warehouse as a whole (double, and at least 3 points).
        outlier = bool(rate is not None and scans >= OUTLIER_MIN_SCANS and rate >= max(2 * wh_rate, wh_rate + 0.03))
        rows.append(
            {
                "worker_id": str(w.id),
                "name": w.name,
                "active": w.active,
                "scans": scans,
                "units_picked": (int(a["matches"] or 0) - int(a["voids"] or 0)) if a else 0,
                "mismatches": int(a["mismatches"] or 0) if a else 0,
                "over_picks": int(a["over_picks"] or 0) if a else 0,
                "reviews": int(a["reviews"] or 0) if a else 0,
                "undos": int(a["voids"] or 0) if a else 0,
                "orders": int(a["orders"] or 0) if a else 0,
                "flags": int(flags.get(w.id, 0)),
                "error_rate": round(rate, 4) if rate is not None else None,
                "needs_attention": outlier,
                "last_active": a["last"].isoformat() if a and a["last"] else None,
            }
        )
    return {"days": days, "warehouse_error_rate": round(wh_rate, 4), "workers": rows}


def sku_insights(db: Session, wh: Warehouse, days: int = 30, limit: int = 10) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    line = OrderLineItem
    mispicked = [
        {"barcode": bc, "sku": sku, "description": desc, "errors": int(n)}
        for bc, sku, desc, n in db.execute(
            select(line.normalized_barcode, func.max(line.sku), func.max(line.sku_description), func.count())
            .join(ScanEvent, ScanEvent.intended_line_item_id == line.id)
            .where(
                ScanEvent.warehouse_id == wh.id,
                ScanEvent.result == ScanResult.mismatch,
                ScanEvent.client_scanned_at >= since,
            )
            .group_by(line.normalized_barcode)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ]
    wrong_grabs = [
        {"barcode": bc, "times": int(n), "orders": int(orders)}
        for bc, n, orders in db.execute(
            select(ScanEvent.normalized_barcode, func.count(), func.count(func.distinct(ScanEvent.order_id)))
            .where(
                ScanEvent.warehouse_id == wh.id,
                ScanEvent.result == ScanResult.mismatch,
                ScanEvent.client_scanned_at >= since,
            )
            .group_by(ScanEvent.normalized_barcode)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ]
    return {"days": days, "most_mispicked": mispicked, "most_grabbed_wrong": wrong_grabs}


def trend(db: Session, wh: Warehouse, days: int = 30) -> dict[str, Any]:
    tz = tz_of(wh)
    start_day = utcnow().astimezone(tz).date() - timedelta(days=days - 1)
    start, _, _ = day_bounds(wh, start_day)
    local_day = cast(func.timezone(wh.timezone if tz.key == wh.timezone else "UTC", ScanEvent.client_scanned_at), Date)
    buckets: dict[date, dict[str, int]] = {}
    for d, result, n in db.execute(
        select(local_day, ScanEvent.result, func.count())
        .where(ScanEvent.warehouse_id == wh.id, ScanEvent.client_scanned_at >= start)
        .group_by(local_day, ScanEvent.result)
    ):
        buckets.setdefault(d, {})[result.value] = n
    series = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        b = buckets.get(d, {})
        series.append(
            {
                "date": d.isoformat(),
                "units_picked": b.get("match", 0) - b.get("void", 0),
                "errors_caught": b.get("mismatch", 0) + b.get("over_pick", 0),
                "reviews": b.get("review", 0),
                "money_saved_cents": (b.get("mismatch", 0) + b.get("over_pick", 0)) * wh.cost_per_error_cents,
            }
        )
    return {"days": days, "series": series}
