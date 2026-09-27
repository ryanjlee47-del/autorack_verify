"""The monthly report: "here's what Autorack saved you", as a PDF.

Emailed to the owners on the 1st (warehouse local time, from 8am) for the
month before, and downloadable for any month from Reports. The numbers are
the same ones the Reports page shows (services/reports.py), plus the month
before for comparison and a line on receiving, returns and counts.
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta
from typing import Any

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import TALLY_KINDS, NotificationSent, Order, OrderKind, OrderStatus, Warehouse, utcnow
from . import billing, reports, tasks
from .dashboard import day_bounds, tz_of

log = logging.getLogger("autorack.monthly")

NAVY = colors.HexColor("#162238")
BLUE = colors.HexColor("#3e7bfa")
GREEN = colors.HexColor("#1f7a4d")
RED = colors.HexColor("#b42318")
MUTED = colors.HexColor("#6b7486")
RULE = colors.HexColor("#e3e6ec")
SOFT = colors.HexColor("#f4f6fa")
TIMES = "\u00d7"


def month_range(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def previous_month(d: date) -> tuple[int, int]:
    last = d.replace(day=1) - timedelta(days=1)
    return last.year, last.month


def parse_month(value: str | None, today: date) -> tuple[int, int]:
    if not value:
        return previous_month(today)
    y, m = value.split("-")
    year, month = int(y), int(m)
    if not (2020 <= year <= 2100 and 1 <= month <= 12):
        raise ValueError("month out of range")
    return year, month


def data(db: Session, wh: Warehouse, year: int, month: int) -> dict[str, Any]:
    start, end = month_range(year, month)
    today = utcnow().astimezone(tz_of(wh)).date()
    partial = end >= today
    end = min(end, today) if start <= today else end
    cur = reports.build(db, wh, start, end)
    py, pm = previous_month(start)
    ps, pe = month_range(py, pm)
    prev = reports.build(db, wh, ps, pe)["totals"]

    s_utc, _, _ = day_bounds(wh, start)
    _, e_utc, _ = day_bounds(wh, end)
    finished = list(
        db.scalars(
            select(Order).where(
                Order.warehouse_id == wh.id,
                Order.kind.in_(TALLY_KINDS),
                Order.status != OrderStatus.cancelled,
                Order.completed_at >= s_utc,
                Order.completed_at < e_utc,
            )
        )
    )
    jobs: dict[str, dict[str, int]] = {k.value: {"finished": 0, "with_differences": 0} for k in TALLY_KINDS}
    for o in finished[:500]:
        row = jobs[o.kind.value]
        row["finished"] += 1
        if not tasks.variance(db, o)["matches"]:
            row["with_differences"] += 1
    returns_opened = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.warehouse_id == wh.id,
                Order.kind == OrderKind.ret,
                Order.created_at >= s_utc,
                Order.created_at < e_utc,
            )
        )
        or 0
    )
    return {
        "warehouse": wh.name,
        "year": year,
        "month": month,
        "label": start.strftime("%B %Y"),
        "from": start,
        "to": end,
        "partial": partial,
        "totals": cur["totals"],
        "previous": prev,
        "previous_label": ps.strftime("%B"),
        "skus": [s for s in cur["skus"] if s["mispicks"]][:8],
        "workers": [w for w in cur["workers"] if w["units_picked"] or w["errors"]][:10],
        "customers": [c for c in cur["customers"] if c["units_picked"]][:8],
        "days": cur["days"],
        "jobs": jobs,
        "returns_opened": int(returns_opened),
        "cost_per_error_cents": wh.cost_per_error_cents,
        "price_cents": billing.monthly_cost_cents(wh),
    }


def has_activity(d: dict[str, Any]) -> bool:
    t = d["totals"]
    return bool(t["units_picked"] or t["errors_caught"] or t["orders_shipped"] or t["orders_completed"])


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _money(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _delta(cur: float | None, prev: float | None, *, pct_points: bool = False) -> str:
    if cur is None or prev is None:
        return ""
    if pct_points:
        diff = (cur - prev) * 100
        return "same as last month" if abs(diff) < 0.05 else f"{diff:+.1f} pts vs last month"
    if not prev:
        return "" if not cur else "new this month"
    change = (cur - prev) / prev * 100
    return "same as last month" if abs(change) < 0.5 else f"{change:+.0f}% vs last month"


S = {
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=22, leading=27, textColor=NAVY),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=NAVY, spaceBefore=14),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14, textColor=NAVY),
    "muted": ParagraphStyle("muted", fontName="Helvetica", fontSize=8.5, leading=12, textColor=MUTED),
    "big": ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=30, leading=34, textColor=GREEN),
    "tile_label": ParagraphStyle("tl", fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=MUTED),
    "tile_value": ParagraphStyle("tv", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=NAVY),
    "tile_delta": ParagraphStyle("td", fontName="Helvetica", fontSize=7.5, leading=10, textColor=MUTED),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=9, leading=12, textColor=NAVY),
    "cell_r": ParagraphStyle("cellr", fontName="Helvetica", fontSize=9, leading=12, textColor=NAVY, alignment=TA_RIGHT),
}


def _tile(label: str, value: str, delta: str) -> list[Any]:
    return [
        Paragraph(label.upper(), S["tile_label"]),
        Paragraph(value, S["tile_value"]),
        Paragraph(delta, S["tile_delta"]),
    ]


def _table(header: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    body = [[Paragraph(f"<b>{h}</b>", S["cell_r"] if i else S["cell"]) for i, h in enumerate(header)]]
    for r in rows:
        body.append([Paragraph(str(c), S["cell_r"] if i else S["cell"]) for i, c in enumerate(r)])
    t = Table(body, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, NAVY),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return t


def _chart(days: list[dict[str, Any]], width: float, height: float = 1.3 * inch) -> Drawing:
    d = Drawing(width, height)
    if not days:
        return d
    top = max([x["units_picked"] for x in days] + [1])
    n = len(days)
    gap = 2.0
    bar_w = max(1.0, (width - gap * n) / n)
    base = 14
    usable = height - base - 6
    d.add(Line(0, base, width, base, strokeColor=RULE, strokeWidth=0.6))
    for i, x in enumerate(days):
        h = usable * x["units_picked"] / top
        left = i * (bar_w + gap)
        d.add(Rect(left, base, bar_w, h, fillColor=BLUE, strokeColor=None))
        if x["errors_caught"]:
            d.add(Rect(left, base + h + 1.5, bar_w, 3, fillColor=RED, strokeColor=None))
        if i == 0 or i == n - 1 or (i + 1) % 7 == 0:
            d.add(
                String(
                    left,
                    2,
                    date.fromisoformat(x["date"]).strftime("%-d %b"),
                    fontName="Helvetica",
                    fontSize=6.5,
                    fillColor=MUTED,
                )
            )
    return d


def _header_footer(d: dict[str, Any]) -> Any:
    def draw(c: Any, doc: Any) -> None:
        w, h = letter
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - 0.62 * inch, w, 0.62 * inch, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(0.6 * inch, h - 0.36 * inch, "AUTORACK")
        c.setFillColor(BLUE)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(0.6 * inch, h - 0.49 * inch, "V E R I F I E D   L O G I S T I C S")
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 9)
        c.drawRightString(w - 0.6 * inch, h - 0.36 * inch, f"Monthly report · {d['label']}")
        c.drawRightString(w - 0.6 * inch, h - 0.5 * inch, d["warehouse"][:70])
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        c.drawString(
            0.6 * inch, 0.45 * inch, f"Generated {utcnow().strftime('%b %-d, %Y')} from your Autorack scan records."
        )
        c.drawRightString(w - 0.6 * inch, 0.45 * inch, f"Page {doc.page}")
        c.restoreState()

    return draw


def render_pdf(d: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.95 * inch,
        bottomMargin=0.75 * inch,
        title=f"Autorack monthly report, {d['label']}",
        author="Autorack",
    )
    width = letter[0] - 1.2 * inch
    t, p = d["totals"], d["previous"]
    cost = d["cost_per_error_cents"]
    saved = t["money_saved_cents"]
    story: list[Any] = []

    if t["errors_caught"]:
        headline = (
            f"Autorack stopped {t['errors_caught']:,} wrong item{'s' if t['errors_caught'] != 1 else ''} "
            f"from shipping in {d['label'].split()[0]}."
        )
    else:
        headline = f"Every scan in {d['label'].split()[0]} was right the first time."
    story.append(Paragraph(headline, S["h1"]))
    story.append(Spacer(1, 6))
    if saved:
        multiple = saved / d["price_cents"] if d["price_cents"] else 0
        story.append(Paragraph(f"≈ {_money(saved)} saved", S["big"]))
        story.append(Spacer(1, 4))
        story.append(
            Paragraph(
                f"{t['errors_caught']:,} mistakes {TIMES} {_money(cost)} each (returns, reshipping, credits and time: "
                "your figure, set in Settings)."
                + (
                    f" That's <b>{multiple:.1f}{TIMES}</b> the {_money(d['price_cents'])} Autorack costs a month."
                    if multiple >= 1
                    else ""
                ),
                S["body"],
            )
        )
    if d["partial"]:
        story.append(Paragraph(f"Month to date, through {d['to'].strftime('%B %-d')}.", S["muted"]))
    story.append(Spacer(1, 14))

    tiles = [
        _tile("Orders shipped", f"{t['orders_shipped']:,}", _delta(t["orders_shipped"], p["orders_shipped"])),
        _tile("Units verified", f"{t['units_picked']:,}", _delta(t["units_picked"], p["units_picked"])),
        _tile("Mistakes caught", f"{t['errors_caught']:,}", _delta(t["errors_caught"], p["errors_caught"])),
        _tile("First-scan accuracy", _pct(t["accuracy"]), _delta(t["accuracy"], p["accuracy"], pct_points=True)),
    ]
    grid = Table([tiles], colWidths=[width / 4] * 4)
    grid.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SOFT),
                ("LINEBEFORE", (1, 0), (-1, -1), 2, colors.white),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(grid)

    story.append(Paragraph("Every day this month", S["h2"]))
    story.append(Paragraph("Units verified per day (blue); a red cap marks days with mistakes caught.", S["muted"]))
    story.append(Spacer(1, 4))
    story.append(_chart(d["days"], width))

    if d["skus"]:
        story.append(
            KeepTogether(
                [
                    Paragraph("Items most often picked wrong", S["h2"]),
                    Paragraph(
                        "The item a worker was trying to pick when they scanned the wrong thing. "
                        "A look-alike product in a nearby bin, or a bad shelf label, is the usual cause.",
                        S["muted"],
                    ),
                    Spacer(1, 4),
                    _table(
                        ["Item", "Wrong picks", "Units verified"],
                        [
                            [
                                s["description"] or s["sku"] or s["barcode"],
                                f"{s['mispicks']:,}",
                                f"{s['units_picked']:,}",
                            ]
                            for s in d["skus"]
                        ],
                        [width * 0.6, width * 0.2, width * 0.2],
                    ),
                ]
            )
        )
    if d["workers"]:
        story.append(
            KeepTogether(
                [
                    Paragraph("Your team", S["h2"]),
                    _table(
                        ["Worker", "Units", "Mistakes caught", "Accuracy"],
                        [
                            [w["name"], f"{w['units_picked']:,}", f"{w['errors']:,}", _pct(w["accuracy"])]
                            for w in d["workers"]
                        ],
                        [width * 0.4, width * 0.2, width * 0.2, width * 0.2],
                    ),
                ]
            )
        )
    if len(d["customers"]) > 1:
        story.append(
            KeepTogether(
                [
                    Paragraph("By customer", S["h2"]),
                    _table(
                        ["Customer", "Orders", "Units", "Mistakes caught", "Saved"],
                        [
                            [
                                c["customer"],
                                f"{c['orders_touched']:,}",
                                f"{c['units_picked']:,}",
                                f"{c['errors_caught']:,}",
                                _money(c["money_saved_cents"]),
                            ]
                            for c in d["customers"]
                        ],
                        [width * 0.36, width * 0.14, width * 0.16, width * 0.18, width * 0.16],
                    ),
                ]
            )
        )
    j = d["jobs"]
    if any(v["finished"] for v in j.values()) or d["returns_opened"]:
        rows = []
        for key, label in (
            ("receive", "Deliveries received"),
            ("return", "Returns checked"),
            ("count", "Cycle counts"),
        ):
            if j[key]["finished"]:
                rows.append([label, f"{j[key]['finished']:,}", f"{j[key]['with_differences']:,}"])
        story.append(
            KeepTogether(
                [
                    Paragraph("Receiving, returns and counts", S["h2"]),
                    _table(["", "Finished", "With differences"], rows, [width * 0.5, width * 0.25, width * 0.25]),
                ]
            )
        )
    story.append(Spacer(1, 16))
    story.append(
        Paragraph(
            f"Compared with {d['previous_label']}. First-scan accuracy is right items ÷ (right items + mistakes "
            "caught). Mistakes caught are wrong items and extra units scanned and put back before packing. "
            "Receiving, returns and counts aren't included in the pick numbers.",
            S["muted"],
        )
    )
    doc.build(story, onFirstPage=_header_footer(d), onLaterPages=_header_footer(d))
    return buf.getvalue()


def filename(d: dict[str, Any]) -> str:
    return f"autorack-{d['year']}-{d['month']:02d}.pdf"


# ---------------------------------------------------------------------------
# The job: the 1st of the month, from 8am local, to the owners
# ---------------------------------------------------------------------------


def run_monthly_reports(db: Session, now: datetime) -> int:
    from . import email, jobs

    sent = 0
    for wh in db.scalars(
        select(Warehouse).where(
            Warehouse.monthly_report_enabled.is_(True), Warehouse.closed_at.is_(None), Warehouse.purged_at.is_(None)
        )
    ):
        local = now.astimezone(tz_of(wh))
        # From 8am on the 1st; the 2nd and 3rd are catch-up if the app was asleep.
        if local.day > 3 or (local.day == 1 and local.hour < 8):
            continue
        year, month = previous_month(local.date())
        key = f"{year}-{month:02d}"
        if db.scalar(
            select(func.count())
            .select_from(NotificationSent)
            .where(
                NotificationSent.warehouse_id == wh.id,
                NotificationSent.kind == "monthly_report",
                NotificationSent.key == key,
            )
        ):
            continue
        d = data(db, wh, year, month)
        if not has_activity(d):
            jobs.claim(db, wh.id, "monthly_report", key, 0)
            db.commit()
            continue
        pdf = render_pdf(d)
        t = d["totals"]
        lines = [
            f"Your Autorack report for {d['label']} is attached.",
            (
                f"Autorack caught {t['errors_caught']:,} mistake{'s' if t['errors_caught'] != 1 else ''} before "
                f"they shipped — about {_money(t['money_saved_cents'])} saved."
                if t["errors_caught"]
                else "No wrong items were scanned all month."
            ),
        ]

        def build(to: str, lines: list[str] = lines, d: dict[str, Any] = d, pdf: bytes = pdf) -> Any:
            msg = email.notice_email(
                to,
                subject=f"{d['warehouse']}: your {d['label']} Autorack report",
                heading=f"{d['label']} at {d['warehouse']}",
                lines=lines,
                rows=[
                    ("Orders shipped", f"{d['totals']['orders_shipped']:,}"),
                    ("Units verified", f"{d['totals']['units_picked']:,}"),
                    ("Mistakes caught", f"{d['totals']['errors_caught']:,}"),
                    ("First-scan accuracy", _pct(d["totals"]["accuracy"])),
                ],
                button_label="Open reports",
                url=jobs.app_url("#/reports"),
                footer="Sent to the owners on the 1st of each month. Turn it off in Settings.",
            )
            msg.attachments.append(email.Attachment(filename(d), pdf, "application/pdf"))
            return msg

        if jobs._send_all(db, wh, "monthly_report", key, jobs.recipients(db, wh, want="owners"), build):
            sent += 1
    return sent
