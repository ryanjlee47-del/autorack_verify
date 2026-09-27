"""Orders that arrive on their own, and tracking that goes back on its own.

* Store connections (Shopify, ShipStation, WooCommerce): every few minutes
  the job runner pulls orders waiting to ship and creates the ones Autorack
  hasn't seen (by the store's own order id, so an order cancelled here never
  comes back). When the phone scans the shipping label, the tracking number
  is queued and pushed to the store, which emails the customer.
* Spreadsheet links: a Google Sheet or CSV URL, re-read on a schedule through
  the normal CSV importer.
* Import address: each warehouse gets a secret email address and upload URL.
  A CSV emailed there, or posted by the watched-folder script, is imported.

A connection that keeps failing emails the owners once, and shows the reason
on the Connections page.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError
from ..models import (
    ImportBatch,
    Integration,
    IntegrationKind,
    Order,
    OrderSource,
    Warehouse,
    utcnow,
)
from . import access, audit, csv_import, secretbox, stores
from . import orders as order_svc
from .audit import Actor

log = logging.getLogger("autorack.integrations")

MAX_PER_RUN = 10
MAX_PUSH_PER_RUN = 50
MAX_PUSH_ATTEMPTS = 6
FAIL_EMAIL_AFTER = 3
MAX_PER_WAREHOUSE = 10

LABELS = {
    IntegrationKind.shopify: "Shopify",
    IntegrationKind.shipstation: "ShipStation",
    IntegrationKind.woocommerce: "WooCommerce",
    IntegrationKind.sheet: "Spreadsheet link",
}
SOURCE = {
    IntegrationKind.shopify: OrderSource.shopify,
    IntegrationKind.shipstation: OrderSource.shipstation,
    IntegrationKind.woocommerce: OrderSource.woocommerce,
    IntegrationKind.sheet: OrderSource.sheet,
}


def actor_for(integ: Integration) -> Actor:
    return Actor("system", str(integ.id), f"{LABELS[integ.kind]} sync")


def connector_for(integ: Integration) -> stores.Connector:
    try:
        secret = secretbox.unseal(integ.secret)
    except secretbox.SecretUnreadable as exc:
        raise stores.StoreError(
            "The saved credentials can't be read any more (the server key changed). Reconnect.", retry=False
        ) from exc
    return stores.connector(integ.kind, integ.config or {}, secret)


def integration_dict(integ: Integration, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    cfg = integ.config or {}
    if not integ.enabled:
        state = "paused"
    elif integ.failures:
        state = "failing"
    elif integ.last_success_at:
        state = "ok"
    else:
        state = "waiting"
    return {
        "id": str(integ.id),
        "kind": integ.kind.value,
        "label": LABELS[integ.kind],
        "name": integ.name,
        "shop": cfg.get("shop"),
        "store_url": cfg.get("store_url"),
        "url": cfg.get("url"),
        "enabled": integ.enabled,
        "push_tracking": integ.push_tracking and integ.kind != IntegrationKind.sheet,
        "can_push_tracking": integ.kind != IntegrationKind.sheet,
        "sync_minutes": integ.sync_minutes,
        "state": state,
        "last_sync_at": integ.last_sync_at.isoformat() if integ.last_sync_at else None,
        "last_success_at": integ.last_success_at.isoformat() if integ.last_success_at else None,
        "next_sync_at": next_due(integ).isoformat() if integ.enabled else None,
        "last_error": integ.last_error,
        "failures": integ.failures,
        "last_created": integ.last_created,
        "total_created": integ.total_created,
        "created_at": integ.created_at.isoformat(),
    }


def active_for(db: Session, warehouse_id: uuid.UUID) -> list[Integration]:
    return list(
        db.scalars(
            select(Integration)
            .where(Integration.warehouse_id == warehouse_id, Integration.deleted_at.is_(None))
            .order_by(Integration.created_at)
        )
    )


def get(db: Session, warehouse_id: uuid.UUID, integration_id: uuid.UUID) -> Integration:
    integ = db.scalar(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.warehouse_id == warehouse_id,
            Integration.deleted_at.is_(None),
        )
    )
    if not integ:
        from ..errors import not_found

        raise not_found("Connection not found")
    return integ


# ---------------------------------------------------------------------------
# Connect / update / disconnect
# ---------------------------------------------------------------------------


def split_fields(kind: IntegrationKind, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(config, secret) for this kind, from the form the owner filled in."""
    f = {k: (str(v).strip() if v is not None else "") for k, v in fields.items()}
    if kind == IntegrationKind.shopify:
        return {"shop": stores.shopify_domain(f.get("shop", ""))}, {"token": f.get("token", "")}
    if kind == IntegrationKind.shipstation:
        return {}, {"api_key": f.get("api_key", ""), "api_secret": f.get("api_secret", "")}
    if kind == IntegrationKind.woocommerce:
        return {"store_url": stores.woo_url(f.get("store_url", ""))}, {
            "consumer_key": f.get("consumer_key", ""),
            "consumer_secret": f.get("consumer_secret", ""),
        }
    return {"url": f.get("url", "")}, {}


def connect(
    db: Session,
    wh: Warehouse,
    kind: IntegrationKind,
    fields: dict[str, Any],
    *,
    push_tracking: bool,
    actor: Actor,
    user_id: uuid.UUID | None,
) -> tuple[Integration, str]:
    if len(active_for(db, wh.id)) >= MAX_PER_WAREHOUSE:
        raise ApiError(400, "too_many_connections", f"A warehouse can have up to {MAX_PER_WAREHOUSE} connections.")
    try:
        config, secret = split_fields(kind, fields)
        found = stores.connector(kind, config, secret).check()
    except stores.StoreError as exc:
        raise ApiError(400, "connection_failed", exc.message) from exc
    integ = Integration(
        warehouse_id=wh.id,
        kind=kind,
        name=found[:200],
        config=config,
        secret=secretbox.seal(secret) if secret else None,
        cursor={},
        enabled=True,
        push_tracking=push_tracking and kind != IntegrationKind.sheet,
        sync_minutes=15 if kind == IntegrationKind.sheet else 10,
        failures=0,
        last_created=0,
        total_created=0,
        created_by_user_id=user_id,
    )
    db.add(integ)
    db.flush()
    audit.record(
        db,
        actor,
        "integration.connected",
        warehouse_id=wh.id,
        target_type="integration",
        target_id=integ.id,
        kind=kind.value,
        name=integ.name,
    )
    return integ, found


def update_credentials(db: Session, integ: Integration, fields: dict[str, Any], actor: Actor) -> None:
    try:
        config, secret = split_fields(integ.kind, {**(integ.config or {}), **fields})
        stores.connector(integ.kind, config, secret).check()
    except stores.StoreError as exc:
        raise ApiError(400, "connection_failed", exc.message) from exc
    integ.config = config
    integ.secret = secretbox.seal(secret) if secret else None
    integ.failures = 0
    integ.last_error = None
    audit.record(
        db,
        actor,
        "integration.reconnected",
        warehouse_id=integ.warehouse_id,
        target_type="integration",
        target_id=integ.id,
    )


def disconnect(db: Session, integ: Integration, actor: Actor) -> None:
    integ.deleted_at = utcnow()
    integ.enabled = False
    integ.secret = None  # the credentials go now; the orders it made stay
    # Nothing left to push with.
    for o in db.scalars(
        select(Order).where(Order.integration_id == integ.id, Order.tracking_push_status.in_(("pending", "failed")))
    ):
        o.tracking_push_status = "skipped"
        o.tracking_push_error = "Store disconnected"
    audit.record(
        db,
        actor,
        "integration.disconnected",
        warehouse_id=integ.warehouse_id,
        target_type="integration",
        target_id=integ.id,
        kind=integ.kind.value,
        name=integ.name,
    )


# ---------------------------------------------------------------------------
# Pulling orders
# ---------------------------------------------------------------------------


def next_due(integ: Integration) -> datetime:
    if not integ.last_sync_at:
        return integ.created_at
    # Back off while a store keeps failing: 10 min, 20, 40... capped at 2h.
    factor = min(2**integ.failures, 12) if integ.failures else 1
    return integ.last_sync_at + timedelta(minutes=integ.sync_minutes * factor)


def sync_one(db: Session, integ: Integration, now: datetime | None = None) -> dict[str, Any]:
    """Pull once. Commits. Returns what happened; failures are recorded on
    the integration, never raised."""
    now = now or utcnow()
    wh = db.get(Warehouse, integ.warehouse_id)
    assert wh is not None
    integ.last_sync_at = now
    if not access.evaluate(wh, now).allowed:
        integ.last_error = "Paused: this warehouse's subscription isn't active."
        db.commit()
        return {"ok": False, "error": integ.last_error}
    try:
        conn = connector_for(integ)
        if isinstance(conn, stores.SheetLink):
            result = _import_sheet(db, wh, integ, conn)
        else:
            result = _import_store_orders(db, wh, integ, conn.fetch(stores.since(now)))
    except stores.StoreError as exc:
        db.rollback()
        integ = db.get(Integration, integ.id) or integ
        integ.last_sync_at = now
        integ.failures += 1
        integ.last_error = exc.message[:1000]
        db.commit()
        _maybe_email_failure(db, integ)
        return {"ok": False, "error": exc.message}
    except Exception as exc:
        db.rollback()
        from . import monitoring

        monitoring.record_exception(exc, source="job", context={"job": "store_sync", "kind": integ.kind.value})
        integ = db.get(Integration, integ.id) or integ
        integ.last_sync_at = now
        integ.failures += 1
        integ.last_error = "Something went wrong on our side while syncing. We've been notified."
        db.commit()
        return {"ok": False, "error": integ.last_error}
    integ.failures = 0
    integ.last_error = None
    integ.last_success_at = now
    integ.last_created = result["created"]
    integ.total_created += result["created"]
    db.commit()
    return {"ok": True, **result}


def _import_store_orders(
    db: Session, wh: Warehouse, integ: Integration, found: list[stores.StoreOrder]
) -> dict[str, Any]:
    ids = [o.store_order_id for o in found]
    seen: set[str] = set()
    for i in range(0, len(ids), 1000):
        seen.update(
            s
            for s in db.scalars(
                select(Order.store_order_id).where(
                    Order.warehouse_id == wh.id,
                    Order.integration_id == integ.id,
                    Order.store_order_id.in_(ids[i : i + 1000]),
                )
            )
            if s
        )
    fresh = [o for o in found if o.store_order_id not in seen]
    taken = csv_import.existing_numbers(db, wh.id, [o.number for o in fresh])
    warnings: list[str] = []
    created = lines = 0
    batch: ImportBatch | None = None
    for so in fresh:
        if so.number in taken:
            warnings.append(f"Order {so.number} already exists in Autorack (added by hand or CSV); skipped.")
            continue
        if not so.lines:
            if so.skipped_lines:
                warnings.append(f"Order {so.number}: no item has a barcode or SKU; skipped.")
            continue
        if len(so.lines) > order_svc.MAX_LINES_PER_ORDER:
            warnings.append(f"Order {so.number} has more than {order_svc.MAX_LINES_PER_ORDER} lines; skipped.")
            continue
        if so.skipped_lines:
            warnings.append(
                f"Order {so.number}: {len(so.skipped_lines)} item(s) without a barcode or SKU left off "
                f"({', '.join(so.skipped_lines[:3])})."
            )
        if batch is None:
            batch = ImportBatch(warehouse_id=wh.id, filename=f"{LABELS[integ.kind]}: {integ.name}"[:255], warnings=[])
            db.add(batch)
            db.flush()
        order = order_svc.create_order(
            db,
            wh,
            external_order_number=so.number,
            lines=so.lines,
            customer=so.customer,
            source=SOURCE[integ.kind],
            import_batch_id=batch.id,
        )
        order.integration_id = integ.id
        order.store_order_id = so.store_order_id[:100]
        created += 1
        lines += len(so.lines)
    if batch is not None:
        batch.orders_created = created
        batch.lines_created = lines
        batch.warnings = warnings[:100]
        audit.record(
            db,
            actor_for(integ),
            "orders.imported",
            warehouse_id=wh.id,
            target_type="import_batch",
            target_id=batch.id,
            filename=batch.filename,
            orders=created,
            lines=lines,
            skipped=len(found) - created,
        )
    return {"found": len(found), "created": created, "warnings": warnings[:20]}


def _import_sheet(db: Session, wh: Warehouse, integ: Integration, conn: stores.SheetLink) -> dict[str, Any]:
    content = conn.download()
    try:
        batch = csv_import.commit(
            db,
            wh,
            content,
            f"Sheet: {integ.name}",
            actor_for(integ),
            None,
            skip_invalid_rows=True,
            source=OrderSource.sheet,
            automatic=True,
        )
    except ApiError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise stores.StoreError(f"The spreadsheet couldn't be read: {detail.get('message', exc.code)}") from exc
    for o in db.scalars(select(Order).where(Order.import_batch_id == batch.id)):
        o.integration_id = integ.id
    if not batch.orders_created:
        # Nothing new: don't leave an empty "import" in the history every 15 min.
        db.delete(batch)
    return {"created": batch.orders_created, "warnings": list(batch.warnings or [])[:20]}


def _maybe_email_failure(db: Session, integ: Integration) -> None:
    if integ.failures != FAIL_EMAIL_AFTER:
        return
    from . import email, jobs

    wh = db.get(Warehouse, integ.warehouse_id)
    if not wh:
        return
    key = f"{integ.id}:{(integ.last_success_at or integ.created_at).isoformat()}"

    def build(to: str) -> Any:
        return email.notice_email(
            to,
            subject=f"{LABELS[integ.kind]} orders aren't reaching Autorack",
            heading=f"We can't pull orders from {integ.name}",
            lines=[
                f"The last {FAIL_EMAIL_AFTER} tries failed. What the store said:",
                integ.last_error or "(no detail)",
                "New orders won't show up on the phones until this is fixed. Autorack keeps retrying.",
            ],
            button_label="Open Connections",
            url=jobs.app_url("#/connections"),
            footer=f"Sent to the owners of {wh.name}. Sent once per outage.",
        )

    jobs._send_all(db, wh, "integration_failing", key, jobs.recipients(db, wh, want="owners"), build)


def run_store_sync(db: Session, now: datetime) -> int:
    candidates = db.scalars(
        select(Integration)
        .where(Integration.enabled.is_(True), Integration.deleted_at.is_(None))
        .order_by(Integration.last_sync_at.asc().nulls_first())
        .limit(200)
    ).all()
    due = [i for i in candidates if next_due(i) <= now][:MAX_PER_RUN]
    for integ in due:
        result = sync_one(db, integ, now)
        if result.get("ok") and products_due(integ, now):
            sync_products(db, integ, now)
    return len(due)


# ---------------------------------------------------------------------------
# Pulling the catalog (products, barcodes, pictures)
# ---------------------------------------------------------------------------

PRODUCTS_EVERY = timedelta(hours=24)
IMAGES_PER_RUN = 60


def products_due(integ: Integration, now: datetime) -> bool:
    if integ.kind == IntegrationKind.sheet:
        return False
    at = (integ.cursor or {}).get("products_at")
    return not at or datetime.fromisoformat(at) <= now - PRODUCTS_EVERY


def sync_products(db: Session, integ: Integration, now: datetime | None = None) -> dict[str, Any]:
    """Create or update catalog products from the store, and fetch pictures
    for products that have none. Never raises for one bad product."""
    from ..models import Product
    from . import catalog

    now = now or utcnow()
    wh = db.get(Warehouse, integ.warehouse_id)
    assert wh is not None
    try:
        found = connector_for(integ).products()  # type: ignore[union-attr]
    except stores.StoreError as exc:
        return {"ok": False, "error": exc.message}
    source = integ.kind.value
    created = updated = images = 0
    warnings: list[str] = []
    changed: list[Product] = []
    for sp in found[:10_000]:
        if not (sp.sku or sp.barcode):
            continue
        p = db.scalar(
            select(Product).where(
                Product.warehouse_id == wh.id, Product.source == source, Product.external_id == sp.external_id[:100]
            )
        )
        if p is None and sp.sku:
            p = db.scalar(
                select(Product).where(
                    Product.warehouse_id == wh.id,
                    Product.active.is_(True),
                    func.upper(Product.sku) == sp.sku.upper(),
                )
            )
        if p is None and sp.barcode:
            p = db.scalar(
                select(Product).where(
                    Product.warehouse_id == wh.id,
                    Product.active.is_(True),
                    Product.normalized_barcode == catalog.key(sp.barcode),
                )
            )
            if p is not None and sp.sku and p.sku and p.sku.upper() != sp.sku.upper():
                p = None  # same barcode, different SKU: a separate product (the barcode clash is reported)
        is_new = p is None
        try:
            with db.begin_nested():
                if p is None:
                    p = Product(warehouse_id=wh.id, name=sp.name, source=source, external_id=sp.external_id[:100])
                    db.add(p)
                    db.flush()
                before = (p.sku, p.normalized_barcode)
                p.name = sp.name or p.name
                if p.source == source:
                    p.external_id = sp.external_id[:100]
                if sp.sku and not p.sku:
                    p.sku = sp.sku
                if sp.barcode and catalog.key(sp.barcode) != p.normalized_barcode:
                    try:
                        p.normalized_barcode = catalog.check_barcode_free(db, wh.id, sp.barcode, product_id=p.id)
                        p.barcode = sp.barcode
                    except ApiError:
                        warnings.append(f"{sp.name}: barcode {sp.barcode} is already on another product.")
                if sp.weight_grams is not None:
                    p.weight_grams = sp.weight_grams
                if sp.location and not p.location:
                    p.location = sp.location
                db.flush()
        except Exception as exc:
            warnings.append(f"{sp.name}: {exc.__class__.__name__}")
            continue
        created += is_new
        updated += not is_new
        if is_new or before != (p.sku, p.normalized_barcode):
            changed.append(p)
        if not p.image_id and sp.image_url and images < IMAGES_PER_RUN:
            try:
                content = stores.fetch_public(sp.image_url, "The store's picture server").content
                with db.begin_nested():
                    catalog.set_image(db, p, content)
                images += 1
                if p not in changed:
                    changed.append(p)
            except (stores.StoreError, ApiError):
                pass
    for p in changed:
        catalog.relink(db, wh, p)
    integ.cursor = {**(integ.cursor or {}), "products_at": now.isoformat(), "products": len(found)}
    db.commit()
    return {
        "ok": True,
        "found": len(found),
        "created": created,
        "updated": updated,
        "images": images,
        "warnings": warnings[:20],
    }


# ---------------------------------------------------------------------------
# Pushing tracking back
# ---------------------------------------------------------------------------


def queue_tracking(order: Order) -> None:
    """Called when the label is scanned. The push happens in the job runner,
    so a slow store never holds up the phone."""
    if order.integration_id and order.store_order_id:
        order.tracking_push_status = "pending"
        order.tracking_push_attempts = 0
        order.tracking_push_error = None


def _retry_at(order: Order) -> datetime:
    last = order.tracking_pushed_at or order.shipped_at or utcnow()
    return last + timedelta(minutes=5 * 2 ** max(0, order.tracking_push_attempts - 1))


def push_one(db: Session, order: Order, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    integ = db.get(Integration, order.integration_id) if order.integration_id else None
    if not integ or integ.deleted_at or not order.tracking_number:
        order.tracking_push_status = "skipped"
        order.tracking_push_error = "Store disconnected" if order.tracking_number else "No tracking number"
        db.commit()
        return {"ok": False, "error": order.tracking_push_error}
    if not integ.push_tracking:
        order.tracking_push_status = "skipped"
        order.tracking_push_error = "Sending tracking to the store is turned off"
        db.commit()
        return {"ok": False, "error": order.tracking_push_error}
    order.tracking_push_attempts += 1
    order.tracking_pushed_at = now
    try:
        result = connector_for(integ).push(order)
    except stores.StoreError as exc:
        order.tracking_push_status = "failed"
        order.tracking_push_error = exc.message[:500]
        if not exc.retry:
            order.tracking_push_attempts = MAX_PUSH_ATTEMPTS
        db.commit()
        return {"ok": False, "error": exc.message}
    except Exception as exc:
        from . import monitoring

        db.rollback()
        monitoring.record_exception(exc, source="job", context={"job": "tracking_push", "kind": integ.kind.value})
        order = db.get(Order, order.id) or order
        order.tracking_push_status = "failed"
        order.tracking_push_error = "Something went wrong on our side. We've been notified."
        order.tracking_pushed_at = now
        db.commit()
        return {"ok": False, "error": order.tracking_push_error}
    order.tracking_push_status = "done"
    order.tracking_push_error = None
    audit.record(
        db,
        actor_for(integ),
        "order.tracking_pushed",
        warehouse_id=order.warehouse_id,
        target_type="order",
        target_id=order.id,
        tracking=order.tracking_number,
        result=result,
    )
    db.commit()
    return {"ok": True, "result": result}


def run_tracking_push(db: Session, now: datetime) -> int:
    rows = db.scalars(
        select(Order)
        .where(
            or_(
                Order.tracking_push_status == "pending",
                (Order.tracking_push_status == "failed") & (Order.tracking_push_attempts < MAX_PUSH_ATTEMPTS),
            )
        )
        .order_by(Order.shipped_at)
        .limit(MAX_PUSH_PER_RUN * 4)
    ).all()
    due = [o for o in rows if o.tracking_push_status == "pending" or _retry_at(o) <= now][:MAX_PUSH_PER_RUN]
    for order in due:
        push_one(db, order, now)
    return len(due)


# ---------------------------------------------------------------------------
# Import address: email and CSV drop URL
# ---------------------------------------------------------------------------


def ensure_import_token(db: Session, wh: Warehouse) -> str:
    if not wh.import_token:
        wh.import_token = secrets.token_hex(16)
        db.flush()
    return wh.import_token


def rotate_import_token(db: Session, wh: Warehouse, actor: Actor) -> str:
    wh.import_token = secrets.token_hex(16)
    audit.record(db, actor, "import_address.rotated", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id)
    db.flush()
    return wh.import_token


def import_address(wh: Warehouse) -> dict[str, Any]:
    s = get_settings()
    token = wh.import_token or ""
    return {
        "email": s.inbound_email_address.replace("{token}", token) if s.inbound_email_enabled and token else None,
        "email_enabled": s.inbound_email_enabled,
        "drop_url": f"{s.api_url}/api/inbound/drop/{token}" if token else None,
    }


def warehouse_for_token(db: Session, token: str) -> Warehouse | None:
    token = (token or "").strip().lower()
    if len(token) != 32 or not all(c in "0123456789abcdef" for c in token):
        return None
    return db.scalar(select(Warehouse).where(Warehouse.import_token == token))


def import_file(
    db: Session, wh: Warehouse, content: bytes, filename: str | None, source: OrderSource, via: str
) -> ImportBatch:
    """A CSV that arrived by email or the drop URL. Invalid rows are skipped
    (nobody is there to fix them); orders already in Autorack are skipped."""
    acc = access.evaluate(wh)
    if not acc.allowed:
        raise ApiError(402, "subscription_inactive", acc.message)
    return csv_import.commit(
        db,
        wh,
        content,
        filename,
        Actor("system", None, via),
        None,
        skip_invalid_rows=True,
        source=source,
        automatic=True,
    )
