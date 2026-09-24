"""CSV pick-list import: the primary way orders enter Autorack.

Canonical columns are `order_number, barcode, quantity, description`, plus
optional `sku` and `location`. Real exports never use exactly those names, so
headers are matched against common synonyms ("Order #", "UPC", "Qty", "Bin"...),
the delimiter is sniffed (comma, tab, semicolon), and both UTF-8 (with or
without Excel's BOM) and Windows-1252 decode.

Imports are two-step: `preview` parses and reports everything that would
happen without writing; `commit` does it. Both run the same parser, so what
the owner approved is what gets written.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import matching
from ..config import get_settings
from ..errors import bad_request
from ..models import ImportBatch, Order, OrderSource, OrderStatus, Warehouse
from . import audit
from . import orders as order_svc
from .audit import Actor

COLUMN_SYNONYMS: dict[str, list[str]] = {
    "order_number": [
        "order number",
        "order",
        "order no",
        "order num",
        "order id",
        "ordernumber",
        "order ref",
        "order reference",
        "so",
        "so number",
        "sales order",
        "pick ticket",
        "pick list",
        "ticket",
        "shipment",
        "shipment id",
        "reference",
        "po",
        "po number",
    ],
    "barcode": [
        "barcode",
        "bar code",
        "upc",
        "upc code",
        "ean",
        "gtin",
        "item barcode",
        "product barcode",
        "scan code",
        "code",
        "expected barcode",
    ],
    "quantity": ["quantity", "qty", "units", "qty ordered", "quantity ordered", "order qty", "pick qty", "count"],
    "description": [
        "description",
        "desc",
        "item description",
        "product description",
        "product",
        "product name",
        "item name",
        "name",
        "title",
    ],
    "sku": ["sku", "item", "item number", "item no", "item code", "part", "part number", "part no", "product code"],
    "location": ["location", "bin", "bin location", "loc", "slot", "shelf", "aisle", "pick location"],
}
REQUIRED = ("order_number", "barcode")

TEMPLATE_CSV = (
    "order_number,barcode,quantity,description,sku,location\r\n"
    "SO-1001,012345678905,2,Blue widget (12 pk),WID-BLU-12,A-01-03\r\n"
    "SO-1001,036000291452,1,Packing tape,TAPE-48,B-04-01\r\n"
    "SO-1002,012345678905,1,Blue widget (12 pk),WID-BLU-12,A-01-03\r\n"
)


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", h.lower()).strip()


@dataclass
class ParsedOrder:
    number: str
    lines: list[order_svc.LineInput] = field(default_factory=list)
    first_row: int = 0
    too_many_lines: bool = False


@dataclass
class ParseResult:
    columns: dict[str, str]  # canonical -> header as it appeared
    orders: list[ParsedOrder]
    errors: list[dict[str, Any]]
    warnings: list[str]
    row_count: int


def decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def map_columns(headers: Sequence[str]) -> dict[str, str]:
    normalized = {_norm_header(h): h for h in headers if h is not None}
    mapping: dict[str, str] = {}
    for canonical, synonyms in COLUMN_SYNONYMS.items():
        for candidate in [canonical.replace("_", " "), *synonyms]:
            if candidate in normalized and normalized[candidate] not in mapping.values():
                mapping[canonical] = normalized[candidate]
                break
    return mapping


def parse(content: bytes) -> ParseResult:
    s = get_settings()
    if len(content) > s.max_import_bytes:
        raise bad_request("file_too_large", f"CSV files are limited to {s.max_import_bytes // (1024 * 1024)} MB.")
    text = decode(content)
    if not text.strip():
        raise bad_request("file_empty", "That file is empty.")
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    columns = map_columns(headers)
    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        raise bad_request(
            "columns_missing",
            "Couldn't find the "
            + " and ".join(m.replace("_", " ") for m in missing)
            + " column. Expected columns: order_number, barcode, quantity, description.",
            headers=headers,
        )

    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    orders: dict[str, ParsedOrder] = {}
    defaulted_qty = 0
    row_count = 0

    def cell(row: dict[str, Any], canonical: str) -> str:
        header = columns.get(canonical)
        v = row.get(header) if header else None
        return str(v).strip() if v is not None else ""

    for row_no, row in enumerate(reader, start=2):  # row 1 is the header
        row_count += 1
        if row_count > s.max_import_rows:
            raise bad_request("too_many_rows", f"Imports are limited to {s.max_import_rows:,} rows per file.")
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        number = cell(row, "order_number")
        barcode = cell(row, "barcode")
        qty_raw = cell(row, "quantity")
        if not number:
            errors.append({"row": row_no, "message": "Missing order number."})
            continue
        if len(number) > 100:
            errors.append({"row": row_no, "message": "Order number is longer than 100 characters."})
            continue
        if not barcode or not matching.normalized_key(barcode):
            errors.append({"row": row_no, "message": f"Order {number}: missing barcode."})
            continue
        if len(barcode) > 200:
            errors.append({"row": row_no, "message": f"Order {number}: barcode longer than 200 characters."})
            continue
        # Excel turns long numeric barcodes into 1.23457E+11; that is data loss
        # we cannot undo, so refuse rather than import a barcode nobody has.
        if re.fullmatch(r"\d(\.\d+)?[eE]\+\d+", barcode):
            errors.append(
                {
                    "row": row_no,
                    "message": f"Order {number}: barcode '{barcode}' was converted to scientific notation "
                    "by a spreadsheet. Format the column as Text and re-export.",
                }
            )
            continue
        if qty_raw:
            try:
                qty_f = float(qty_raw.replace(",", ""))
            except ValueError:
                errors.append({"row": row_no, "message": f"Order {number}: quantity '{qty_raw}' is not a number."})
                continue
            if qty_f != int(qty_f) or qty_f < 1 or qty_f > 100_000:
                errors.append(
                    {"row": row_no, "message": f"Order {number}: quantity must be a whole number, 1 to 100000."}
                )
                continue
            qty = int(qty_f)
        else:
            qty = 1
            defaulted_qty += 1
        po = orders.setdefault(number, ParsedOrder(number=number, first_row=row_no))
        po.lines.append(
            order_svc.LineInput(
                barcode=barcode,
                quantity=qty,
                sku=cell(row, "sku") or None,
                description=cell(row, "description") or None,
                location=cell(row, "location") or None,
            )
        )

    merged_lines = 0
    for po in orders.values():
        before = len(po.lines)
        po.lines = order_svc.merge_line_inputs(po.lines)
        merged_lines += before - len(po.lines)
        if len(po.lines) > order_svc.MAX_LINES_PER_ORDER:
            po.too_many_lines = True
            errors.append(
                {
                    "row": po.first_row,
                    "message": f"Order {po.number} has {len(po.lines)} lines; "
                    f"the limit is {order_svc.MAX_LINES_PER_ORDER}.",
                }
            )
        groups = matching.equivalent_line_groups([(i, li.barcode) for i, li in enumerate(po.lines)])
        for g in groups:
            codes = ", ".join(po.lines[int(str(i))].barcode for i in g)
            warnings.append(
                f"Order {po.number}: barcodes {codes} are the same product in different formats. "
                "Scans of it will need review. Consider merging them into one line."
            )
    if defaulted_qty:
        warnings.append(f"{defaulted_qty} row(s) had no quantity; assumed 1.")
    if merged_lines:
        warnings.append(f"{merged_lines} duplicate barcode row(s) were merged into one line with the total quantity.")
    if "quantity" not in columns:
        warnings.append("No quantity column found; every line assumes a quantity of 1.")

    return ParseResult(
        columns=columns, orders=list(orders.values()), errors=errors, warnings=warnings, row_count=row_count
    )


def existing_numbers(db: Session, warehouse_id: uuid.UUID, numbers: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(numbers), 1000):
        chunk = numbers[i : i + 1000]
        found.update(
            n
            for n in db.scalars(
                select(Order.external_order_number).where(
                    Order.warehouse_id == warehouse_id,
                    Order.external_order_number.in_(chunk),
                    Order.status != OrderStatus.cancelled,
                )
            )
            if n
        )
    return found


def preview(db: Session, wh: Warehouse, content: bytes) -> dict[str, Any]:
    pr = parse(content)
    existing = existing_numbers(db, wh.id, [o.number for o in pr.orders])
    new_orders = [o for o in pr.orders if o.number not in existing and not o.too_many_lines]
    warnings = list(pr.warnings)
    if existing:
        sample = ", ".join(sorted(existing)[:5])
        warnings.insert(
            0,
            f"{len(existing)} order(s) already exist and will be skipped ({sample}{'…' if len(existing) > 5 else ''}).",
        )
    return {
        "columns": pr.columns,
        "row_count": pr.row_count,
        "orders_found": len(pr.orders),
        "orders_new": len(new_orders),
        "orders_existing": sorted(existing),
        "lines": sum(len(o.lines) for o in new_orders),
        "units": sum(li.quantity for o in new_orders for li in o.lines),
        "errors": pr.errors[:200],
        "error_count": len(pr.errors),
        "warnings": warnings,
        "sample": [
            {
                "order_number": o.number,
                "lines": [
                    {
                        "barcode": li.barcode,
                        "quantity": li.quantity,
                        "description": li.description,
                        "sku": li.sku,
                        "location": li.location,
                    }
                    for li in o.lines[:10]
                ],
                "line_count": len(o.lines),
            }
            for o in new_orders[:10]
        ],
    }


def commit(
    db: Session,
    wh: Warehouse,
    content: bytes,
    filename: str | None,
    actor: Actor,
    user_id: uuid.UUID | None,
    skip_invalid_rows: bool = False,
) -> ImportBatch:
    pr = parse(content)
    if pr.errors and not skip_invalid_rows:
        raise bad_request(
            "import_has_errors",
            f"The file has {len(pr.errors)} problem row(s). Fix them, or import with 'skip invalid rows'.",
            errors=pr.errors[:200],
        )
    existing = existing_numbers(db, wh.id, [o.number for o in pr.orders])
    batch = ImportBatch(
        warehouse_id=wh.id,
        filename=(filename or "")[:255] or None,
        uploaded_by_user_id=user_id,
        warnings=pr.warnings,
    )
    db.add(batch)
    db.flush()
    created = lines = 0
    for po in pr.orders:
        if po.number in existing or po.too_many_lines:
            continue
        order_svc.create_order(
            db,
            wh,
            external_order_number=po.number,
            lines=po.lines,
            source=OrderSource.csv,
            import_batch_id=batch.id,
            created_by_user_id=user_id,
        )
        created += 1
        lines += len(po.lines)
    batch.orders_created = created
    batch.lines_created = lines
    batch.orders_skipped = len(existing)
    audit.record(
        db,
        actor,
        "orders.imported",
        warehouse_id=wh.id,
        target_type="import_batch",
        target_id=batch.id,
        filename=batch.filename,
        orders=created,
        lines=lines,
        skipped=len(existing),
        invalid_rows=len(pr.errors),
    )
    db.commit()
    return batch
