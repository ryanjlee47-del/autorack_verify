"""Security hardening from the penetration test.

* memberships.pending: an existing Autorack user added to another
  warehouse must accept before it appears (no silent adds).
* magic_link_tokens.nonce_hash: a sign-in code only works in the browser
  that started the sign-in (no login CSRF).
* owner_sessions.authenticated_at: when the person last proved who they are
  with Google (operator actions need a recent one).

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("magic_link_tokens", sa.Column("nonce_hash", sa.String(64)))
    op.add_column("owner_sessions", sa.Column("authenticated_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE owner_sessions SET authenticated_at = created_at")


def downgrade() -> None:
    op.drop_column("owner_sessions", "authenticated_at")
    op.drop_column("magic_link_tokens", "nonce_hash")
    op.drop_column("memberships", "pending")
