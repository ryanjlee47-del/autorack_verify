"""SQLAlchemy ORM models: the system of record.

Tenancy: every row that belongs to a customer carries `warehouse_id`, including
child rows whose parent already has one (line items, scans). The duplication is
deliberate: it lets every query filter on `warehouse_id` directly, which is the
whole tenant-isolation strategy (see services/tenancy notes in the README).

Append-only tables (`scan_events`, `audit_log`) are protected by triggers
created in the initial migration, so history cannot be edited even by a buggy
endpoint or a hand-typed SQL statement.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _enum(cls: type[enum.Enum], name: str) -> SAEnum:
    # VARCHAR + CHECK rather than a native Postgres ENUM: adding a value to a
    # native enum needs special migration handling, a CHECK does not.
    return SAEnum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class SubscriptionStatus(enum.StrEnum):
    pilot = "pilot"  # free pilot, set by the operator
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    unpaid = "unpaid"
    canceled = "canceled"
    incomplete = "incomplete"
    incomplete_expired = "incomplete_expired"
    paused = "paused"


class UserRole(enum.StrEnum):
    owner = "owner"
    manager = "manager"


class OrderStatus(enum.StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    flagged = "flagged"
    cancelled = "cancelled"


class OrderSource(enum.StrEnum):
    manual = "manual"
    csv = "csv"


class ScanResult(enum.StrEnum):
    match = "match"  # right item, counted toward the line
    over_pick = "over_pick"  # right item, but the line already has enough
    mismatch = "mismatch"  # confidently not in this order: an error caught
    review = "review"  # ambiguous or low-confidence: not counted, needs a human
    void = "void"  # a worker undid one of their earlier matches


class FlagReason(enum.StrEnum):
    wrong_item_in_location = "wrong_item_in_location"
    out_of_stock = "out_of_stock"
    damaged = "damaged"
    label_unreadable = "label_unreadable"
    other = "other"


# ---------------------------------------------------------------------------
# Tenants and people
# ---------------------------------------------------------------------------


class Warehouse(Base):
    __tablename__ = "warehouses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    # Billing contact. Login identities live in `users`.
    owner_email: Mapped[str] = mapped_column(String(320))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        _enum(SubscriptionStatus, "subscription_status"), default=SubscriptionStatus.trialing
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), unique=True)

    # Short code a phone uses to link itself to this warehouse.
    join_code: Mapped[str] = mapped_column(String(16), unique=True)

    # Matching engine settings (see matching.py). Tier 6 is opt-in.
    loose_match_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    suffix_len: Mapped[int] = mapped_column(Integer, default=8)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list[User]] = relationship(back_populates="warehouse")
    workers: Mapped[list[Worker]] = relationship(back_populates="warehouse")
    orders: Mapped[list[Order]] = relationship(back_populates="warehouse")

    __table_args__ = (CheckConstraint("suffix_len BETWEEN 6 AND 14", name="ck_warehouses_suffix_len"),)


class User(Base):
    """An owner or manager who signs in to the dashboard by magic link."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lowercased
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), default=UserRole.owner)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    warehouse: Mapped[Warehouse] = relationship(back_populates="users")


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))


class OwnerSession(Base):
    __tablename__ = "owner_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    user: Mapped[User] = relationship()


class Device(Base):
    """A phone linked to a warehouse. Workers sign in on a device with a PIN."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    label: Mapped[str] = mapped_column(String(100), default="Phone")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Salted, slow hash. Verification only.
    pin_hash: Mapped[str] = mapped_column(String(255))
    # Keyed HMAC of (warehouse, PIN). Lets a PIN-only login find its worker
    # without trying every salted hash, and enforces "unique within a
    # warehouse" in the database. Useless without SECRET_KEY.
    pin_fingerprint: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    warehouse: Mapped[Warehouse] = relationship(back_populates="workers")

    __table_args__ = (
        # Only active workers hold a PIN; deactivating someone frees theirs.
        Index(
            "uq_workers_active_pin",
            "warehouse_id",
            "pin_fingerprint",
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class WorkerSession(Base):
    __tablename__ = "worker_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    worker: Mapped[Worker] = relationship()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    filename: Mapped[str | None] = mapped_column(String(255))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    orders_created: Mapped[int] = mapped_column(Integer, default=0)
    lines_created: Mapped[int] = mapped_column(Integer, default=0)
    orders_skipped: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list[str]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    external_order_number: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), default=OrderStatus.pending, index=True
    )
    source: Mapped[OrderSource] = mapped_column(_enum(OrderSource, "order_source"), default=OrderSource.manual)
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_batches.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    assigned_worker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workers.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # Bumped on every change a phone's cached copy would need to know about.
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    warehouse: Mapped[Warehouse] = relationship(back_populates="orders")
    line_items: Mapped[list[OrderLineItem]] = relationship(back_populates="order", order_by="OrderLineItem.line_no")
    flags: Mapped[list[OrderFlag]] = relationship(back_populates="order", order_by="OrderFlag.created_at")
    assigned_worker: Mapped[Worker | None] = relationship()

    __table_args__ = (
        Index(
            "uq_orders_external_number",
            "warehouse_id",
            "external_order_number",
            unique=True,
            postgresql_where=text("external_order_number IS NOT NULL AND status <> 'cancelled'"),
        ),
        Index("ix_orders_warehouse_status_created", "warehouse_id", "status", "created_at"),
    )


class OrderLineItem(Base):
    __tablename__ = "order_line_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    expected_barcode: Mapped[str] = mapped_column(String(200))
    # matching.normalize(expected_barcode).normalized: dedupes lines and
    # anchors learned aliases.
    normalized_barcode: Mapped[str] = mapped_column(String(200))
    expected_quantity: Mapped[int] = mapped_column(Integer, default=1)
    scanned_quantity: Mapped[int] = mapped_column(Integer, default=0)
    sku: Mapped[str | None] = mapped_column(String(100))
    sku_description: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("order_id", "normalized_barcode", name="uq_line_items_order_barcode"),
        CheckConstraint("expected_quantity >= 1", name="ck_line_items_expected_qty"),
        CheckConstraint("scanned_quantity >= 0", name="ck_line_items_scanned_qty"),
    )


class ScanEvent(Base):
    """One scan attempt, matched or not. Append-only (enforced by trigger).

    `id` is generated on the phone, which makes offline sync idempotent: a
    batch re-sent after a dropped response inserts nothing new.
    """

    __tablename__ = "scan_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    # The line this scan counted against (match/over_pick/void), if any.
    line_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("order_line_items.id"), index=True)
    # The line the worker was trying to pick. Makes "which SKUs get mis-picked"
    # answerable, because a mismatch by definition matched no line.
    intended_line_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("order_line_items.id"))
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    worker_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("worker_sessions.id"))
    scanned_barcode: Mapped[str] = mapped_column(String(500))
    normalized_barcode: Mapped[str] = mapped_column(String(500))
    result: Mapped[ScanResult] = mapped_column(_enum(ScanResult, "scan_result"))
    is_match: Mapped[bool] = mapped_column(Boolean)
    match_tier: Mapped[int | None] = mapped_column(Integer)
    # What the phone told the worker at the time (it decides offline). Kept
    # alongside the server's authoritative result so disagreements are visible.
    client_result: Mapped[str | None] = mapped_column(String(32))
    voids_scan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_events.id"), unique=True)
    was_offline: Mapped[bool] = mapped_column(Boolean, default=False)
    client_seq: Mapped[int | None] = mapped_column(BigInteger)
    client_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_scan_events_warehouse_received", "warehouse_id", "received_at"),
        Index("ix_scan_events_warehouse_worker", "warehouse_id", "worker_id", "received_at"),
    )


class OrderFlag(Base):
    """A worker raising a hand: 'I can't pick this line correctly.'"""

    __tablename__ = "order_flags"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # client-generated
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    line_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("order_line_items.id"))
    scan_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_events.id"))
    worker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workers.id"))
    reason: Mapped[FlagReason] = mapped_column(_enum(FlagReason, "flag_reason"))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolution_note: Mapped[str | None] = mapped_column(String(500))

    order: Mapped[Order] = relationship(back_populates="flags")
    worker: Mapped[Worker | None] = relationship()


class BarcodeAlias(Base):
    """Owner-taught equivalence: scanning `alias_key` means `target_key`.

    Both keys are normalized barcodes. Warehouse-wide: once taught, it applies
    to every order that contains the target barcode.
    """

    __tablename__ = "barcode_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    alias_key: Mapped[str] = mapped_column(String(500))
    target_key: Mapped[str] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("warehouse_id", "alias_key", name="uq_aliases_warehouse_key"),)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """Every privileged action. Append-only (enforced by trigger)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # user | worker | device | system | stripe | operator
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_label: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class StripeEvent(Base):
    """Webhook idempotency: Stripe retries, and may deliver out of order."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(100))
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RateLimitHit(Base):
    """Shared rate-limit ledger, so every API process sees the same budget."""

    __tablename__ = "rate_limit_hits"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_rate_limit_lookup", "bucket", "key", "created_at"),)
