"""Shareable proof links; monthly report opt-out.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("share_token", sa.String(64), nullable=True))
    op.add_column("orders", sa.Column("shared_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_orders_share_token", "orders", ["share_token"])
    op.add_column(
        "warehouses", sa.Column("monthly_report_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
    )


def downgrade() -> None:
    op.drop_column("warehouses", "monthly_report_enabled")
    op.drop_constraint("uq_orders_share_token", "orders", type_="unique")
    op.drop_column("orders", "shared_at")
    op.drop_column("orders", "share_token")
