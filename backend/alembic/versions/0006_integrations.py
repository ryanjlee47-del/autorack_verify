"""Store integrations, auto-import and tracking push-back.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
OLD_SOURCES = ("manual", "csv", "sample")
NEW_SOURCES = (*OLD_SOURCES, "shopify", "shipstation", "woocommerce", "sheet", "email", "drop")


def _source_check(values: tuple[str, ...]) -> None:
    op.drop_constraint("order_source", "orders", type_="check")
    op.create_check_constraint("order_source", "orders", "source IN (" + ", ".join(f"'{v}'" for v in values) + ")")


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column(
            "kind",
            sa.String(32),
            sa.CheckConstraint("kind IN ('shopify', 'shipstation', 'woocommerce', 'sheet')", name="integration_kind"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("config", JSON, nullable=False),
        sa.Column("secret", sa.Text(), nullable=True),
        sa.Column("cursor", JSON, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("push_tracking", sa.Boolean(), nullable=False),
        sa.Column("sync_minutes", sa.Integer(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_created", sa.Integer(), nullable=False),
        sa.Column("total_created", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_integrations_warehouse_id"), "integrations", ["warehouse_id"])

    op.add_column("orders", sa.Column("integration_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_orders_integration_id", "orders", "integrations", ["integration_id"], ["id"])
    op.add_column("orders", sa.Column("store_order_id", sa.String(100), nullable=True))
    op.add_column("orders", sa.Column("tracking_push_status", sa.String(16), nullable=True))
    op.add_column("orders", sa.Column("tracking_push_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("tracking_push_error", sa.String(500), nullable=True))
    op.add_column("orders", sa.Column("tracking_pushed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_orders_tracking_push",
        "orders",
        ["tracking_push_status"],
        postgresql_where=sa.text("tracking_push_status IN ('pending', 'failed')"),
    )
    _source_check(NEW_SOURCES)

    op.add_column("warehouses", sa.Column("import_token", sa.String(40), nullable=True))
    op.create_unique_constraint("uq_warehouses_import_token", "warehouses", ["import_token"])


def downgrade() -> None:
    op.drop_constraint("uq_warehouses_import_token", "warehouses", type_="unique")
    op.drop_column("warehouses", "import_token")
    op.execute("UPDATE orders SET source = 'csv' WHERE source NOT IN ('manual', 'csv', 'sample')")
    _source_check(OLD_SOURCES)
    op.drop_index("ix_orders_tracking_push", table_name="orders")
    for col in (
        "tracking_pushed_at",
        "tracking_push_error",
        "tracking_push_attempts",
        "tracking_push_status",
        "store_order_id",
    ):
        op.drop_column("orders", col)
    op.drop_constraint("fk_orders_integration_id", "orders", type_="foreign_key")
    op.drop_column("orders", "integration_id")
    op.drop_table("integrations")
