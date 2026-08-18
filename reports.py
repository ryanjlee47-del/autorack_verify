"""Savings report generation, triggered from the operator GUI.

Produces a standalone HTML file per account -- inline CSS, no external
asset references -- so it opens directly as a file:// URL and prints
cleanly to PDF via the browser's native print dialog. That gets a
professional-looking, shareable report without adding a PDF-rendering
dependency (reportlab/weasyprint/etc.) to the fixed stack: Jinja2 is
already a dependency (of Flask), and this module uses it directly,
independent of any Flask request context, since admin_gui.py isn't a
Flask app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import jinja2

import billing
import db
import pricing

BASE_DIR = Path(__file__).parent

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
)


def _safe_filename_part(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()
    return cleaned or "account"


def generate_savings_report_html(conn, account_id: int, generated_at: str | None = None) -> str:
    account = db.get_account(conn, account_id)
    if not account:
        raise ValueError(f"no account {account_id}")

    catches = db.count_billable_catches(conn, account_id)
    savings_cents = billing.savings_to_date_cents(conn, account_id)
    net_owed_cents = billing.net_amount_owed_cents(conn, account_id)
    worker_rows = db.worker_stats_for_account(conn, account_id)

    workers = []
    for r in worker_rows:
        total = r["total_scans"] or 0
        reject_rate = f"{(r['reject_count'] / total * 100):.0f}%" if total else "--"
        workers.append(
            {
                "display_name": r["display_name"],
                "total_scans": total,
                "ok_count": r["ok_count"] or 0,
                "reject_count": r["reject_count"] or 0,
                "duplicate_count": r["duplicate_count"] or 0,
                "unresolved_count": r["unresolved_count"] or 0,
                "reject_rate": reject_rate,
                "billed_amount": pricing.format_cents_as_dollars(r["billed_cents"] or 0),
            }
        )

    template = _env.get_template("savings_report.html")
    return template.render(
        account_name=account["name"],
        generated_at=generated_at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        catches=catches,
        free_allowance=account["free_allowance"],
        price_per_catch=pricing.format_cents_as_dollars(account["price_per_catch_cents"]),
        savings=pricing.format_cents_as_dollars(savings_cents),
        net_owed=pricing.format_cents_as_dollars(net_owed_cents),
        workers=workers,
    )


def write_savings_report(conn, account_id: int, dest_path: Path | str) -> Path:
    """Render and write one account's report to an exact path."""
    html = generate_savings_report_html(conn, account_id)
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(html, encoding="utf-8")
    return dest_path


def default_report_filename(account_row) -> str:
    return f"savings-report-{account_row['id']}-{_safe_filename_part(account_row['name'])}.html"


def write_savings_reports_for_all_accounts(conn, dest_dir: Path | str) -> list[Path]:
    """One report per account, written into dest_dir with an
    auto-generated filename per account. Returns the paths written."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for account in db.list_accounts(conn):
        dest = dest_dir / default_report_filename(account)
        written.append(write_savings_report(conn, account["id"], dest))
    return written
