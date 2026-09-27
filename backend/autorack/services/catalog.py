"""The product catalog, and what it does for orders.

* Linking: an order line is matched to a product by barcode (any of the
  product's barcodes) or SKU, when the order is created and whenever the
  product is saved. A linked line shows the product's picture, bin and
  packer note on the phone, and inherits its lot/serial/expiry settings.
* Extra barcodes: every barcode of the product is accepted for the line, so
  an order that lists a SKU can still be picked by scanning the real UPC.
* Case packs: a barcode with pack_qty 12 counts 12 units in one scan.
* Kits: ordering a kit means picking its parts; the kit line is split into
  component lines when the order is created.
* Substitutes: a manager-approved alternative counts for the line and is
  recorded as a substitution.
"""

from __future__ import annotations

import base64
import dataclasses
import io
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import matching
from ..errors import bad_request
from ..models import (
    KitComponent,
    Order,
    OrderLineItem,
    OrderStatus,
    Product,
    ProductBarcode,
    ProductImage,
    ProductSubstitute,
    Warehouse,
)
from . import orders as order_svc

log = logging.getLogger("autorack.catalog")

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FULL_PX = 1024
THUMB_PX = 192


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def process_image(content: bytes) -> tuple[bytes, bytes]:
    """(full JPEG, thumbnail JPEG) from any common image upload. Re-encoding
    strips metadata (GPS, camera) and anything that isn't a picture."""
    if not content:
        raise bad_request("image_empty", "That file is empty.")
    if len(content) > MAX_IMAGE_BYTES:
        raise bad_request("image_too_large", "Pictures are limited to 10 MB.")
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise bad_request("image_invalid", "That file isn't a picture we can read (JPEG, PNG, WebP or GIF).") from exc
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    img = img.convert("RGB")

    def encode(px: int, quality: int) -> bytes:
        copy = img.copy()
        copy.thumbnail((px, px))
        out = io.BytesIO()
        copy.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()

    return encode(FULL_PX, 84), encode(THUMB_PX, 76)


def set_image(db: Session, product: Product, content: bytes) -> ProductImage:
    full, thumb = process_image(content)
    img = ProductImage(
        warehouse_id=product.warehouse_id,
        product_id=product.id,
        content_type="image/jpeg",
        data=full,
        thumb=thumb,
    )
    db.add(img)
    db.flush()
    product.image_id = img.id
    return img


def thumb_data_urls(db: Session, image_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Thumbnails inlined as data: URLs, so the phone has them offline."""
    if not image_ids:
        return {}
    rows = db.execute(select(ProductImage.id, ProductImage.thumb).where(ProductImage.id.in_(image_ids)))
    return {iid: "data:image/jpeg;base64," + base64.b64encode(thumb).decode() for iid, thumb in rows}


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def key(code: str | None) -> str | None:
    k = matching.normalized_key(code or "") if code else ""
    return k or None


def get(db: Session, warehouse_id: uuid.UUID, product_id: uuid.UUID) -> Product:
    p = db.scalar(select(Product).where(Product.id == product_id, Product.warehouse_id == warehouse_id))
    if not p:
        from ..errors import not_found

        raise not_found("Product not found")
    return p


def check_barcode_free(db: Session, warehouse_id: uuid.UUID, code: str, *, product_id: uuid.UUID | None = None) -> str:
    """A barcode can only mean one product (else a scan couldn't be decided)."""
    k = key(code)
    if not k:
        raise bad_request("barcode_required", "Enter a barcode.")
    clash = db.scalar(
        select(Product.name).where(
            Product.warehouse_id == warehouse_id,
            Product.active.is_(True),
            Product.normalized_barcode == k,
            Product.id != product_id if product_id else True,
        )
    ) or db.scalar(
        select(Product.name)
        .join(ProductBarcode, ProductBarcode.product_id == Product.id)
        .where(
            ProductBarcode.warehouse_id == warehouse_id,
            ProductBarcode.normalized_barcode == k,
            Product.active.is_(True),
            ProductBarcode.product_id != product_id if product_id else True,
        )
    )
    if clash:
        raise bad_request("barcode_taken", f"That barcode already belongs to {clash}.")
    return k


def product_dict(
    db: Session, p: Product, *, full: bool = False, thumbs: dict[uuid.UUID, str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(p.id),
        "sku": p.sku,
        "name": p.name,
        "barcode": p.barcode,
        "location": p.location,
        "weight_grams": p.weight_grams,
        "packer_note": p.packer_note,
        "no_barcode": p.no_barcode,
        "track_lot": p.track_lot,
        "track_serial": p.track_serial,
        "track_expiry": p.track_expiry,
        "image_id": str(p.image_id) if p.image_id else None,
        "thumb": (thumbs or {}).get(p.image_id) if p.image_id else None,
        "client_id": str(p.client_id) if p.client_id else None,
        "source": p.source,
        "active": p.active,
        "updated_at": p.updated_at.isoformat(),
    }
    if full:
        out["barcodes"] = [
            {"id": str(b.id), "barcode": b.barcode, "pack_qty": b.pack_qty, "label": b.label}
            for b in db.scalars(
                select(ProductBarcode).where(ProductBarcode.product_id == p.id).order_by(ProductBarcode.pack_qty)
            )
        ]
        out["components"] = [
            {"product_id": str(c.id), "name": c.name, "sku": c.sku, "barcode": c.barcode, "quantity": q}
            for c, q in db.execute(
                select(Product, KitComponent.quantity)
                .join(KitComponent, KitComponent.component_id == Product.id)
                .where(KitComponent.kit_id == p.id)
                .order_by(Product.name)
            )
        ]
        out["substitutes"] = [
            {"product_id": str(sp.id), "name": sp.name, "sku": sp.sku, "barcode": sp.barcode, "note": s.note}
            for s, sp in db.execute(
                select(ProductSubstitute, Product)
                .join(Product, Product.id == ProductSubstitute.substitute_id)
                .where(ProductSubstitute.product_id == p.id)
                .order_by(Product.name)
            )
        ]
    return out


# ---------------------------------------------------------------------------
# Looking products up for order lines
# ---------------------------------------------------------------------------


@dataclass
class Catalog:
    """Everything needed to link and expand a batch of lines, in 4 queries."""

    by_key: dict[str, Product] = field(default_factory=dict)
    pack_by_key: dict[str, int] = field(default_factory=dict)
    by_sku: dict[str, Product] = field(default_factory=dict)
    kits: dict[uuid.UUID, list[tuple[Product, int]]] = field(default_factory=dict)

    def find(self, barcode: str | None, sku: str | None) -> tuple[Product | None, int]:
        """(product, units per ordered unit) for a line."""
        k = key(barcode)
        if k and k in self.by_key:
            return self.by_key[k], self.pack_by_key.get(k, 1)
        for code in (sku, barcode):
            if code and code.strip().upper() in self.by_sku:
                return self.by_sku[code.strip().upper()], 1
        return None, 1


def load(db: Session, warehouse_id: uuid.UUID, lines: list[order_svc.LineInput]) -> Catalog:
    keys = {k for li in lines for k in (key(li.barcode), key(li.sku)) if k}
    skus = {c.strip().upper() for li in lines for c in (li.sku, li.barcode) if c and c.strip()}
    cat = Catalog()
    if not keys and not skus:
        return cat
    for p in db.scalars(
        select(Product).where(
            Product.warehouse_id == warehouse_id,
            Product.active.is_(True),
            or_(Product.normalized_barcode.in_(keys), func.upper(Product.sku).in_(skus)),
        )
    ):
        if p.normalized_barcode:
            cat.by_key[p.normalized_barcode] = p
        if p.sku:
            cat.by_sku[p.sku.upper()] = p
    for b, p in db.execute(
        select(ProductBarcode, Product)
        .join(Product, Product.id == ProductBarcode.product_id)
        .where(
            ProductBarcode.warehouse_id == warehouse_id,
            ProductBarcode.normalized_barcode.in_(keys),
            Product.active.is_(True),
        )
    ):
        cat.by_key[b.normalized_barcode] = p
        cat.pack_by_key[b.normalized_barcode] = b.pack_qty
    found = {p.id for p in [*cat.by_key.values(), *cat.by_sku.values()]}
    if found:
        for kc, comp in db.execute(
            select(KitComponent, Product)
            .join(Product, Product.id == KitComponent.component_id)
            .where(KitComponent.kit_id.in_(found))
        ):
            cat.kits.setdefault(kc.kit_id, []).append((comp, kc.quantity))
    return cat


def apply(db: Session, warehouse_id: uuid.UUID, lines: list[order_svc.LineInput]) -> list[order_svc.LineInput]:
    """Link lines to products, fill blanks from the catalog, and split kits."""
    cat = load(db, warehouse_id, lines)
    out: list[order_svc.LineInput] = []
    for li in lines:
        product, per_unit = cat.find(li.barcode, li.sku)
        if product is None:
            out.append(li)
            continue
        parts = cat.kits.get(product.id)
        if parts:
            for comp, qty in parts:
                out.append(
                    _from_product(
                        comp,
                        dataclasses.replace(
                            li,
                            barcode=comp.barcode or comp.sku or f"NO-BARCODE-{str(comp.id)[:8].upper()}",
                            sku=comp.sku,
                            description=comp.name,
                            location=comp.location,
                            required_lot=None,
                        ),
                        li.quantity * qty,
                        kit=product,
                    )
                )
            continue
        linked = _from_product(product, li, li.quantity * per_unit)
        if product.barcode and key(li.barcode) != product.normalized_barcode and per_unit == 1:
            # Ordered by SKU: pick by the product's real barcode (the SKU is kept).
            known = {k for k, prod in cat.by_key.items() if prod.id == product.id}
            if key(li.barcode) not in known:
                linked = dataclasses.replace(linked, barcode=product.barcode, sku=linked.sku or li.barcode)
        out.append(linked)
    return [x for x in out if x.barcode]


def _from_product(
    p: Product, li: order_svc.LineInput, quantity: int, kit: Product | None = None
) -> order_svc.LineInput:
    return dataclasses.replace(
        li,
        quantity=min(quantity, 100_000),
        sku=li.sku or p.sku,
        description=li.description or p.name,
        location=li.location or p.location,
        track_lot=li.track_lot or p.track_lot,
        track_serial=li.track_serial or p.track_serial,
        track_expiry=li.track_expiry or p.track_expiry,
        product_id=p.id,
        kit_product_id=kit.id if kit else None,
        kit_name=kit.name if kit else None,
        confirm_without_scan=li.confirm_without_scan or p.no_barcode or not (p.barcode or p.sku),
    )


# ---------------------------------------------------------------------------
# What the match index needs: extra keys, case packs, substitutes
# ---------------------------------------------------------------------------


@dataclass
class MatchExtras:
    aliases: list[tuple[str, str]] = field(default_factory=list)  # (key, line_id)
    packs: dict[str, int] = field(default_factory=dict)  # key -> units per scan
    subs: dict[str, str] = field(default_factory=dict)  # key -> substitute's name


def match_extras(db: Session, lines: list[OrderLineItem]) -> MatchExtras:
    ex = MatchExtras()
    linked = {li.product_id: li for li in lines if li.product_id}
    if not linked:
        return ex
    own = {li.normalized_barcode for li in lines}
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(linked)))}

    def add(k: str | None, line: OrderLineItem) -> None:
        # Never let an extra key shadow another line's own barcode.
        if k and (k not in own or k == line.normalized_barcode):
            ex.aliases.append((k, str(line.id)))

    for pid, line in linked.items():
        p = products.get(pid)
        if p and p.normalized_barcode:
            add(p.normalized_barcode, line)
    for b in db.scalars(select(ProductBarcode).where(ProductBarcode.product_id.in_(linked))):
        add(b.normalized_barcode, linked[b.product_id])
        if b.pack_qty > 1:
            ex.packs[b.normalized_barcode] = b.pack_qty
    for s, sub in db.execute(
        select(ProductSubstitute, Product)
        .join(Product, Product.id == ProductSubstitute.substitute_id)
        .where(ProductSubstitute.product_id.in_(linked), Product.active.is_(True))
    ):
        line = linked[s.product_id]
        keys = [
            sub.normalized_barcode,
            *db.scalars(select(ProductBarcode.normalized_barcode).where(ProductBarcode.product_id == sub.id)),
        ]
        for k in keys:
            if k and k not in own:
                ex.aliases.append((k, str(line.id)))
                ex.subs[k] = sub.name
    return ex


# ---------------------------------------------------------------------------
# Keeping open orders in step with the catalog
# ---------------------------------------------------------------------------


def relink(db: Session, wh: Warehouse, product: Product) -> int:
    """Link open orders' lines to this product (and bump them, so phones
    fetch the new picture, barcodes and notes)."""
    keys = {k for k in [product.normalized_barcode] if k}
    keys |= set(db.scalars(select(ProductBarcode.normalized_barcode).where(ProductBarcode.product_id == product.id)))
    conds = []
    if keys:
        conds.append(OrderLineItem.normalized_barcode.in_(keys))
    if product.sku:
        conds.append(func.upper(OrderLineItem.sku) == product.sku.upper())
        conds.append(func.upper(OrderLineItem.expected_barcode) == product.sku.upper())
    conds.append(OrderLineItem.product_id == product.id)
    rows = db.execute(
        select(OrderLineItem, Order)
        .join(Order, Order.id == OrderLineItem.order_id)
        .where(
            OrderLineItem.warehouse_id == wh.id,
            Order.status.in_([OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]),
            or_(*conds),
        )
    ).all()
    bumped: set[uuid.UUID] = set()
    for line, order in rows:
        if line.product_id not in (None, product.id):
            continue
        line.product_id = product.id if product.active else None
        if product.no_barcode:
            line.confirm_without_scan = True
        if order.id not in bumped:
            order_svc.bump(order)
            bumped.add(order.id)
    return len(bumped)
