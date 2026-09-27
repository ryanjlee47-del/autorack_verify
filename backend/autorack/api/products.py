"""The product catalog: products, pictures, extra and case barcodes, kits,
approved substitutes, CSV import, and barcodes for items that have none."""

from __future__ import annotations

import csv
import io
import re
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, current_owner, require_manager
from ..errors import ApiError, bad_request, conflict, not_found
from ..models import KitComponent, Product, ProductBarcode, ProductImage, ProductSubstitute
from ..services import audit, catalog, csv_import, usage
from ..services import orders as order_svc

router = APIRouter(tags=["products"])


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=100)
    weight_grams: int | None = Field(default=None, ge=0, le=10_000_000)
    packer_note: str | None = Field(default=None, max_length=500)
    no_barcode: bool = False
    track_lot: bool = False
    track_serial: bool = False
    track_expiry: bool = False


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=100)
    weight_grams: int | None = Field(default=None, ge=0, le=10_000_000)
    packer_note: str | None = Field(default=None, max_length=500)
    no_barcode: bool | None = None
    track_lot: bool | None = None
    track_serial: bool | None = None
    track_expiry: bool | None = None


class BarcodeIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=200)
    pack_qty: int = Field(default=1, ge=1, le=100_000)
    label: str | None = Field(default=None, max_length=60)


class ComponentIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=10_000)


class ComponentsIn(BaseModel):
    components: list[ComponentIn] = Field(max_length=200)


class SubstituteIn(BaseModel):
    substitute_id: uuid.UUID
    note: str | None = Field(default=None, max_length=300)


def _sku_free(db: Session, ctx: OwnerContext, sku: str | None, product_id: uuid.UUID | None = None) -> str | None:
    sku = order_svc.clean(sku, 100)
    if sku and db.scalar(
        select(Product.id).where(
            Product.warehouse_id == ctx.warehouse.id,
            Product.active.is_(True),
            func.upper(Product.sku) == sku.upper(),
            Product.id != product_id if product_id else True,
        )
    ):
        raise conflict("sku_taken", f"Another product already uses SKU {sku}.")
    return sku


def _saved(db: Session, ctx: OwnerContext, p: Product, action: str) -> dict[str, Any]:
    catalog.relink(db, ctx.warehouse, p)
    audit.record(db, ctx.actor, action, warehouse_id=ctx.warehouse.id, target_type="product", target_id=p.id, sku=p.sku)
    db.commit()
    return catalog.product_dict(db, p, full=True, thumbs=catalog.thumb_data_urls(db, {p.image_id} - {None}))


@router.get("/products")
def list_products(
    q: str | None = Query(None, max_length=100),
    show: str = Query("active", description="active | kits | no_image | no_barcode | archived"),
    ids: str | None = Query(None, max_length=20_000, description="Comma-separated product ids"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Product).where(Product.warehouse_id == ctx.warehouse.id)
    if ids:
        try:
            wanted = [uuid.UUID(x) for x in ids.split(",") if x.strip()][:500]
        except ValueError:
            raise bad_request("ids_invalid", "ids must be product ids.") from None
        stmt = stmt.where(Product.id.in_(wanted))
    else:
        stmt = stmt.where(Product.active.is_(show != "archived"))
    if show == "kits":
        stmt = stmt.where(Product.id.in_(select(KitComponent.kit_id)))
    elif show == "no_image":
        stmt = stmt.where(Product.image_id.is_(None))
    elif show == "no_barcode":
        stmt = stmt.where(or_(Product.barcode.is_(None), Product.no_barcode.is_(True)))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(like),
                Product.sku.ilike(like),
                Product.barcode.ilike(like),
                Product.location.ilike(like),
                Product.id.in_(select(ProductBarcode.product_id).where(ProductBarcode.barcode.ilike(like))),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Product.name, Product.id).offset(offset).limit(limit)))
    thumbs = catalog.thumb_data_urls(db, {p.image_id for p in rows if p.image_id})
    kit_ids = (
        set(db.scalars(select(KitComponent.kit_id).where(KitComponent.kit_id.in_([p.id for p in rows]))))
        if rows
        else set()
    )
    packs: dict[uuid.UUID, int] = {}
    if rows:
        for pid, n in db.execute(
            select(ProductBarcode.product_id, func.max(ProductBarcode.pack_qty))
            .where(ProductBarcode.product_id.in_([p.id for p in rows]))
            .group_by(ProductBarcode.product_id)
        ):
            packs[pid] = int(n)
    out = []
    for p in rows:
        d = catalog.product_dict(db, p, thumbs=thumbs)
        d["is_kit"] = p.id in kit_ids
        d["max_pack"] = packs.get(p.id, 1)
        out.append(d)
    return {"products": out, "total": int(total), "offset": offset, "limit": limit}


@router.post("/products", status_code=201)
def create_product(
    body: ProductIn, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    sku = _sku_free(db, ctx, body.sku)
    barcode = order_svc.clean(body.barcode, 200)
    k = catalog.check_barcode_free(db, ctx.warehouse.id, barcode) if barcode else None
    if not sku and not barcode and not body.no_barcode:
        raise bad_request(
            "identifier_required", "Give the product a barcode or a SKU (or mark it as having no barcode)."
        )
    p = Product(
        warehouse_id=ctx.warehouse.id,
        name=body.name.strip(),
        sku=sku,
        barcode=barcode,
        normalized_barcode=k,
        location=order_svc.clean(body.location, 100),
        weight_grams=body.weight_grams,
        packer_note=order_svc.clean(body.packer_note, 500),
        no_barcode=body.no_barcode,
        track_lot=body.track_lot,
        track_serial=body.track_serial,
        track_expiry=body.track_expiry,
    )
    db.add(p)
    db.flush()
    usage.track(db, ctx.warehouse.id, "catalog.create")
    return _saved(db, ctx, p, "product.created")


@router.get("/products/template.csv", response_class=Response)
def products_template(ctx: OwnerContext = Depends(current_owner)) -> Response:
    return Response(
        PRODUCT_TEMPLATE,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="autorack-products-template.csv"'},
    )


@router.get("/products/{product_id}")
def get_product(
    product_id: uuid.UUID, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    return catalog.product_dict(db, p, full=True, thumbs=catalog.thumb_data_urls(db, {p.image_id} - {None}))


@router.patch("/products/{product_id}")
def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    changes = body.model_dump(exclude_unset=True)
    if "sku" in changes:
        p.sku = _sku_free(db, ctx, changes["sku"], p.id)
    if "barcode" in changes:
        code = order_svc.clean(changes["barcode"], 200)
        p.normalized_barcode = catalog.check_barcode_free(db, ctx.warehouse.id, code, product_id=p.id) if code else None
        p.barcode = code
    if changes.get("name"):
        p.name = changes["name"].strip()
    for f, limit in (("location", 100), ("packer_note", 500)):
        if f in changes:
            setattr(p, f, order_svc.clean(changes[f], limit))
    if "weight_grams" in changes:
        p.weight_grams = changes["weight_grams"]
    for f in ("no_barcode", "track_lot", "track_serial", "track_expiry"):
        if changes.get(f) is not None:
            setattr(p, f, bool(changes[f]))
    return _saved(db, ctx, p, "product.updated")


@router.delete("/products/{product_id}", status_code=204)
def archive_product(
    product_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> None:
    """Archived, not deleted: past orders still point at it."""
    p = catalog.get(db, ctx.warehouse.id, product_id)
    p.active = False
    catalog.relink(db, ctx.warehouse, p)
    audit.record(
        db, ctx.actor, "product.archived", warehouse_id=ctx.warehouse.id, target_type="product", target_id=p.id
    )
    db.commit()


@router.post("/products/{product_id}/restore")
def restore_product(
    product_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    _sku_free(db, ctx, p.sku, p.id)
    if p.barcode:
        catalog.check_barcode_free(db, ctx.warehouse.id, p.barcode, product_id=p.id)
    p.active = True
    return _saved(db, ctx, p, "product.restored")


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------


@router.post("/products/{product_id}/image")
async def upload_image(
    product_id: uuid.UUID,
    request: Request,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The picture as the request body (any common image type)."""
    p = catalog.get(db, ctx.warehouse.id, product_id)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > catalog.MAX_IMAGE_BYTES:
            raise bad_request("image_too_large", "Pictures are limited to 10 MB.")
    catalog.set_image(db, p, body)
    return _saved(db, ctx, p, "product.image_set")


@router.delete("/products/{product_id}/image", status_code=204)
def remove_image(
    product_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> None:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    p.image_id = None
    catalog.relink(db, ctx.warehouse, p)
    db.commit()


@router.get("/products/{product_id}/image", response_class=Response)
def get_image(
    product_id: uuid.UUID,
    size: str = Query("full", pattern="^(full|thumb)$"),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> Response:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    img = db.get(ProductImage, p.image_id) if p.image_id else None
    if not img or img.warehouse_id != ctx.warehouse.id:
        raise not_found("No picture")
    return Response(
        img.thumb if size == "thumb" else img.data,
        media_type=img.content_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# Extra barcodes and case packs
# ---------------------------------------------------------------------------


@router.post("/products/{product_id}/barcodes", status_code=201)
def add_barcode(
    product_id: uuid.UUID, body: BarcodeIn, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    code = order_svc.clean(body.barcode, 200) or ""
    k = catalog.check_barcode_free(db, ctx.warehouse.id, code)
    db.add(
        ProductBarcode(
            warehouse_id=ctx.warehouse.id,
            product_id=p.id,
            barcode=code,
            normalized_barcode=k,
            pack_qty=body.pack_qty,
            label=order_svc.clean(body.label, 60) or (f"Case of {body.pack_qty}" if body.pack_qty > 1 else None),
        )
    )
    db.flush()
    return _saved(db, ctx, p, "product.barcode_added")


@router.delete("/products/{product_id}/barcodes/{barcode_id}")
def remove_barcode(
    product_id: uuid.UUID,
    barcode_id: uuid.UUID,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    b = db.scalar(select(ProductBarcode).where(ProductBarcode.id == barcode_id, ProductBarcode.product_id == p.id))
    if not b:
        raise not_found("Barcode not found")
    db.delete(b)
    db.flush()
    return _saved(db, ctx, p, "product.barcode_removed")


@router.post("/products/{product_id}/assign-barcode")
def assign_barcode(
    product_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """For an item with no barcode of its own: use its SKU if that's free,
    else a new code. Print it on labels from the Products page."""
    p = catalog.get(db, ctx.warehouse.id, product_id)
    if p.barcode:
        raise conflict("has_barcode", "This product already has a barcode.")
    candidates = [p.sku] if p.sku and re.fullmatch(r"[\x20-\x7e]{3,40}", p.sku) else []
    candidates += [f"AR{secrets.randbelow(10**10):010d}" for _ in range(5)]
    for code in candidates:
        try:
            k = catalog.check_barcode_free(db, ctx.warehouse.id, code)
        except ApiError:
            continue
        p.barcode, p.normalized_barcode, p.no_barcode = code, k, False
        return _saved(db, ctx, p, "product.barcode_assigned")
    raise conflict("no_code", "Couldn't find a free barcode. Try again.")


# ---------------------------------------------------------------------------
# Kits and substitutes
# ---------------------------------------------------------------------------


@router.put("/products/{product_id}/components")
def set_components(
    product_id: uuid.UUID,
    body: ComponentsIn,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Make this product a kit of these parts (an empty list: not a kit)."""
    p = catalog.get(db, ctx.warehouse.id, product_id)
    wanted: dict[uuid.UUID, int] = {}
    for c in body.components:
        if c.product_id == p.id:
            raise bad_request("kit_self", "A kit can't contain itself.")
        comp = catalog.get(db, ctx.warehouse.id, c.product_id)
        if db.scalar(select(KitComponent.id).where(KitComponent.kit_id == comp.id)):
            raise bad_request("kit_nested", f"{comp.name} is itself a kit. Add its parts instead.")
        wanted[comp.id] = wanted.get(comp.id, 0) + c.quantity
    if wanted and db.scalar(select(KitComponent.id).where(KitComponent.component_id == p.id)):
        raise bad_request("kit_nested", "This product is a part of another kit, so it can't be a kit itself.")
    for kc in db.scalars(select(KitComponent).where(KitComponent.kit_id == p.id)):
        db.delete(kc)
    db.flush()
    for cid, qty in wanted.items():
        db.add(KitComponent(warehouse_id=ctx.warehouse.id, kit_id=p.id, component_id=cid, quantity=qty))
    db.flush()
    return _saved(db, ctx, p, "product.kit_set")


@router.post("/products/{product_id}/substitutes", status_code=201)
def add_substitute(
    product_id: uuid.UUID,
    body: SubstituteIn,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    sub = catalog.get(db, ctx.warehouse.id, body.substitute_id)
    if sub.id == p.id:
        raise bad_request("substitute_self", "A product can't substitute for itself.")
    if not sub.barcode:
        raise bad_request("substitute_no_barcode", f"{sub.name} has no barcode, so it couldn't be scanned in.")
    if db.scalar(
        select(ProductSubstitute.id).where(
            ProductSubstitute.product_id == p.id, ProductSubstitute.substitute_id == sub.id
        )
    ):
        raise conflict("substitute_exists", "That substitute is already approved.")
    db.add(
        ProductSubstitute(
            warehouse_id=ctx.warehouse.id,
            product_id=p.id,
            substitute_id=sub.id,
            note=order_svc.clean(body.note, 300),
            created_by_user_id=ctx.user.id,
        )
    )
    db.flush()
    return _saved(db, ctx, p, "product.substitute_approved")


@router.delete("/products/{product_id}/substitutes/{substitute_id}")
def remove_substitute(
    product_id: uuid.UUID,
    substitute_id: uuid.UUID,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = catalog.get(db, ctx.warehouse.id, product_id)
    s = db.scalar(
        select(ProductSubstitute).where(
            ProductSubstitute.product_id == p.id, ProductSubstitute.substitute_id == substitute_id
        )
    )
    if not s:
        raise not_found("Substitute not found")
    db.delete(s)
    db.flush()
    return _saved(db, ctx, p, "product.substitute_removed")


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

PRODUCT_COLUMNS: dict[str, list[str]] = {
    "sku": ["sku", "item", "item number", "item code", "product code", "part number", "variant sku"],
    "barcode": ["barcode", "upc", "ean", "gtin", "variant barcode", "item barcode"],
    "name": ["name", "title", "description", "product", "product name", "item name"],
    "location": ["location", "bin", "bin location", "slot", "shelf"],
    "weight_grams": ["weight grams", "weight g", "grams", "variant grams"],
    "weight_oz": ["weight oz", "ounces"],
    "weight_lb": ["weight lb", "weight lbs", "pounds", "weight"],
    "case_barcode": ["case barcode", "case upc", "carton barcode", "inner barcode", "case gtin"],
    "case_qty": ["case qty", "case quantity", "units per case", "pack qty", "case pack"],
    "packer_note": ["note", "packer note", "packing note", "notes"],
    "track": ["track", "capture", "trace"],
}


def _import_case(db: Session, ctx: OwnerContext, p: Product, case_code: str, case_qty: str) -> None:
    if not case_code:
        return
    k = catalog.key(case_code)
    qty = int(float(case_qty)) if case_qty else 1
    if not 1 <= qty <= 100_000:
        raise ValueError("case quantity must be 1 to 100000")
    existing = db.scalar(
        select(ProductBarcode).where(
            ProductBarcode.warehouse_id == ctx.warehouse.id, ProductBarcode.normalized_barcode == k
        )
    )
    if existing and existing.product_id != p.id:
        raise ValueError(f"case barcode {case_code} belongs to another product")
    if existing:
        existing.pack_qty = qty
    else:
        catalog.check_barcode_free(db, ctx.warehouse.id, case_code, product_id=p.id)
        db.add(
            ProductBarcode(
                warehouse_id=ctx.warehouse.id,
                product_id=p.id,
                barcode=case_code[:200],
                normalized_barcode=k or "",
                pack_qty=qty,
                label=f"Case of {qty}" if qty > 1 else None,
            )
        )
    db.flush()


PRODUCT_TEMPLATE = (
    "sku,barcode,name,location,weight_grams,case_barcode,case_qty,packer_note,track\r\n"
    "WID-BLU-12,012345678905,Blue widget (12 pk),A-01-03,450,10012345678902,12,,\r\n"
    "SAL-500,09501101530003,Saline 500 ml,C-02-01,520,,,Keep upright,lot+expiry\r\n"
)


def _col_map(headers: list[str]) -> dict[str, str]:
    norm = {re.sub(r"[^a-z0-9]+", " ", h.lower()).strip(): h for h in headers if h}
    out: dict[str, str] = {}
    for canon, syns in PRODUCT_COLUMNS.items():
        for cand in [canon.replace("_", " "), *syns]:
            if cand in norm and norm[cand] not in out.values():
                out[canon] = norm[cand]
                break
    return out


def _grams(row: dict[str, str], cols: dict[str, str]) -> int | None:
    def num(c: str) -> float | None:
        v = (row.get(cols.get(c, ""), "") or "").strip().replace(",", "")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    for c, factor in (("weight_grams", 1.0), ("weight_oz", 28.3495), ("weight_lb", 453.592)):
        v = num(c)
        if v is not None and v >= 0:
            return round(v * factor)
    return None


@router.post("/products/import")
async def import_products(
    request: Request,
    file: UploadFile = File(...),
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Add or update products from a CSV (matched by SKU, else barcode)."""
    data = await file.read(get_settings().max_import_bytes + 1)
    if len(data) > get_settings().max_import_bytes:
        raise bad_request("file_too_large", "That file is too large.")
    text = csv_import.decode(data)
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = _col_map(list(reader.fieldnames or []))
    if "name" not in cols or not ({"sku", "barcode"} & cols.keys()):
        raise bad_request(
            "columns_missing",
            "Couldn't find the columns. Expected at least: name, and sku or barcode.",
            headers=reader.fieldnames or [],
        )
    created = updated = 0
    errors: list[dict[str, Any]] = []
    touched: list[Product] = []
    for row_no, row in enumerate(reader, start=2):
        if row_no > 20_001:
            errors.append({"row": row_no, "message": "Stopped at 20,000 rows."})
            break

        def cell(c: str, row: dict[str, str] = row) -> str:
            return (row.get(cols.get(c, ""), "") or "").strip()

        name, sku, barcode = cell("name"), cell("sku")[:100] or None, cell("barcode")[:200] or None
        if not name or not (sku or barcode):
            if any((v or "").strip() for v in row.values() if isinstance(v, str)):
                errors.append({"row": row_no, "message": "Needs a name and a SKU or barcode."})
            continue
        p = None
        if sku:
            p = db.scalar(
                select(Product).where(
                    Product.warehouse_id == ctx.warehouse.id,
                    Product.active.is_(True),
                    func.upper(Product.sku) == sku.upper(),
                )
            )
        if p is None and barcode:
            p = db.scalar(
                select(Product).where(
                    Product.warehouse_id == ctx.warehouse.id,
                    Product.active.is_(True),
                    Product.normalized_barcode == catalog.key(barcode),
                )
            )
            if p is not None and sku and p.sku and p.sku.upper() != sku.upper():
                errors.append(
                    {"row": row_no, "message": f"{sku}: barcode {barcode} already belongs to {p.name} (SKU {p.sku})."}
                )
                continue
        is_new = p is None
        try:
            with db.begin_nested():
                if p is None:
                    p = Product(warehouse_id=ctx.warehouse.id, name=name[:500], source="csv")
                    db.add(p)
                p.name = name[:500]
                if sku:
                    p.sku = sku
                if barcode and catalog.key(barcode) != p.normalized_barcode:
                    db.flush()
                    p.normalized_barcode = catalog.check_barcode_free(db, ctx.warehouse.id, barcode, product_id=p.id)
                    p.barcode = barcode
                for c in ("location", "packer_note"):
                    if cell(c):
                        setattr(p, c, cell(c)[: 100 if c == "location" else 500])
                grams = _grams(row, cols)
                if grams is not None:
                    p.weight_grams = grams
                if cell("track"):
                    for f, v in order_svc.parse_track(cell("track")).items():
                        setattr(p, f, v)
                db.flush()
                _import_case(db, ctx, p, cell("case_barcode"), cell("case_qty"))
        except (ValueError, ApiError) as exc:
            detail = exc.detail if isinstance(exc, ApiError) and isinstance(exc.detail, dict) else {}
            errors.append({"row": row_no, "message": f"{sku or barcode}: {detail.get('message') or exc}"})
            continue
        touched.append(p)
        created += is_new
        updated += not is_new
    for p in touched:
        catalog.relink(db, ctx.warehouse, p)
    audit.record(
        db,
        ctx.actor,
        "products.imported",
        warehouse_id=ctx.warehouse.id,
        created=created,
        updated=updated,
        errors=len(errors),
    )
    usage.track(db, ctx.warehouse.id, "catalog.import")
    db.commit()
    return {"created": created, "updated": updated, "errors": errors[:200], "error_count": len(errors)}
