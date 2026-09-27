"""License agreement e-signatures.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agreement_signatures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("agreement_version", sa.String(32), nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("signer_name", sa.String(200), nullable=False),
        sa.Column("signer_title", sa.String(200), nullable=False),
        sa.Column("signer_email", sa.String(320), nullable=False),
        sa.Column("company_name", sa.String(300), nullable=False),
        sa.Column("company_address", sa.String(500), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("viewed_seconds", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signed_pdf", sa.LargeBinary(), nullable=False),
        sa.Column("signed_pdf_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agreement_signatures_warehouse_id"), "agreement_signatures", ["warehouse_id"])
    op.create_index("ix_agreement_signatures_wh_version", "agreement_signatures", ["warehouse_id", "agreement_version"])
    # Evidence: never edited or deleted (same guard as scan_events and audit_log).
    op.execute(
        "CREATE TRIGGER agreement_signatures_append_only BEFORE UPDATE OR DELETE ON agreement_signatures "
        "FOR EACH ROW EXECUTE FUNCTION autorack_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS agreement_signatures_append_only ON agreement_signatures")
    op.drop_table("agreement_signatures")
