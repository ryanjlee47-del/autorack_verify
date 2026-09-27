"""Yearly plan and founding-customer price lock.

Every warehouse that exists today is a founding customer at today's prices.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("warehouses", sa.Column("billing_interval", sa.String(8)))
    op.add_column("warehouses", sa.Column("founding_since", sa.DateTime(timezone=True)))
    op.add_column("warehouses", sa.Column("founding_month_cents", sa.Integer()))
    op.add_column("warehouses", sa.Column("founding_year_cents", sa.Integer()))
    op.add_column("warehouses", sa.Column("founding_forfeited_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE warehouses SET billing_interval = 'month' WHERE stripe_subscription_id IS NOT NULL")
    op.execute(
        "UPDATE warehouses SET founding_since = created_at, founding_month_cents = 2900, "
        "founding_year_cents = 29000 WHERE purged_at IS NULL AND subscription_status <> 'canceled'"
    )


def downgrade() -> None:
    for col in (
        "founding_forfeited_at",
        "founding_year_cents",
        "founding_month_cents",
        "founding_since",
        "billing_interval",
    ):
        op.drop_column("warehouses", col)
