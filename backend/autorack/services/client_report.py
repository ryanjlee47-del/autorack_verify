"""The accuracy report a 3PL hands its client: one month, under the 3PL's
own logo and name. "We shipped 1,240 of your orders; every unit was checked
by barcode; 18 wrong items were caught before they went out."

A 3PL wins and keeps clients by proving accuracy. This is that proof, made
from the scan log, downloadable by both sides and emailed to the client's
portal logins on the 1st.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import KeepTogether, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Client,
    Membership,
    NotificationSent,
    Order,
    OrderKind,
    OrderLineItem,
    OrderStatus,
    Photo,
    ScanEvent,
    User,
    UserRole,
    Warehouse,
    WarehouseLogo,
)
from . import client_billing
from . import dashboard as dash
from . import orders as order_svc
from .dashboard import tz_of
from .monthly import BLUE, MUTED, NAVY, RULE, _pct, _table, _text, previous_month
from .monthly import _delta as delta

log = logging.getLogger("autorack.client_report")
GREEN = colors.HexColor("#17803d")
OPEN = [OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------


def clean_logo(content: bytes) -> bytes:
    """A logo upload, as a small PNG (transparency kept, metadata dropped)."""
    from ..errors import bad_request

    try:
        img = Image.open(io.BytesIO(content), formats=["PNG", "JPEG", "WEBP"])
        img.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise bad_request("logo_invalid", "Upload a PNG, JPEG or WebP logo.") from exc
    img = img.convert("RGBA")
    img.thumbnail((800, 300))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def logo_of(db: Session, warehouse_id: Any) -> WarehouseLogo | None:
    return db.get(WarehouseLogo, warehouse_id)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


def numbers(db: Session, wh: Warehouse, client: Client, year: int, month: int) -> dict[str, Any]:
    """This client's month: what shipped, how accurately, how fast, what came back."""
    start, end = client_billing.period(wh, year, month)
    counts = client_billing.usage(db, wh, client.id, start, end)
    out_the_door = func.coalesce(Order.shipped_at, Order.completed_at)
    shipped = list(
        db.scalars(
            select(Order)
            .where(
                Order.warehouse_id == wh.id,
                Order.client_id == client.id,
                Order.kind == OrderKind.pick,
                Order.status.in_([OrderStatus.shipped, OrderStatus.completed]),
                out_the_door >= start,
                out_the_door < end,
            )
            .order_by(out_the_door)
        )
    )
    ids = [o.id for o in shipped]
    late = 0
    for o in shipped:
        due = order_svc.due_at(o, wh)
        when = o.shipped_at or o.completed_at
        late += bool(due and when and when > due)
    caught = (
        db.scalar(
            select(func.count())
            .select_from(ScanEvent)
            .where(ScanEvent.order_id.in_(ids), ScanEvent.result.in_(dash.ERROR_RESULTS))
        )
        if ids
        else 0
    ) or 0
    short = (
        db.scalar(select(func.sum(OrderLineItem.short_quantity)).where(OrderLineItem.order_id.in_(ids))) if ids else 0
    ) or 0
    with_photo = (
        db.scalar(
            select(func.count(func.distinct(Photo.order_id))).where(Photo.order_id.in_(ids), Photo.kind == "pack")
        )
        if ids
        else 0
    ) or 0
    units = counts["per_unit"]
    return {
        "month": f"{year:04d}-{month:02d}",
        "label": datetime(year, month, 1).strftime("%B %Y"),
        "client": client.name,
        "warehouse": wh.name,
        "orders_shipped": counts["per_order"],
        "units_shipped": units,
        "on_time": len(shipped) - late,
        "late": late,
        "wrong_items_caught": int(caught),
        # Right units against right units plus the wrong ones caught: how
        # often a picker's first grab was right. Every unit was verified.
        "accuracy": units / (units + int(caught)) if units + caught else None,
        "units_short": int(short),
        "returns": counts["per_return"],
        "units_received": counts["per_receive_unit"],
        "orders_with_box_photo": int(with_photo),
        "open_orders": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.warehouse_id == wh.id,
                Order.client_id == client.id,
                Order.kind == OrderKind.pick,
                Order.status.in_(OPEN),
            )
        )
        or 0,
        "recent": [
            {
                "number": o.external_order_number or str(o.id)[:8],
                "shipped_at": (o.shipped_at or o.completed_at).astimezone(tz_of(wh)).strftime("%b %-d")
                if (o.shipped_at or o.completed_at)
                else "",
                "tracking": o.tracking_number or "",
            }
            for o in shipped[-12:]
        ],
    }


def with_previous(db: Session, wh: Warehouse, client: Client, year: int, month: int) -> dict[str, Any]:
    cur = numbers(db, wh, client, year, month)
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    prev = numbers(db, wh, client, py, pm)
    cur["previous"] = {k: prev[k] for k in ("orders_shipped", "units_shipped", "wrong_items_caught", "accuracy")}
    return cur


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _header(d: dict[str, Any], logo: bytes | None) -> Any:
    def draw(c: Any, doc: Any) -> None:
        w, h = doc.pagesize
        c.saveState()
        top = h - 0.45 * inch
        if logo:
            img = ImageReader(io.BytesIO(logo))
            iw, ih = img.getSize()
            scale = min(2.2 * inch / iw, 0.6 * inch / ih)
            c.drawImage(img, 0.6 * inch, top - ih * scale, iw * scale, ih * scale, mask="auto")
        else:
            c.setFillColor(NAVY)
            c.setFont("Helvetica-Bold", 15)
            c.drawString(0.6 * inch, top - 0.3 * inch, d["warehouse"][:60])
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9)
        c.drawRightString(w - 0.6 * inch, top - 0.12 * inch, f"Order accuracy report · {d['label']}")
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(w - 0.6 * inch, top - 0.3 * inch, f"Prepared for {d['client'][:60]}")
        c.setStrokeColor(RULE)
        c.line(0.6 * inch, top - 0.75 * inch, w - 0.6 * inch, top - 0.75 * inch)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        c.drawString(0.6 * inch, 0.45 * inch, f"{d['warehouse'][:60]} · every unit verified by barcode scan")
        c.setFillColor(BLUE)
        c.drawRightString(w - 0.6 * inch, 0.45 * inch, "Verified with Autorack")
        c.restoreState()

    return draw


def _tiles(d: dict[str, Any], width: float) -> Table:
    p = d.get("previous") or {}
    cells = [
        ("Orders shipped", f"{d['orders_shipped']:,}", delta(d["orders_shipped"], p.get("orders_shipped"))),
        ("Units shipped", f"{d['units_shipped']:,}", delta(d["units_shipped"], p.get("units_shipped"))),
        ("Wrong items caught", f"{d['wrong_items_caught']:,}", "stopped before packing"),
        ("Shipped on time", f"{d['on_time']:,}", f"{d['late']:,} late" if d["late"] else "none late"),
    ]
    row = [
        [_text(label.upper(), "tile_label"), _text(value, "tile_value"), _text(sub, "tile_delta")]
        for label, value, sub in cells
    ]
    t = Table([row], colWidths=[width / 4] * 4)
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return t


def render_pdf(d: dict[str, Any], logo: bytes | None) -> bytes:
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=1.35 * inch,
        bottomMargin=0.75 * inch,
        title=f"Order accuracy report, {d['client']}, {d['label']}",
        author=d["warehouse"],
    )
    width = letter[0] - 1.2 * inch
    story: list[Any] = []
    acc = d["accuracy"]
    if d["orders_shipped"]:
        story.append(_text(f"{_pct(acc)} picked right the first time", "h1"))
        story.append(Spacer(1, 4))
        prev = (d.get("previous") or {}).get("accuracy")
        trend = delta(acc, prev, pct_points=True)
        story.append(
            _text(
                f"We shipped {d['orders_shipped']:,} of your orders in {d['label'].split()[0]}. Every unit was "
                f"checked by barcode before it was packed, and {d['wrong_items_caught']:,} wrong "
                f"item{'s were' if d['wrong_items_caught'] != 1 else ' was'} caught and swapped for the right "
                f"one before shipping." + (f" Accuracy {trend}." if trend else ""),
                "body",
            )
        )
    else:
        story.append(_text(f"No orders shipped for you in {d['label']}", "h1"))
    story.append(Spacer(1, 14))
    story.append(_tiles(d, width))
    story.append(Spacer(1, 6))
    detail = [
        ["Units reported short (out of stock or damaged)", f"{d['units_short']:,}"],
        ["Orders with a photo of the packed box", f"{d['orders_with_box_photo']:,}"],
        ["Returns processed", f"{d['returns']:,}"],
        ["Units received into stock", f"{d['units_received']:,}"],
        ["Orders waiting to be picked now", f"{d['open_orders']:,}"],
    ]
    story.append(_table(["", ""], detail, [width * 0.75, width * 0.25]))
    if d["recent"]:
        story.append(
            KeepTogether(
                [
                    _text("Recent shipments", "h2"),
                    _text(
                        "Every order has a full scan record (and a photo of the packed box where one was taken): "
                        "ask us for the evidence pack for any shipment, or download it from your portal.",
                        "muted",
                    ),
                    Spacer(1, 4),
                    _table(
                        ["Order", "Shipped", "Tracking"],
                        [[r["number"], r["shipped_at"], r["tracking"]] for r in d["recent"]],
                        [width * 0.35, width * 0.2, width * 0.45],
                    ),
                ]
            )
        )
    story.append(Spacer(1, 12))
    story.append(
        _text(
            "How it's measured: every unit is scanned at the shelf and checked against your order. A wrong item "
            "is flagged on the spot and put back, so it never ships. First-scan accuracy is right units ÷ (right "
            "units + wrong items caught).",
            "muted",
        )
    )
    doc.build(story, onFirstPage=_header(d, logo), onLaterPages=_header(d, logo))
    return buf.getvalue()


def filename(d: dict[str, Any]) -> str:
    safe = "".join(ch for ch in d["client"] if ch.isalnum())[:30] or "client"
    return f"accuracy-{safe}-{d['month']}.pdf"


def pdf_for(db: Session, wh: Warehouse, client: Client, year: int, month: int) -> tuple[bytes, dict[str, Any]]:
    d = with_previous(db, wh, client, year, month)
    logo = logo_of(db, wh.id)
    return render_pdf(d, logo.data if logo else None), d


# ---------------------------------------------------------------------------
# Monthly email to the client's portal logins
# ---------------------------------------------------------------------------


def recipients(db: Session, wh: Warehouse, client: Client) -> list[str]:
    rows = db.scalars(
        select(User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.warehouse_id == wh.id,
            Membership.client_id == client.id,
            Membership.role == UserRole.client,
            Membership.active.is_(True),
            Membership.pending.is_(False),
            User.active.is_(True),
        )
    )
    return sorted(set(rows))


def run_client_reports(db: Session, now: datetime) -> int:
    from . import email, jobs

    sent = 0
    for wh in db.scalars(select(Warehouse).where(Warehouse.closed_at.is_(None), Warehouse.purged_at.is_(None))):
        local = now.astimezone(tz_of(wh))
        if local.day > 3 or (local.day == 1 and local.hour < 8):
            continue
        year, month = previous_month(local.date())
        for client in db.scalars(
            select(Client).where(Client.warehouse_id == wh.id, Client.active.is_(True), Client.monthly_report.is_(True))
        ):
            key = f"{client.id}:{year}-{month:02d}"
            if db.scalar(
                select(func.count())
                .select_from(NotificationSent)
                .where(
                    NotificationSent.warehouse_id == wh.id,
                    NotificationSent.kind == "client_report",
                    NotificationSent.key == key,
                )
            ):
                continue
            to = recipients(db, wh, client)
            d = numbers(db, wh, client, year, month)
            if not to or not d["orders_shipped"]:
                jobs.claim(db, wh.id, "client_report", key, 0)
                db.commit()
                continue
            pdf, d = pdf_for(db, wh, client, year, month)

            def build(addr: str, d: dict[str, Any] = d, pdf: bytes = pdf, w: Warehouse = wh) -> Any:
                msg = email.notice_email(
                    addr,
                    subject=f"{d['client']}: your {d['label']} order accuracy report from {w.name}",
                    heading=f"Your orders in {d['label']}",
                    lines=[
                        f"{w.name} shipped {d['orders_shipped']:,} of your orders in {d['label'].split()[0]}, "
                        f"every unit checked by barcode. The report is attached.",
                    ],
                    rows=[
                        ("Orders shipped", f"{d['orders_shipped']:,}"),
                        ("Picked right the first time", _pct(d["accuracy"])),
                        ("Wrong items caught before shipping", f"{d['wrong_items_caught']:,}"),
                        ("Shipped on time", f"{d['on_time']:,}"),
                    ],
                    button_label="Open your portal",
                    url=f"{get_settings().frontend_url.rstrip('/')}/portal/",
                    footer=f"Sent by {w.name} on the 1st of each month. Reply to reach them.",
                )
                msg.reply_to = w.owner_email
                msg.attachments.append(email.Attachment(filename(d), pdf, "application/pdf"))
                return msg

            if jobs._send_all(db, wh, "client_report", key, to, build):
                sent += 1
    return sent
