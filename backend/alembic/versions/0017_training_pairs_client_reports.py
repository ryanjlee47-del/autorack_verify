"""Practice rounds, 3PL logos, monthly client accuracy reports.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False, index=True),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=False, index=True),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("scans", sa.Integer(), nullable=False),
        sa.Column("mistakes", sa.Integer(), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "warehouse_logos",
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), primary_key=True),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("clients", sa.Column("monthly_report", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("clients", "monthly_report")
    op.drop_table("warehouse_logos")
    op.drop_table("training_runs")
