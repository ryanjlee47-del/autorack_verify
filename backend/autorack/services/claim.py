"""The claim pack: one PDF with everything that shows what went into a parcel.

For a carrier claim ("it arrived damaged / empty"), a marketplace dispute
("item not as described"), or a customer who says something was missing.
It holds the order, what was verified by scan (when, which barcode, lot and
serial), the boxes and their tracking numbers, the photos of the packed boxes
and the inserts. Built only from the append-only scan log.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Order, OrderFlag, Photo, ScanEvent, ScanResult, Warehouse, utcnow
from . import dashboard as dash
from . import floor
from . import orders as order_svc
from .monthly import MUTED, NAVY, RULE, TIMES, S, _table

MAX_PHOTOS = 8
DASH = "\u2013"


def _fmt(dt: Any, tz: Any) -> str:
    return dt.astimezone(tz).strftime("%b %-d, %Y %-I:%M %p") if dt else DASH


def _p(text: Any, style: str = "body") -> Paragraph:
    return Paragraph(escape(str(text)), S[style])


def _photo(data: bytes, max_w: float, max_h: float) -> Image | None:
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        return None
    scale = min(max_w / w, max_h / h)
    return Image(io.BytesIO(data), width=w * scale, height=h * scale)


def build(db: Session, wh: Warehouse, order: Order, *, internal: bool) -> bytes:
    """`internal`: the warehouse's own copy, with who picked what and the
    problem reports. A client's copy leaves both out."""
    tz = dash.tz_of(wh)
    lines = order_svc.lines_for(db, order.id)
    by_line = {li.id: li for li in lines}
    workers = dash.worker_names(db, wh.id) if internal else {}
    voided = select(ScanEvent.voids_scan_id).where(ScanEvent.order_id == order.id, ScanEvent.voids_scan_id.is_not(None))
    picks = list(
        db.scalars(
            select(ScanEvent)
            .where(ScanEvent.order_id == order.id, ScanEvent.result == ScanResult.match, ScanEvent.id.not_in(voided))
            .order_by(ScanEvent.client_scanned_at, ScanEvent.client_seq)
        )
    )
    caught = (
        db.scalar(
            select(func.count())
            .select_from(ScanEvent)
            .where(ScanEvent.order_id == order.id, ScanEvent.result.in_(dash.ERROR_RESULTS))
        )
        or 0
    )
    boxes = order_svc.package_dicts(db, order, workers or None)
    photos = list(
        db.scalars(
            select(Photo)
            .where(Photo.order_id == order.id, Photo.kind == "pack")
            .order_by(Photo.created_at)
            .limit(MAX_PHOTOS)
        )
    )
    done = floor.insert_checks(db, order.id)
    inserts = [(i.name, i.id in done) for i in floor.inserts_for(db, order, lines)]
    number = order.external_order_number or str(order.id)[:8]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.95 * inch,
        bottomMargin=0.75 * inch,
        title=f"Shipment evidence, order {number}",
        author="Autorack",
    )
    width = letter[0] - 1.2 * inch
    story: list[Any] = [
        Paragraph(f"Shipment evidence: order {escape(number)}", S["h1"]),
        Spacer(1, 4),
        _p(
            "Every item below was scanned and matched to the order before the box was sealed. "
            "Records come from Autorack's scan log, which can't be edited after the fact.",
            "muted",
        ),
        Spacer(1, 10),
    ]
    units = sum(p.quantity for p in picks)
    facts = [
        ["Shipped by", wh.name],
        ["Customer", order.customer or DASH],
        ["Order created", _fmt(order.created_at, tz)],
        ["Picking finished", _fmt(order.completed_at, tz)],
        ["Label scanned (shipped)", _fmt(order.shipped_at, tz)],
        ["Units verified by scan", f"{units} of {sum(li.expected_quantity for li in lines)}"],
        ["Wrong items caught and put back", str(caught)],
    ]
    if len(boxes) <= 1 and order.tracking_number:
        facts.insert(5, ["Tracking", f"{order.carrier or ''} {order.tracking_number}".strip()])
    t = Table([[_p(k, "muted"), _p(v)] for k, v in facts], colWidths=[2.2 * inch, width - 2.2 * inch])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 8)]

    if len(boxes) > 1:
        story.append(Paragraph(f"Shipped in {len(boxes)} boxes", S["h2"]))
        story.append(
            _table(
                ["Box", "Tracking", "Label scanned"],
                [
                    [
                        str(b["box"]),
                        f"{b['carrier'] or ''} {b['tracking_number']}".strip(),
                        b["at"][:16].replace("T", " "),
                    ]
                    for b in boxes
                ],
                [0.7 * inch, 3.6 * inch, width - 4.3 * inch],
            )
        )

    story.append(Paragraph("What was ordered", S["h2"]))
    story.append(
        _table(
            ["Item", "SKU", "Ordered", "Verified", "Short"],
            [
                [
                    escape(li.sku_description or li.expected_barcode),
                    escape(li.sku or ""),
                    str(li.expected_quantity),
                    str(min(li.scanned_quantity, li.expected_quantity)),
                    str(li.short_quantity or ""),
                ]
                for li in lines
            ],
            [width - 4.4 * inch, 1.4 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch],
        )
    )

    story.append(Paragraph("Every unit, as scanned", S["h2"]))
    header = ["Time", "Item", "Barcode scanned", "Lot / serial / expiry"] + (["Picked by"] if internal else [])
    rows = []
    for s in picks:
        li = by_line.get(s.line_item_id) if s.line_item_id else None
        item = (li.sku_description or li.sku or li.expected_barcode) if li else ""
        if s.quantity > 1:
            item += f" {TIMES} {s.quantity}"
        if s.substitution:
            item += " (approved substitute)"
        trace = " · ".join(
            x for x in (s.lot and f"Lot {s.lot}", s.serial and f"S/N {s.serial}", s.expiry and f"Exp {s.expiry}") if x
        )
        row = [
            _fmt(s.client_scanned_at, tz),
            escape(item),
            "No barcode: checked by hand" if s.confirmed else escape(s.scanned_barcode),
            escape(trace),
        ]
        if internal:
            row.append(escape(workers.get(s.worker_id, "")))
        rows.append(row)
    widths = [1.45 * inch, 2.2 * inch, 1.6 * inch, 1.2 * inch] + ([width - 6.45 * inch] if internal else [])
    if not internal:
        widths[1] += width - sum(widths)
    empty = [[DASH, "Nothing verified by scan", "", ""] + ([""] if internal else [])]
    story.append(_table(header, rows or empty, widths))

    if inserts:
        story.append(Paragraph("Inserts", S["h2"]))
        story.append(
            _table(
                ["Insert", "In the box"],
                [[escape(n), "Yes" if ok else "Not recorded"] for n, ok in inserts],
                [width - 1.6 * inch, 1.6 * inch],
            )
        )

    if photos:
        story.append(Paragraph("The packed box" + ("es" if len(boxes) > 1 else ""), S["h2"]))
        cells = []
        for ph in photos:
            img = _photo(ph.data, width / 2 - 8, 3.2 * inch)
            if img is None:
                continue
            cap = f"{'Box ' + str(ph.box) + ' · ' if len(boxes) > 1 and ph.box else ''}{_fmt(ph.created_at, tz)}"
            cells.append([img, _p(cap, "muted")])
        grid = []
        for i in range(0, len(cells), 2):
            pair = cells[i : i + 2]
            grid.append([c[0] for c in pair] + ([""] if len(pair) == 1 else []))
            grid.append([c[1] for c in pair] + ([""] if len(pair) == 1 else []))
        if grid:
            gt = Table(grid, colWidths=[width / 2, width / 2])
            gt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            story.append(KeepTogether(gt))

    if internal:
        flags = list(db.scalars(select(OrderFlag).where(OrderFlag.order_id == order.id).order_by(OrderFlag.created_at)))
        if flags:
            story.append(Paragraph("Problems reported (internal)", S["h2"]))
            for f in flags:
                who = workers.get(f.worker_id, "") if f.worker_id else ""
                text = f"{f.reason.value.replace('_', ' ')}{f' ({f.short_quantity} short)' if f.short_quantity else ''}"
                text += f" · {who} · {_fmt(f.created_at, tz)}"
                if f.resolved_at:
                    text += f" · resolved {_fmt(f.resolved_at, tz)}"
                story.append(_p(text))

    generated = utcnow()

    def frame(c: Any, d: Any) -> None:
        w, h = letter
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - 0.62 * inch, w, 0.62 * inch, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(0.6 * inch, h - 0.4 * inch, "AUTORACK")
        c.setFont("Helvetica", 9)
        c.drawRightString(w - 0.6 * inch, h - 0.36 * inch, f"Shipment evidence · order {number}"[:80])
        c.drawRightString(w - 0.6 * inch, h - 0.5 * inch, wh.name[:70])
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        c.drawString(
            0.6 * inch, 0.45 * inch, f"Generated {_fmt(generated, tz)} from the Autorack scan log. Order id {order.id}."
        )
        c.drawRightString(w - 0.6 * inch, 0.45 * inch, f"Page {d.page}")
        c.restoreState()

    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return buf.getvalue()


def filename(order: Order) -> str:
    number = "".join(ch for ch in (order.external_order_number or str(order.id)[:8]) if ch.isalnum() or ch in "-_")
    return f"autorack-evidence-{number or 'order'}.pdf"
