"""Lot, serial and expiry capture.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("track_lot", "track_serial", "track_expiry"):
        op.add_column("order_line_items", sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("order_line_items", sa.Column("required_lot", sa.String(100), nullable=True))
    # scan_events is append-only (trigger on UPDATE/DELETE); adding nullable
    # columns doesn't touch existing rows.
    op.add_column("scan_events", sa.Column("lot", sa.String(100), nullable=True))
    op.add_column("scan_events", sa.Column("serial", sa.String(100), nullable=True))
    op.add_column("scan_events", sa.Column("expiry", sa.Date(), nullable=True))
    op.add_column("scan_events", sa.Column("problem", sa.String(32), nullable=True))
    op.create_index(
        "ix_scan_events_warehouse_lot",
        "scan_events",
        ["warehouse_id", "lot"],
        postgresql_where=sa.text("lot IS NOT NULL"),
    )
    op.create_index(
        "ix_scan_events_warehouse_serial",
        "scan_events",
        ["warehouse_id", "serial"],
        postgresql_where=sa.text("serial IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_scan_events_warehouse_serial", table_name="scan_events")
    op.drop_index("ix_scan_events_warehouse_lot", table_name="scan_events")
    for col in ("problem", "expiry", "serial", "lot"):
        op.drop_column("scan_events", col)
    for col in ("required_lot", "track_expiry", "track_serial", "track_lot"):
        op.drop_column("order_line_items", col)
