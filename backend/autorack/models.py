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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
    owner = "owner"  # everything, including billing, settings and the team
    manager = "manager"  # runs the operation; no billing, settings or team
    supervisor = "supervisor"  # floor lead: watches, resolves flags; no billing, no editing orders


class OrderStatus(enum.StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    shipped = "shipped"  # completed, and its shipping label was scanned
    flagged = "flagged"
    cancelled = "cancelled"


class OrderSource(enum.StrEnum):
    manual = "manual"
    csv = "csv"
    sample = "sample"  # onboarding demo orders
    shopify = "shopify"
    shipstation = "shipstation"
    woocommerce = "woocommerce"
    sheet = "sheet"  # a Google Sheet / CSV link polled on a schedule
    email = "email"  # a CSV attached to an email to the warehouse's import address
    drop = "drop"  # a CSV posted to the import URL (watched-folder script, Zapier...)


class IntegrationKind(enum.StrEnum):
    shopify = "shopify"
    shipstation = "shipstation"
    woocommerce = "woocommerce"
    sheet = "sheet"


class ScanResult(enum.StrEnum):
    match = "match"  # right item, counted toward the line
    over_pick = "over_pick"  # right item, but the line already has enough
    mismatch = "mismatch"  # confidently not in this order: an error caught
    review = "review"  # ambiguous or low-confidence: not counted, needs a human
    void = "void"  # a worker undid one of their earlier matches
    # Receiving, returns and counts tally what's there instead of policing a
    # pick, so they have their own results and never touch pick metrics.
    counted = "counted"  # on the list: counted toward its line (even past the expected quantity)
    extra = "extra"  # not on the list: recorded as found
    uncounted = "uncounted"  # a worker undid a counted or extra scan


class OrderKind(enum.StrEnum):
    pick = "pick"  # an order going out: the original job
    receive = "receive"  # a delivery checked against its purchase order
    ret = "return"  # a customer return checked against what shipped
    count = "count"  # a cycle count of one or more locations


TALLY_KINDS = (OrderKind.receive, OrderKind.ret, OrderKind.count)
PICK_RESULTS = (ScanResult.match, ScanResult.over_pick, ScanResult.mismatch, ScanResult.review)
# Results whose `quantity` is units (a case scan counts its pack size); every
# other result is one scan, one event.
UNIT_RESULTS = (ScanResult.match, ScanResult.void, ScanResult.counted, ScanResult.uncounted)


class FlagReason(enum.StrEnum):
    wrong_item_in_location = "wrong_item_in_location"
    out_of_stock = "out_of_stock"
    damaged = "damaged"
    label_unreadable = "label_unreadable"
    short_pick = "short_pick"  # "found only 2 of 3"
    other = "other"


class ShortReason(enum.StrEnum):
    out_of_stock = "out_of_stock"
    damaged = "damaged"
    not_found = "not_found"
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
    # Secret in this warehouse's import email address and CSV drop URL.
    import_token: Mapped[str | None] = mapped_column(String(40), unique=True)
    # "Here's what Autorack saved you" PDF to the owners on the 1st.
    monthly_report_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Matching engine settings (see matching.py). Tier 6 is opt-in.
    loose_match_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    suffix_len: Mapped[int] = mapped_column(Integer, default=8)

    # What one mistake that reaches a customer costs this warehouse (returns,
    # reshipping, credits, time). Drives the "money saved" estimate.
    cost_per_error_cents: Mapped[int] = mapped_column(Integer, default=5000, server_default="5000")
    # Notifications
    daily_summary_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    daily_summary_hour: Mapped[int] = mapped_column(Integer, default=17, server_default="17")  # local time
    alert_on_flag: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    alert_error_rate: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # Features some floors want and some don't
    leaderboard_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    require_ship_scan: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    onboarding_dismissed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    # Closing an account (owner or operator): scanning stops at once and the
    # data is deleted after a grace period (license agreement, Section 9.1),
    # unless the account is reopened first. `purged_at` marks the tombstone
    # left behind: the row and its signed agreements, nothing else.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(320))
    close_reason: Mapped[str | None] = mapped_column(String(500))
    deletion_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list[User]] = relationship(back_populates="warehouse")
    workers: Mapped[list[Worker]] = relationship(back_populates="warehouse")
    orders: Mapped[list[Order]] = relationship(back_populates="warehouse")

    __table_args__ = (
        CheckConstraint("suffix_len BETWEEN 6 AND 14", name="ck_warehouses_suffix_len"),
        CheckConstraint("daily_summary_hour BETWEEN 0 AND 23", name="ck_warehouses_summary_hour"),
        CheckConstraint("cost_per_error_cents BETWEEN 0 AND 10000000", name="ck_warehouses_cost_per_error"),
    )


class User(Base):
    """A person who signs in to the dashboard with Google.

    What they can do, and where, lives in `memberships`: one person can run
    several warehouses, with a different role in each.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Home warehouse: where a fresh sign-in lands. Empty only for an operator
    # account that runs no warehouse of its own.
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lowercased
    # Google's permanent id for the account that first signed in with this
    # email; a later sign-in with the same email but another account is refused.
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    warehouse: Mapped[Warehouse | None] = relationship(back_populates="users")


class Membership(Base):
    """A user's role at one warehouse, and which emails they get from it."""

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "membership_role"), default=UserRole.owner)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_daily_summary: Mapped[bool] = mapped_column(Boolean, default=True)
    email_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "warehouse_id", name="uq_memberships_user_warehouse"),)


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))


class OAuthState(Base):
    """One Sign in with Google attempt: the state and PKCE verifier sent to
    Google, and (for a sign-up) the warehouse details and signed agreement
    to create once Google says who this is."""

    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_verifier: Mapped[str] = mapped_column(String(128))
    intent: Mapped[str] = mapped_column(String(16))  # signin | signup
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    next_path: Mapped[str | None] = mapped_column(String(200))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # The warehouse this session is looking at (users can switch).
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"))

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
    # The "what Autorack records about you" notice, acknowledged on a phone.
    notice_version: Mapped[str | None] = mapped_column(String(16))
    notice_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class Product(Base):
    """The catalog: what a warehouse stocks, so the phone can show a picture,
    the bin and notes, and so case barcodes, kits and substitutes work."""

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    sku: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(500))
    barcode: Mapped[str | None] = mapped_column(String(200))
    normalized_barcode: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(100))
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    # Shown in big letters when this item is picked or packed ("Fragile").
    packer_note: Mapped[str | None] = mapped_column(String(500))
    no_barcode: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    track_lot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    track_serial: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    track_expiry: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    image_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Which 3PL client owns it (None for a warehouse's own stock).
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    external_id: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index(
            "uq_products_sku",
            "warehouse_id",
            "sku",
            unique=True,
            postgresql_where=text("sku IS NOT NULL AND active"),
        ),
        Index("ix_products_warehouse_barcode", "warehouse_id", "normalized_barcode"),
    )


class ProductImage(Base):
    """A product picture, resized on upload (full: 1024px, thumb: 192px)."""

    __tablename__ = "product_images"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    content_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    thumb: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProductBarcode(Base):
    """Another barcode for a product: a second unit barcode (UPC vs EAN), or
    a case/inner pack that counts `pack_qty` units in one scan."""

    __tablename__ = "product_barcodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    barcode: Mapped[str] = mapped_column(String(200))
    normalized_barcode: Mapped[str] = mapped_column(String(200))
    pack_qty: Mapped[int] = mapped_column(Integer, default=1)
    label: Mapped[str | None] = mapped_column(String(60))

    __table_args__ = (
        UniqueConstraint("warehouse_id", "normalized_barcode", name="uq_product_barcodes_key"),
        CheckConstraint("pack_qty >= 1 AND pack_qty <= 100000", name="ck_product_barcodes_pack_qty"),
    )


class KitComponent(Base):
    """A kit (bundle) is picked as its parts: ordering 1 kit means picking
    `quantity` of each component."""

    __tablename__ = "kit_components"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    kit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    component_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint("kit_id", "component_id", name="uq_kit_components_pair"),
        CheckConstraint("quantity >= 1", name="ck_kit_components_qty"),
        CheckConstraint("kit_id <> component_id", name="ck_kit_components_not_self"),
    )


class ProductSubstitute(Base):
    """A manager-approved alternative: scanning `substitute_id` on a line for
    `product_id` counts, and is recorded as a substitution, not a mistake."""

    __tablename__ = "product_substitutes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    substitute_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("product_id", "substitute_id", name="uq_product_substitutes_pair"),
        CheckConstraint("product_id <> substitute_id", name="ck_product_substitutes_not_self"),
    )


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


class Integration(Base):
    """A connected order source: a store (Shopify, ShipStation, WooCommerce)
    or a spreadsheet link, pulled on a schedule by the job runner.

    `config` holds what's safe to show (shop domain, store URL, sheet link);
    `secret` holds the API credentials, encrypted (services/secretbox.py) and
    never sent back to the browser.
    """

    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    kind: Mapped[IntegrationKind] = mapped_column(_enum(IntegrationKind, "integration_kind"))
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    secret: Mapped[str | None] = mapped_column(Text)
    # Where the last pull got to (store-specific), so each pull is incremental.
    cursor: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    push_tracking: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_minutes: Mapped[int] = mapped_column(Integer, default=10)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_created: Mapped[int] = mapped_column(Integer, default=0)
    total_created: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    external_order_number: Mapped[str | None] = mapped_column(String(100))
    customer: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), default=OrderStatus.pending, index=True
    )
    kind: Mapped[OrderKind] = mapped_column(
        _enum(OrderKind, "order_kind"), default=OrderKind.pick, server_default="pick"
    )
    # Counts: hide the expected quantity from the worker, so they count what's there.
    blind: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Returns: the shipped order this return is checked against.
    return_of_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    # Receiving/returns/counts end when the worker says so.
    finished_by_worker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workers.id"))
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
    # Pack-and-ship verification: the shipping label scanned onto the box.
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    carrier: Mapped[str | None] = mapped_column(String(32))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_by_worker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workers.id"))
    # Orders pulled from a store: where they came from, so tracking can go back.
    integration_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("integrations.id"))
    store_order_id: Mapped[str | None] = mapped_column(String(100))
    # pending -> done | failed (retried) | skipped
    tracking_push_status: Mapped[str | None] = mapped_column(String(16))
    tracking_push_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tracking_push_error: Mapped[str | None] = mapped_column(String(500))
    tracking_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Public proof-of-shipment link (/proof.html#t=...): revocable, unguessable.
    share_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    warehouse: Mapped[Warehouse] = relationship(back_populates="orders")
    line_items: Mapped[list[OrderLineItem]] = relationship(back_populates="order", order_by="OrderLineItem.line_no")
    flags: Mapped[list[OrderFlag]] = relationship(back_populates="order", order_by="OrderFlag.created_at")
    assigned_worker: Mapped[Worker | None] = relationship(foreign_keys=[assigned_worker_id])

    __table_args__ = (
        Index(
            "uq_orders_external_number",
            "warehouse_id",
            "external_order_number",
            unique=True,
            postgresql_where=text("external_order_number IS NOT NULL AND status <> 'cancelled'"),
        ),
        Index("ix_orders_warehouse_status_created", "warehouse_id", "status", "created_at"),
        Index("ix_orders_warehouse_kind_status", "warehouse_id", "kind", "status"),
        Index("ix_orders_warehouse_tracking", "warehouse_id", "tracking_number"),
        Index(
            "ix_orders_tracking_push",
            "tracking_push_status",
            postgresql_where=text("tracking_push_status IN ('pending', 'failed')"),
        ),
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
    # Units a worker reported they couldn't find (see OrderFlag.short_quantity).
    short_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sku: Mapped[str | None] = mapped_column(String(100))
    sku_description: Mapped[str | None] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(100))
    # Traceability: what the worker must record for each unit of this line.
    track_lot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    track_serial: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    track_expiry: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Only this lot may ship (a recall, a customer's spec, first-expiry-first-out).
    required_lot: Mapped[str | None] = mapped_column(String(100))
    # The catalog product this line is (matched by barcode or SKU), and the
    # kit it came from when an ordered kit was split into its parts.
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"), index=True)
    kit_product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"))
    kit_name: Mapped[str | None] = mapped_column(String(300))
    # No barcode on this item: the worker confirms it by tapping (recorded as
    # not scan-verified).
    confirm_without_scan: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("order_id", "normalized_barcode", name="uq_line_items_order_barcode"),
        # 0 is allowed for count lists ("the system says none here"); picks,
        # receipts and returns require at least 1 in the service layer.
        CheckConstraint("expected_quantity >= 0", name="ck_line_items_expected_qty"),
        CheckConstraint("scanned_quantity >= 0", name="ck_line_items_scanned_qty"),
        CheckConstraint("short_quantity >= 0", name="ck_line_items_short_qty"),
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
    # Lot / serial / expiry of the unit scanned (from its GS1 barcode or typed).
    lot: Mapped[str | None] = mapped_column(String(100))
    serial: Mapped[str | None] = mapped_column(String(100))
    expiry: Mapped[date | None] = mapped_column(Date)
    # Why a scan of the right product was refused: wrong_lot | expired |
    # serial_repeat | details_missing.
    problem: Mapped[str | None] = mapped_column(String(32))
    # Units this scan stands for: a case barcode counts its pack size.
    quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # An approved substitute was scanned in place of the ordered product.
    substitution: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Confirmed by tapping, for an item with no barcode: not scan-verified.
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    was_offline: Mapped[bool] = mapped_column(Boolean, default=False)
    client_seq: Mapped[int | None] = mapped_column(BigInteger)
    client_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_scan_events_warehouse_received", "warehouse_id", "received_at"),
        Index("ix_scan_events_warehouse_worker", "warehouse_id", "worker_id", "received_at"),
        # Recall lookups ("which orders shipped lot X / serial Y?").
        Index("ix_scan_events_warehouse_lot", "warehouse_id", "lot", postgresql_where=text("lot IS NOT NULL")),
        Index("ix_scan_events_warehouse_serial", "warehouse_id", "serial", postgresql_where=text("serial IS NOT NULL")),
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
    # Short picks: how many units couldn't be picked, and why.
    short_quantity: Mapped[int | None] = mapped_column(Integer)
    short_reason: Mapped[ShortReason | None] = mapped_column(_enum(ShortReason, "short_reason"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolution_note: Mapped[str | None] = mapped_column(String(500))
    # accepted (ship it short) | reopened (pick again) | resolved (plain flag)
    resolution: Mapped[str | None] = mapped_column(String(16))

    order: Mapped[Order] = relationship(back_populates="flags")
    worker: Mapped[Worker | None] = relationship()


class Photo(Base):
    """A picture a worker took of a problem (damaged box, empty bin...).

    Stored in Postgres rather than on disk: free app hosts have no persistent
    disk, and a phone-compressed JPEG is ~100-250 KB. Linked to a flag; the id
    is generated on the phone so offline uploads are idempotent.
    """

    __tablename__ = "photos"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    flag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("order_flags.id"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workers.id"))
    content_type: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


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


class AgreementSignature(Base):
    """A warehouse owner's electronic signature on the license agreement.

    Append-only (trigger): a signature is evidence, so it is never edited.
    Holds everything needed to prove it later: which exact document (version
    and SHA-256), who signed and for which company, when, from where, and the
    signed PDF itself, stamped and with a signature certificate page.
    """

    __tablename__ = "agreement_signatures"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    agreement_version: Mapped[str] = mapped_column(String(32))
    document_sha256: Mapped[str] = mapped_column(String(64))
    signer_name: Mapped[str] = mapped_column(String(200))
    signer_title: Mapped[str] = mapped_column(String(200))
    signer_email: Mapped[str] = mapped_column(String(320))
    company_name: Mapped[str] = mapped_column(String(300))
    company_address: Mapped[str] = mapped_column(String(500))
    consent_text: Mapped[str] = mapped_column(Text)
    viewed_seconds: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    signed_pdf: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    signed_pdf_sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (Index("ix_agreement_signatures_wh_version", "warehouse_id", "agreement_version"),)


class NotificationSent(Base):
    """Every automatic email, once. The unique key makes each send idempotent,
    so a job that runs twice (two processes, a retry) never emails twice."""

    __tablename__ = "notifications_sent"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    warehouse_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("warehouses.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    key: Mapped[str] = mapped_column(String(128))
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("warehouse_id", "kind", "key", name="uq_notifications_once"),)


class FeatureUsage(Base):
    """Daily per-warehouse feature counters, for the operator's usage view."""

    __tablename__ = "feature_usage"

    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    feature: Mapped[str] = mapped_column(String(48), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class ErrorEvent(Base):
    """Something broke: a server exception, a failed job, a browser error.

    One row per distinct problem (its signature), counting repeats, so a bug
    hit a thousand times is one line and one alert, not a thousand.
    """

    __tablename__ = "error_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(16))  # server | job | browser
    kind: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(1000))
    detail: Mapped[str | None] = mapped_column(Text)  # traceback or stack, truncated
    context: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)  # last occurrence: path, app, ...
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted_count: Mapped[int] = mapped_column(Integer, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RateLimitHit(Base):
    """Shared rate-limit ledger, so every API process sees the same budget."""

    __tablename__ = "rate_limit_hits"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_rate_limit_lookup", "bucket", "key", "created_at"),)
