"""Packing photos, batch picking with totes.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "warehouses", sa.Column("require_pack_photo", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("photos", "flag_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("photos", sa.Column("kind", sa.String(16), nullable=False, server_default="problem"))
    op.create_table(
        "pick_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("assigned_worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pick_batches_warehouse_id"), "pick_batches", ["warehouse_id"])
    op.add_column("orders", sa.Column("batch_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_orders_batch_id", "orders", "pick_batches", ["batch_id"], ["id"])
    op.create_index(op.f("ix_orders_batch_id"), "orders", ["batch_id"])
    op.add_column("orders", sa.Column("tote", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "tote")
    op.drop_index(op.f("ix_orders_batch_id"), table_name="orders")
    op.drop_constraint("fk_orders_batch_id", "orders", type_="foreign_key")
    op.drop_column("orders", "batch_id")
    op.drop_table("pick_batches")
    op.drop_column("photos", "kind")
    op.execute("DELETE FROM photos WHERE flag_id IS NULL")
    op.alter_column("photos", "flag_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("warehouses", "require_pack_photo")
