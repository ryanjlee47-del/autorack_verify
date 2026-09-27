"""The license agreement every warehouse owner e-signs before using Autorack.

The document is a PDF kept in autorack/legal/ with a small JSON file saying
where its blanks are. Signing records who signed which exact bytes (version +
SHA-256), for which company, when and from where, and keeps a signed copy:
the original PDF with the signer's details stamped into the blanks and a
signature certificate appended as the last page.

Replacing the agreement: add license-agreement-v2.pdf/.json, render its page
images (backend/scripts/render_agreement_pages.py), point CURRENT_VERSION at
it. Every owner is then asked to sign the new version on next sign-in.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import bad_request
from ..models import AgreementSignature, User, Warehouse, utcnow
from . import audit
from .audit import Actor

# The source PDF (exported by macOS) has a slightly off xref table; pypdf
# repairs it and says so on every read. Harmless, and noisy in the logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)

LEGAL_DIR = Path(__file__).resolve().parents[1] / "legal"
CURRENT_VERSION = "v1"
SIGNATURE_FONT = "AutorackSignature"
INK = HexColor("#1a2a6c")  # dark blue, like a pen

CONSENT_TEXT = (
    "I have reviewed the entire Autorack Application License Agreement. I am authorized to enter into it on "
    "behalf of the Client named above. I agree to its terms, and I agree that the name I typed is my "
    "electronic signature and has the same effect as signing by hand (Section 22, Electronic Signatures)."
)


@dataclass(frozen=True)
class Agreement:
    version: str
    title: str
    pdf: bytes
    sha256: str
    pages: int
    page_size: tuple[float, float]
    fields: dict[str, dict[str, Any]]

    def public(self) -> dict[str, Any]:
        """What the signing page needs to show it and overlay the blanks."""
        return {
            "version": self.version,
            "title": self.title,
            "sha256": self.sha256,
            "pages": [f"/legal/{self.version}/page-{i}.png" for i in range(1, self.pages + 1)],
            "page_size": list(self.page_size),
            "fields": self.fields,
            # A byte-identical copy ships with the frontend (checked by a test),
            # so the link works whether or not the API is on the same host.
            "pdf_url": f"/legal/license-agreement-{self.version}.pdf",
            "consent_text": CONSENT_TEXT,
            "countersigner": {
                "name": get_settings().agreement_countersigner_name or None,
                "title": get_settings().agreement_countersigner_title or None,
            },
        }


@lru_cache
def load(version: str = CURRENT_VERSION) -> Agreement:
    meta = json.loads((LEGAL_DIR / f"license-agreement-{version}.json").read_text())
    pdf = (LEGAL_DIR / meta["file"]).read_bytes()
    sha = hashlib.sha256(pdf).hexdigest()
    if sha != meta["sha256"]:
        # The PDF changed without a new version: signatures would point at
        # the wrong text. Refuse loudly rather than collect them.
        raise RuntimeError(f"{meta['file']} does not match the sha256 in its JSON; publish it as a new version")
    return Agreement(
        version=meta["version"],
        title=meta["title"],
        pdf=pdf,
        sha256=sha,
        pages=meta["pages"],
        page_size=(float(meta["page_size"][0]), float(meta["page_size"][1])),
        fields=meta["fields"],
    )


def current() -> Agreement:
    return load(CURRENT_VERSION)


# ---------------------------------------------------------------------------
# Who has signed
# ---------------------------------------------------------------------------


def signature_for(db: Session, warehouse_id: uuid.UUID, version: str | None = None) -> AgreementSignature | None:
    return db.scalar(
        select(AgreementSignature)
        .where(
            AgreementSignature.warehouse_id == warehouse_id,
            AgreementSignature.agreement_version == (version or CURRENT_VERSION),
        )
        .order_by(AgreementSignature.signed_at.desc())
        .limit(1)
    )


def signature_for_any(db: Session, warehouse_id: uuid.UUID) -> AgreementSignature | None:
    """The most recent signature on any version (for the signed copy)."""
    return db.scalar(
        select(AgreementSignature)
        .where(AgreementSignature.warehouse_id == warehouse_id)
        .order_by(AgreementSignature.signed_at.desc())
        .limit(1)
    )


def signed_filename(sig: AgreementSignature) -> str:
    company = "".join(ch for ch in sig.company_name if ch.isalnum() or ch in " -_").strip().replace(" ", "-")[:60]
    return f"Autorack-License-Agreement-{sig.agreement_version}-{company or 'signed'}-{sig.signed_at:%Y-%m-%d}.pdf"


def is_signed(db: Session, warehouse_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(AgreementSignature.id).where(
                AgreementSignature.warehouse_id == warehouse_id,
                AgreementSignature.agreement_version == CURRENT_VERSION,
            )
        )
        is not None
    )


def signature_dict(sig: AgreementSignature | None) -> dict[str, Any] | None:
    if sig is None:
        return None
    return {
        "id": str(sig.id),
        "version": sig.agreement_version,
        "signer_name": sig.signer_name,
        "signer_title": sig.signer_title,
        "signer_email": sig.signer_email,
        "company_name": sig.company_name,
        "company_address": sig.company_address,
        "signed_at": sig.signed_at.isoformat(),
        "document_sha256": sig.document_sha256,
    }


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


@dataclass
class SignerDetails:
    signer_name: str
    signer_title: str
    company_name: str
    company_address: str
    agreement_version: str
    accepted: bool
    viewed_seconds: int | None = None


def clean(details: SignerDetails) -> SignerDetails:
    def tidy(value: str, limit: int) -> str:
        return " ".join((value or "").split())[:limit]

    d = SignerDetails(
        signer_name=tidy(details.signer_name, 200),
        signer_title=tidy(details.signer_title, 200),
        company_name=tidy(details.company_name, 300),
        company_address=tidy(details.company_address, 500),
        agreement_version=details.agreement_version,
        accepted=details.accepted,
        viewed_seconds=details.viewed_seconds,
    )
    if d.agreement_version != CURRENT_VERSION:
        raise bad_request(
            "agreement_outdated", "The agreement was updated while you were reading. Reload the page to see it."
        )
    if not d.accepted:
        raise bad_request("agreement_not_accepted", "Tick the box to confirm you've reviewed and agree to it.")
    if len(d.signer_name) < 2:
        raise bad_request("signer_name_required", "Type your full name to sign.")
    if not d.signer_title:
        raise bad_request("signer_title_required", "Enter your title (for example Owner or Operations Manager).")
    if not d.company_name:
        raise bad_request("company_name_required", "Enter your company's legal name.")
    if not d.company_address:
        raise bad_request("company_address_required", "Enter your company's address.")
    return d


def sign(
    db: Session,
    wh: Warehouse,
    user: User,
    details: SignerDetails,
    *,
    ip: str | None,
    user_agent: str | None,
) -> AgreementSignature:
    """Record the signature and store the stamped PDF. Caller commits."""
    d = clean(details)
    doc = current()
    now = utcnow()
    sig_id = uuid.uuid4()
    pdf = stamp(
        doc,
        d,
        signer_email=user.email,
        signed_at=now,
        timezone=wh.timezone,
        signature_id=sig_id,
        ip=ip,
        user_agent=user_agent,
    )
    sig = AgreementSignature(
        id=sig_id,
        warehouse_id=wh.id,
        user_id=user.id,
        agreement_version=doc.version,
        document_sha256=doc.sha256,
        signer_name=d.signer_name,
        signer_title=d.signer_title,
        signer_email=user.email,
        company_name=d.company_name,
        company_address=d.company_address,
        consent_text=CONSENT_TEXT,
        viewed_seconds=d.viewed_seconds,
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
        signed_at=now,
        signed_pdf=pdf,
        signed_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
    )
    db.add(sig)
    db.flush()
    audit.record(
        db,
        Actor("user", str(user.id), user.email, ip),
        "agreement.signed",
        warehouse_id=wh.id,
        target_type="agreement",
        target_id=sig.id,
        version=doc.version,
        signer=d.signer_name,
        company=d.company_name,
    )
    return sig


# ---------------------------------------------------------------------------
# The signed PDF
# ---------------------------------------------------------------------------


def _fonts() -> None:
    if SIGNATURE_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(SIGNATURE_FONT, str(LEGAL_DIR / "DancingScript-500.ttf")))


def _fit(text: str, font: str, size: float, width: float, minimum: float = 5.0) -> float:
    while size > minimum and pdfmetrics.stringWidth(text, font, size) > width:
        size -= 0.25
    return size


def signing_date(signed_at: datetime, timezone: str) -> str:
    try:
        local = signed_at.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        local = signed_at
    return f"{local:%B} {local.day}, {local.year}"


def field_values(d: SignerDetails, signed_at: datetime, timezone: str) -> dict[str, str]:
    s = get_settings()
    values = {
        "effective_date": signing_date(signed_at, timezone),
        "company_name": d.company_name,
        "company_address": d.company_address,
        "client_signature": d.signer_name,
        "client_name": d.signer_name,
        "client_title": d.signer_title,
    }
    if s.agreement_countersigner_name:
        values["autorack_signature"] = s.agreement_countersigner_name
        values["autorack_name"] = s.agreement_countersigner_name
        values["autorack_title"] = s.agreement_countersigner_title
    return values


def stamp(
    doc: Agreement,
    d: SignerDetails,
    *,
    signer_email: str,
    signed_at: datetime,
    timezone: str,
    signature_id: uuid.UUID,
    ip: str | None,
    user_agent: str | None,
) -> bytes:
    _fonts()
    width, height = doc.page_size
    values = field_values(d, signed_at, timezone)
    by_page: dict[int, list[tuple[dict[str, Any], str]]] = {}
    for key, value in values.items():
        spec = doc.fields.get(key)
        if spec and value:
            by_page.setdefault(int(spec["box"][0]), []).append((spec, value))

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(doc.pdf)))
    for number, page in enumerate(writer.pages, start=1):
        if number in by_page:
            page.merge_page(_overlay(by_page[number], width, height))
    writer.add_page(
        _certificate(
            doc,
            d,
            signer_email=signer_email,
            signed_at=signed_at,
            signature_id=signature_id,
            ip=ip,
            user_agent=user_agent,
            width=width,
            height=height,
        )
    )
    writer.add_metadata(
        {
            "/Title": f"{doc.title} ({doc.version}) — signed by {d.company_name}",
            "/Author": "Autorack",
            "/Subject": f"Electronically signed {signed_at.isoformat()}",
        }
    )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _overlay(items: list[tuple[dict[str, Any], str]], width: float, height: float) -> Any:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    for spec, value in items:
        _, x0, y0, x1, y1 = spec["box"]
        script = bool(spec.get("script"))
        font = SIGNATURE_FONT if script else "Helvetica"
        size = _fit(value, font, float(spec.get("size", 10)), (x1 - x0) - 2)
        if spec.get("mode") == "cover":
            c.setFillColor(white)
            c.rect(x0 - 0.5, height - y1 - 1, (x1 - x0) + 1, (y1 - y0) + 1.5, stroke=0, fill=1)
            c.setFillColor(INK)
            c.setFont(font, size)
            c.drawString(x0, height - y1 + 2.4, value)
        else:
            c.setFillColor(INK)
            c.setFont(font, size)
            # Sit on the underline: a little above the bottom of the line's box.
            c.drawString(x0 + 3, height - y1 + (3.5 if script else 3.0), value)
    c.save()
    return PdfReader(io.BytesIO(buf.getvalue())).pages[0]


def _certificate(
    doc: Agreement,
    d: SignerDetails,
    *,
    signer_email: str,
    signed_at: datetime,
    signature_id: uuid.UUID,
    ip: str | None,
    user_agent: str | None,
    width: float,
    height: float,
) -> Any:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    navy = HexColor("#162238")
    grey = HexColor("#5b6475")
    x = 64.0
    y = height - 72.0
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, "Electronic signature certificate")
    y -= 18
    c.setFont("Helvetica", 9.5)
    c.setFillColor(grey)
    c.drawString(x, y, "Appended by Autorack. Records how and when this Agreement was signed electronically.")
    y -= 30

    def row(label: str, value: str, mono: bool = False) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(grey)
        c.drawString(x, y, label.upper())
        c.setFont("Courier" if mono else "Helvetica", 10.5)
        c.setFillColor(navy)
        for line in _wrap(value, "Courier" if mono else "Helvetica", 10.5, width - x - 64 - 150):
            c.drawString(x + 150, y, line)
            y -= 14
        y -= 6

    row("Document", f"{doc.title}, version {doc.version}")
    row("Document SHA-256", doc.sha256, mono=True)
    row("Client", d.company_name)
    row("Client address", d.company_address)
    row("Signed by", f"{d.signer_name}, {d.signer_title}")
    row("Signer email", signer_email)
    row("Signed at (UTC)", signed_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
    row("IP address", ip or "unknown")
    row("Browser", (user_agent or "unknown")[:300])
    row("Signature ID", str(signature_id), mono=True)
    if d.viewed_seconds is not None:
        row("Time on document", f"{d.viewed_seconds // 60} min {d.viewed_seconds % 60} s, scrolled to the end")
    y -= 6
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(grey)
    c.drawString(x, y, "SIGNER'S CONFIRMATION")
    y -= 16
    c.setFont("Helvetica", 10.5)
    c.setFillColor(navy)
    for line in _wrap(CONSENT_TEXT, "Helvetica", 10.5, width - 2 * x):
        c.drawString(x, y, line)
        y -= 14
    y -= 14
    _fonts()
    c.setFont(SIGNATURE_FONT, _fit(d.signer_name, SIGNATURE_FONT, 26, 260))
    c.setFillColor(INK)
    c.drawString(x, y - 10, d.signer_name)
    c.setStrokeColor(grey)
    c.line(x, y - 16, x + 260, y - 16)
    c.setFont("Helvetica", 9)
    c.setFillColor(grey)
    c.drawString(x, y - 28, f"Typed by {d.signer_name} as their electronic signature")
    c.save()
    return PdfReader(io.BytesIO(buf.getvalue())).pages[0]


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    def fits(t: str) -> bool:
        return pdfmetrics.stringWidth(t, font, size) <= width

    tokens: list[str] = []
    for word in text.split():
        # A token longer than a line (a hash, a user agent): hard-break it.
        while not fits(word) and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and not fits(word[:cut]):
                cut -= 1
            tokens.append(word[:cut])
            word = word[cut:]
        tokens.append(word)
    lines: list[str] = []
    for t in tokens:
        if lines and fits(f"{lines[-1]} {t}"):
            lines[-1] = f"{lines[-1]} {t}"
        else:
            lines.append(t)
    return lines or [""]
