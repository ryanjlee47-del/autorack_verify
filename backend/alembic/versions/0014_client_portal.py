"""3PL client portal logins and per-client billing rates.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _roles(values: list[str]) -> None:
    op.drop_constraint("membership_role", "memberships", type_="check")
    quoted = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint("membership_role", "memberships", f"role IN ({quoted})")


def upgrade() -> None:
    _roles(["owner", "manager", "supervisor", "client"])
    op.add_column("memberships", sa.Column("client_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_memberships_client_id", "memberships", "clients", ["client_id"], ["id"])
    op.add_column(
        "clients",
        sa.Column(
            "rates", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False, server_default="{}"
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "rates")
    op.execute("DELETE FROM memberships WHERE role = 'client'")
    op.drop_constraint("fk_memberships_client_id", "memberships", type_="foreignkey")
    op.drop_column("memberships", "client_id")
    _roles(["owner", "manager", "supervisor"])
