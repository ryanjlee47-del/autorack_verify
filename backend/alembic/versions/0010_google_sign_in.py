"""Sign in with Google: account binding and OAuth attempts.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_users_google_sub", "users", ["google_sub"])
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("intent", sa.String(16), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False
        ),
        sa.Column("next_path", sa.String(200), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index(op.f("ix_oauth_states_expires_at"), "oauth_states", ["expires_at"])


def downgrade() -> None:
    op.drop_table("oauth_states")
    op.drop_constraint("uq_users_google_sub", "users", type_="unique")
    op.drop_column("users", "google_sub")
