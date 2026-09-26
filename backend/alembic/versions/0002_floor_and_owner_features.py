"""Pack & ship, short picks, photos, memberships, notifications, usage.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _replace_enum_check(table: str, column: str, name: str, values: list[str]) -> None:
    op.drop_constraint(name, table, type_="check")
    quoted = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(name, table, f"{column} IN ({quoted})")


def upgrade() -> None:
    # --- warehouses: settings for the new features ---------------------------
    with op.batch_alter_table("warehouses") as b:
        b.add_column(sa.Column("cost_per_error_cents", sa.Integer(), nullable=False, server_default="5000"))
        b.add_column(sa.Column("daily_summary_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        b.add_column(sa.Column("daily_summary_hour", sa.Integer(), nullable=False, server_default="17"))
        b.add_column(sa.Column("alert_on_flag", sa.Boolean(), nullable=False, server_default=sa.true()))
        b.add_column(sa.Column("alert_error_rate", sa.Boolean(), nullable=False, server_default=sa.true()))
        b.add_column(sa.Column("leaderboard_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("require_ship_scan", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("onboarding_dismissed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint("ck_warehouses_summary_hour", "warehouses", "daily_summary_hour BETWEEN 0 AND 23")
    op.create_check_constraint(
        "ck_warehouses_cost_per_error", "warehouses", "cost_per_error_cents BETWEEN 0 AND 10000000"
    )

    # --- memberships: roles move off users ------------------------------------
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "manager",
                "supervisor",
                name="membership_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("email_daily_summary", sa.Boolean(), nullable=False),
        sa.Column("email_alerts", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "warehouse_id", name="uq_memberships_user_warehouse"),
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"])
    op.create_index(op.f("ix_memberships_warehouse_id"), "memberships", ["warehouse_id"])
    op.execute(
        """
        INSERT INTO memberships (id, user_id, warehouse_id, role, active,
                                 email_daily_summary, email_alerts, created_at)
        SELECT gen_random_uuid(), id, warehouse_id, role, active,
               role = 'owner', role = 'owner', created_at
        FROM users
        """
    )
    op.drop_constraint("user_role", "users", type_="check")
    op.drop_column("users", "role")
    op.alter_column("users", "warehouse_id", existing_type=sa.Uuid(), nullable=True)

    op.add_column("owner_sessions", sa.Column("warehouse_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_owner_sessions_warehouse_id", "owner_sessions", "warehouses", ["warehouse_id"], ["id"])

    # --- orders: customer and shipping ----------------------------------------
    op.add_column("orders", sa.Column("customer", sa.String(200), nullable=True))
    op.add_column("orders", sa.Column("tracking_number", sa.String(100), nullable=True))
    op.add_column("orders", sa.Column("carrier", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("shipped_by_worker_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_orders_shipped_by_worker_id", "orders", "workers", ["shipped_by_worker_id"], ["id"])
    op.create_index(op.f("ix_orders_customer"), "orders", ["customer"])
    op.create_index("ix_orders_warehouse_tracking", "orders", ["warehouse_id", "tracking_number"])
    _replace_enum_check(
        "orders",
        "status",
        "order_status",
        ["pending", "in_progress", "completed", "shipped", "flagged", "cancelled"],
    )
    _replace_enum_check("orders", "source", "order_source", ["manual", "csv", "sample"])

    # --- short picks ------------------------------------------------------------
    op.add_column("order_line_items", sa.Column("short_quantity", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_line_items_short_qty", "order_line_items", "short_quantity >= 0")
    op.add_column("order_flags", sa.Column("short_quantity", sa.Integer(), nullable=True))
    op.add_column(
        "order_flags",
        sa.Column(
            "short_reason",
            sa.Enum(
                "out_of_stock",
                "damaged",
                "not_found",
                "other",
                name="short_reason",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.add_column("order_flags", sa.Column("resolution", sa.String(16), nullable=True))
    _replace_enum_check(
        "order_flags",
        "reason",
        "flag_reason",
        ["wrong_item_in_location", "out_of_stock", "damaged", "label_unreadable", "short_pick", "other"],
    )

    # --- photos -------------------------------------------------------------------
    op.create_table(
        "photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("flag_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=True),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["flag_id"], ["order_flags.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_photos_warehouse_id"), "photos", ["warehouse_id"])
    op.create_index(op.f("ix_photos_flag_id"), "photos", ["flag_id"])
    op.create_index(op.f("ix_photos_order_id"), "photos", ["order_id"])
    op.create_index(op.f("ix_photos_created_at"), "photos", ["created_at"])

    # --- notifications and usage -------------------------------------------------
    op.create_table(
        "notifications_sent",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("recipients", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("warehouse_id", "kind", "key", name="uq_notifications_once"),
    )
    op.create_index(op.f("ix_notifications_sent_warehouse_id"), "notifications_sent", ["warehouse_id"])
    op.create_table(
        "feature_usage",
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("feature", sa.String(48), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"]),
        sa.PrimaryKeyConstraint("warehouse_id", "feature", "day"),
    )


def downgrade() -> None:
    op.drop_table("feature_usage")
    op.drop_index(op.f("ix_notifications_sent_warehouse_id"), table_name="notifications_sent")
    op.drop_table("notifications_sent")
    op.drop_table("photos")
    _replace_enum_check(
        "order_flags",
        "reason",
        "flag_reason",
        ["wrong_item_in_location", "out_of_stock", "damaged", "label_unreadable", "other"],
    )
    op.drop_column("order_flags", "resolution")
    op.drop_constraint("short_reason", "order_flags", type_="check")
    op.drop_column("order_flags", "short_reason")
    op.drop_column("order_flags", "short_quantity")
    op.drop_constraint("ck_line_items_short_qty", "order_line_items", type_="check")
    op.drop_column("order_line_items", "short_quantity")
    _replace_enum_check("orders", "source", "order_source", ["manual", "csv"])
    _replace_enum_check(
        "orders", "status", "order_status", ["pending", "in_progress", "completed", "flagged", "cancelled"]
    )
    op.drop_index("ix_orders_warehouse_tracking", table_name="orders")
    op.drop_index(op.f("ix_orders_customer"), table_name="orders")
    op.drop_constraint("fk_orders_shipped_by_worker_id", "orders", type_="foreignkey")
    for col in ("shipped_by_worker_id", "shipped_at", "carrier", "tracking_number", "customer"):
        op.drop_column("orders", col)
    op.drop_constraint("fk_owner_sessions_warehouse_id", "owner_sessions", type_="foreignkey")
    op.drop_column("owner_sessions", "warehouse_id")
    op.execute("DELETE FROM users WHERE warehouse_id IS NULL")
    op.alter_column("users", "warehouse_id", existing_type=sa.Uuid(), nullable=False)
    op.add_column("users", sa.Column("role", sa.String(32), nullable=False, server_default="owner"))
    op.execute(
        "UPDATE users u SET role = CASE WHEN m.role = 'owner' THEN 'owner' ELSE 'manager' END "
        "FROM memberships m WHERE m.user_id = u.id AND m.warehouse_id = u.warehouse_id"
    )
    op.create_check_constraint("user_role", "users", "role IN ('owner', 'manager')")
    op.drop_table("memberships")
    op.drop_constraint("ck_warehouses_cost_per_error", "warehouses", type_="check")
    op.drop_constraint("ck_warehouses_summary_hour", "warehouses", type_="check")
    for col in (
        "onboarding_dismissed",
        "require_ship_scan",
        "leaderboard_enabled",
        "alert_error_rate",
        "alert_on_flag",
        "daily_summary_hour",
        "daily_summary_enabled",
        "cost_per_error_cents",
    ):
        op.drop_column("warehouses", col)
