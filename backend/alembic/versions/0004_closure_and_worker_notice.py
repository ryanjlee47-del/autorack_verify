"""Account closure and deletion; worker privacy notice.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Deleting a closed account's data is the one legitimate DELETE on the
# append-only tables. It happens only inside a transaction that has set
# autorack.purging = 'on' (SET LOCAL), which the purge job does and nothing
# else. UPDATE stays forbidden without exception.
APPEND_ONLY_WITH_PURGE = """
CREATE OR REPLACE FUNCTION autorack_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('autorack.purging', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_STRICT = """
CREATE OR REPLACE FUNCTION autorack_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("warehouses", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("warehouses", sa.Column("closed_by", sa.String(320), nullable=True))
    op.add_column("warehouses", sa.Column("close_reason", sa.String(500), nullable=True))
    op.add_column("warehouses", sa.Column("deletion_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("warehouses", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("notice_version", sa.String(16), nullable=True))
    op.add_column("workers", sa.Column("notice_acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(APPEND_ONLY_WITH_PURGE)


def downgrade() -> None:
    op.execute(APPEND_ONLY_STRICT)
    op.drop_column("workers", "notice_acknowledged_at")
    op.drop_column("workers", "notice_version")
    for col in ("purged_at", "deletion_due_at", "close_reason", "closed_by", "closed_at"):
        op.drop_column("warehouses", col)
