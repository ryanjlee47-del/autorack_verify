"""Rush orders and ship cutoffs, 3PL clients, pack inserts, multi-box shipments,
restock tasks, time clock.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("warehouses", sa.Column("ship_cutoff", sa.String(5), nullable=True))
    op.add_column(
        "warehouses", sa.Column("time_clock_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(40), nullable=True),
        sa.Column("contact_email", sa.String(320), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_clients_warehouse_id", "clients", ["warehouse_id"])
    op.create_index("uq_clients_name", "clients", ["warehouse_id", sa.text("lower(name)")], unique=True)
    op.create_foreign_key("fk_products_client_id", "products", "clients", ["client_id"], ["id"])

    op.add_column("orders", sa.Column("rush", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("ship_by", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_orders_client_id", "orders", "clients", ["client_id"], ["id"])
    op.create_index("ix_orders_client_id", "orders", ["client_id"])

    op.create_table(
        "pack_inserts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("barcode", sa.String(200), nullable=True),
        sa.Column("normalized_barcode", sa.String(200), nullable=True),
        sa.Column("scan_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("client_id", sa.Uuid(), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pack_inserts_warehouse_id", "pack_inserts", ["warehouse_id"])
    op.create_table(
        "order_insert_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("insert_id", sa.Uuid(), sa.ForeignKey("pack_inserts.id"), nullable=False),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("scanned", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", "insert_id", name="uq_order_insert_checks"),
    )
    op.create_index("ix_order_insert_checks_warehouse_id", "order_insert_checks", ["warehouse_id"])
    op.create_index("ix_order_insert_checks_order_id", "order_insert_checks", ["order_id"])

    op.create_table(
        "packages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("box_no", sa.Integer(), nullable=False),
        sa.Column("tracking_number", sa.String(100), nullable=False),
        sa.Column("carrier", sa.String(32), nullable=True),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", "box_no", name="uq_packages_order_box"),
    )
    op.create_index("ix_packages_order_id", "packages", ["order_id"])
    op.create_index("ix_packages_warehouse_tracking", "packages", ["warehouse_id", "tracking_number"])
    # Every order shipped so far went out in one box.
    op.execute(
        """
        INSERT INTO packages (id, warehouse_id, order_id, box_no, tracking_number, carrier, worker_id, created_at)
        SELECT gen_random_uuid(), warehouse_id, id, 1, tracking_number, carrier, shipped_by_worker_id,
               COALESCE(shipped_at, updated_at)
        FROM orders WHERE tracking_number IS NOT NULL
        """
    )
    op.add_column("photos", sa.Column("box", sa.Integer(), nullable=True))

    op.create_table(
        "restock_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("location", sa.String(100), nullable=True),
        sa.Column("barcode", sa.String(200), nullable=True),
        sa.Column("normalized_barcode", sa.String(200), nullable=True),
        sa.Column("sku", sa.String(100), nullable=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("reported_by_worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by_worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("done_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_restock_tasks_warehouse_id", "restock_tasks", ["warehouse_id"])
    op.create_index("ix_restock_tasks_status", "restock_tasks", ["status"])

    op.create_table(
        "shifts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("device_id", sa.Uuid(), sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("clock_in", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clock_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(16), nullable=True),
        sa.Column("edited_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_shifts_warehouse_id", "shifts", ["warehouse_id"])
    op.create_index("ix_shifts_worker_id", "shifts", ["worker_id"])
    op.create_index(
        "uq_shifts_open", "shifts", ["worker_id"], unique=True, postgresql_where=sa.text("clock_out IS NULL")
    )


def downgrade() -> None:
    op.drop_table("shifts")
    op.drop_table("restock_tasks")
    op.drop_column("photos", "box")
    op.drop_table("packages")
    op.drop_table("order_insert_checks")
    op.drop_table("pack_inserts")
    op.drop_index("ix_orders_client_id", table_name="orders")
    op.drop_constraint("fk_orders_client_id", "orders", type_="foreignkey")
    op.drop_column("orders", "client_id")
    op.drop_column("orders", "ship_by")
    op.drop_column("orders", "rush")
    op.drop_constraint("fk_products_client_id", "products", type_="foreignkey")
    op.drop_table("clients")
    op.drop_column("warehouses", "time_clock_enabled")
    op.drop_column("warehouses", "ship_cutoff")
