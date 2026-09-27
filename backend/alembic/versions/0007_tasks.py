"""Receiving, returns and cycle counts: order kinds and tally scan results.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

OLD_RESULTS = ("match", "over_pick", "mismatch", "review", "void")
NEW_RESULTS = (*OLD_RESULTS, "counted", "extra", "uncounted")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "kind",
            sa.String(32),
            sa.CheckConstraint("kind IN ('pick', 'receive', 'return', 'count')", name="order_kind"),
            nullable=False,
            server_default="pick",
        ),
    )
    op.add_column("orders", sa.Column("blind", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("return_of_order_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_orders_return_of_order_id", "orders", "orders", ["return_of_order_id"], ["id"])
    op.add_column("orders", sa.Column("finished_by_worker_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_orders_finished_by_worker_id", "orders", "workers", ["finished_by_worker_id"], ["id"])
    op.create_index("ix_orders_warehouse_kind_status", "orders", ["warehouse_id", "kind", "status"])

    op.drop_constraint("scan_result", "scan_events", type_="check")
    op.create_check_constraint("scan_result", "scan_events", _in("result", NEW_RESULTS))

    op.drop_constraint("ck_line_items_expected_qty", "order_line_items", type_="check")
    op.create_check_constraint("ck_line_items_expected_qty", "order_line_items", "expected_quantity >= 0")


def downgrade() -> None:
    # Tally scans are append-only history; refuse rather than rewrite it.
    op.drop_constraint("ck_line_items_expected_qty", "order_line_items", type_="check")
    op.create_check_constraint("ck_line_items_expected_qty", "order_line_items", "expected_quantity >= 1")
    op.drop_constraint("scan_result", "scan_events", type_="check")
    op.create_check_constraint("scan_result", "scan_events", _in("result", OLD_RESULTS))
    op.drop_index("ix_orders_warehouse_kind_status", table_name="orders")
    op.drop_constraint("fk_orders_finished_by_worker_id", "orders", type_="foreign_key")
    op.drop_column("orders", "finished_by_worker_id")
    op.drop_constraint("fk_orders_return_of_order_id", "orders", type_="foreign_key")
    op.drop_column("orders", "return_of_order_id")
    op.drop_column("orders", "blind")
    op.drop_column("orders", "kind")
