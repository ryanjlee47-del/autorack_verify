"""Product catalog: images, case packs, kits, substitutes; scan quantities.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("sku", sa.String(100), nullable=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("barcode", sa.String(200), nullable=True),
        sa.Column("normalized_barcode", sa.String(200), nullable=True),
        sa.Column("location", sa.String(100), nullable=True),
        sa.Column("weight_grams", sa.Integer(), nullable=True),
        sa.Column("packer_note", sa.String(500), nullable=True),
        sa.Column("no_barcode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("track_lot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("track_serial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("track_expiry", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("image_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("external_id", sa.String(100), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_products_warehouse_id"), "products", ["warehouse_id"])
    op.create_index(op.f("ix_products_client_id"), "products", ["client_id"])
    op.create_index("ix_products_warehouse_barcode", "products", ["warehouse_id", "normalized_barcode"])
    op.create_index(
        "uq_products_sku",
        "products",
        ["warehouse_id", "sku"],
        unique=True,
        postgresql_where=sa.text("sku IS NOT NULL AND active"),
    )
    op.create_table(
        "product_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("thumb", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_product_images_warehouse_id"), "product_images", ["warehouse_id"])
    op.create_index(op.f("ix_product_images_product_id"), "product_images", ["product_id"])
    op.create_table(
        "product_barcodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("barcode", sa.String(200), nullable=False),
        sa.Column("normalized_barcode", sa.String(200), nullable=False),
        sa.Column("pack_qty", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(60), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("warehouse_id", "normalized_barcode", name="uq_product_barcodes_key"),
        sa.CheckConstraint("pack_qty >= 1 AND pack_qty <= 100000", name="ck_product_barcodes_pack_qty"),
    )
    op.create_index(op.f("ix_product_barcodes_warehouse_id"), "product_barcodes", ["warehouse_id"])
    op.create_index(op.f("ix_product_barcodes_product_id"), "product_barcodes", ["product_id"])
    op.create_table(
        "kit_components",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("kit_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("component_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kit_id", "component_id", name="uq_kit_components_pair"),
        sa.CheckConstraint("quantity >= 1", name="ck_kit_components_qty"),
        sa.CheckConstraint("kit_id <> component_id", name="ck_kit_components_not_self"),
    )
    op.create_index(op.f("ix_kit_components_warehouse_id"), "kit_components", ["warehouse_id"])
    op.create_index(op.f("ix_kit_components_kit_id"), "kit_components", ["kit_id"])
    op.create_table(
        "product_substitutes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("substitute_id", sa.Uuid(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "substitute_id", name="uq_product_substitutes_pair"),
        sa.CheckConstraint("product_id <> substitute_id", name="ck_product_substitutes_not_self"),
    )
    op.create_index(op.f("ix_product_substitutes_warehouse_id"), "product_substitutes", ["warehouse_id"])
    op.create_index(op.f("ix_product_substitutes_product_id"), "product_substitutes", ["product_id"])

    op.add_column("order_line_items", sa.Column("product_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_line_items_product_id", "order_line_items", "products", ["product_id"], ["id"])
    op.create_index(op.f("ix_order_line_items_product_id"), "order_line_items", ["product_id"])
    op.add_column("order_line_items", sa.Column("kit_product_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_line_items_kit_product_id", "order_line_items", "products", ["kit_product_id"], ["id"])
    op.add_column("order_line_items", sa.Column("kit_name", sa.String(300), nullable=True))
    op.add_column(
        "order_line_items",
        sa.Column("confirm_without_scan", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("scan_events", sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("scan_events", sa.Column("substitution", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("scan_events", sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    for col in ("confirmed", "substitution", "quantity"):
        op.drop_column("scan_events", col)
    op.drop_column("order_line_items", "confirm_without_scan")
    op.drop_column("order_line_items", "kit_name")
    op.drop_constraint("fk_line_items_kit_product_id", "order_line_items", type_="foreign_key")
    op.drop_column("order_line_items", "kit_product_id")
    op.drop_index(op.f("ix_order_line_items_product_id"), table_name="order_line_items")
    op.drop_constraint("fk_line_items_product_id", "order_line_items", type_="foreign_key")
    op.drop_column("order_line_items", "product_id")
    for t in ("product_substitutes", "kit_components", "product_barcodes", "product_images", "products"):
        op.drop_table(t)
