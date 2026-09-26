"""Feature usage counters: which parts of the product each warehouse uses.

One row per warehouse, feature and (UTC) day, incremented in the caller's
transaction. Counts only, never content: this is for the operator to see
whether pilots use pick sheets, short picks or reports, not what they ship.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import FeatureUsage, utcnow

# Feature names shown to the operator, with a human label.
FEATURES: dict[str, str] = {
    "floor.scan": "Scans",
    "floor.void": "Undos",
    "floor.flag": "Problems flagged",
    "floor.short": "Short picks",
    "floor.ship": "Shipping labels scanned",
    "floor.photo": "Photos taken",
    "orders.import": "CSV imports",
    "orders.manual": "Orders typed in",
    "orders.sample": "Sample orders loaded",
    "orders.pick_sheets": "Pick sheets printed",
    "orders.proof": "Shipment proofs opened",
    "flags.resolve": "Problems resolved",
    "aliases.create": "Barcodes taught",
    "reports.view": "Reports viewed",
    "exports.csv": "CSV exports",
    "board.view": "Floor board opened",
    "team.invite": "Team invites",
    "settings.update": "Settings changed",
    "warehouse.add": "Warehouses added",
}


def track(db: Session, warehouse_id: uuid.UUID, feature: str, n: int = 1) -> None:
    if n <= 0:
        return
    stmt = insert(FeatureUsage).values(warehouse_id=warehouse_id, feature=feature[:48], day=utcnow().date(), count=n)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[FeatureUsage.warehouse_id, FeatureUsage.feature, FeatureUsage.day],
            set_={"count": FeatureUsage.count + stmt.excluded.count},
        )
    )


def totals(db: Session, days: int = 30, warehouse_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    since = utcnow().date() - timedelta(days=days - 1)
    stmt = (
        select(
            FeatureUsage.feature,
            func.sum(FeatureUsage.count),
            func.count(func.distinct(FeatureUsage.warehouse_id)),
            func.max(FeatureUsage.day),
        )
        .where(FeatureUsage.day >= since)
        .group_by(FeatureUsage.feature)
    )
    if warehouse_id:
        stmt = stmt.where(FeatureUsage.warehouse_id == warehouse_id)
    found = {f: (int(n), int(w), d) for f, n, w, d in db.execute(stmt)}
    out = []
    for key in list(FEATURES) + sorted(set(found) - set(FEATURES)):
        n, w, last = found.get(key, (0, 0, None))
        out.append(
            {
                "feature": key,
                "label": FEATURES.get(key, key),
                "count": n,
                "warehouses": w,
                "last_used": last.isoformat() if isinstance(last, date) else None,
            }
        )
    return out
