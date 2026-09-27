"""The license agreement: signed at sign-up, required before the dashboard
works, stored as evidence, downloadable as a stamped PDF."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from conftest import AGREEMENT, Owner, last_link_token, signup
from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from autorack.config import get_settings
from autorack.models import AgreementSignature, User, Warehouse
from autorack.services import agreement as agreement_svc
from autorack.services import auth as auth_svc
from autorack.services.audit import OPERATOR

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def test_public_agreement_matches_the_files_it_points_to(client):
    info = client.get("/api/legal/agreement").json()
    assert info["version"] == agreement_svc.CURRENT_VERSION
    assert len(info["pages"]) == 8
    for page in info["pages"]:
        assert (FRONTEND / page.lstrip("/")).is_file(), page
    pdf = client.get("/api/legal/agreement.pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    assert hashlib.sha256(pdf.content).hexdigest() == info["sha256"]
    # The frontend's copy (linked from the site) must be the very same file.
    static = (FRONTEND / info["pdf_url"].lstrip("/")).read_bytes()
    assert hashlib.sha256(static).hexdigest() == info["sha256"]
    assert "Section 22" in info["consent_text"]


def test_signup_records_a_signature_and_a_signed_copy(client, db):
    owner = signup(client, "Harbor DC")
    sig = db.scalar(select(AgreementSignature))
    assert sig.signer_name == AGREEMENT["signer_name"] and sig.company_name == AGREEMENT["company_name"]
    assert sig.document_sha256 == agreement_svc.current().sha256
    assert sig.viewed_seconds == 95 and sig.consent_text == agreement_svc.CONSENT_TEXT
    status = client.get("/api/agreement", headers=owner.h).json()
    assert status["required"] is False and status["signature"]["signer_title"] == "Owner"

    r = client.get("/api/agreement/signed.pdf", headers=owner.h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert hashlib.sha256(r.content).hexdigest() == sig.signed_pdf_sha256
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 9  # the agreement plus the signature certificate
    body = pdf_text(r.content)
    assert "Electronic signature certificate" in body
    assert AGREEMENT["company_name"] in body and owner.email in body
    assert agreement_svc.current().sha256 in body.replace("\n", "")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"accept_agreement": False}, "agreement_not_accepted"),
        ({"agreement_version": "v0"}, "agreement_outdated"),
        ({"signer_name": " "}, "signer_name_required"),
        ({"company_address": "   "}, "company_address_required"),
    ],
)
def test_no_account_without_a_valid_signature(client, db, change, code):
    body = {"warehouse_name": "X", "email": "nosig@example.com", "timezone": "UTC", **AGREEMENT, **change}
    r = client.post("/api/auth/signup", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == code
    assert db.scalar(select(User).where(User.email == "nosig@example.com")) is None
    assert db.scalar(select(Warehouse)) is None


def unsigned_warehouse(client, db) -> Owner:
    """A warehouse from before the agreement existed (or made by the CLI)."""
    wh, user = auth_svc.create_warehouse(db, name="Old Co", owner_email="old@example.com", actor=OPERATOR)
    auth_svc.issue_magic_link(db, user, None)
    db.commit()
    client.post("/api/auth/magic-link", json={"email": "old@example.com"})
    token = client.post("/api/auth/verify", json={"token": last_link_token("old@example.com")}).json()["token"]
    return Owner(token=token, email="old@example.com", warehouse_id=str(wh.id))


def test_existing_warehouse_must_sign_before_using_the_dashboard(client, db):
    owner = unsigned_warehouse(client, db)
    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["agreement"] == {"required": True, "can_sign": True, "version": agreement_svc.CURRENT_VERSION}
    for url in ("/api/orders", "/api/dashboard/summary", "/api/workers", "/api/billing"):
        r = client.get(url, headers=owner.h)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "agreement_required", url
    assert client.get("/api/agreement/signed.pdf", headers=owner.h).status_code == 404

    r = client.post("/api/agreement/sign", json=AGREEMENT, headers=owner.h)
    assert r.status_code == 200 and r.json()["required"] is False
    assert client.get("/api/orders", headers=owner.h).status_code == 200
    assert client.get("/api/auth/me", headers=owner.h).json()["agreement"]["required"] is False


def test_only_owners_sign(client, db):
    owner = unsigned_warehouse(client, db)
    wh = db.scalar(select(Warehouse).where(Warehouse.name == "Old Co"))
    mgr_user = User(warehouse_id=wh.id, email="mgr@example.com")
    db.add(mgr_user)
    db.flush()
    auth_svc.add_membership(db, mgr_user, wh.id, auth_svc.UserRole.manager)
    auth_svc.issue_magic_link(db, mgr_user, None)
    db.commit()
    client.post("/api/auth/magic-link", json={"email": "mgr@example.com"})
    token = client.post("/api/auth/verify", json={"token": last_link_token("mgr@example.com")}).json()["token"]
    mgr = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=mgr).json()["agreement"]["can_sign"] is False
    r = client.post("/api/agreement/sign", json=AGREEMENT, headers=mgr)
    assert r.status_code == 403
    assert client.get("/api/agreement", headers=owner.h).json()["required"] is True


def test_signatures_are_append_only(client, db):
    signup(client)
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE agreement_signatures SET signer_name = 'Someone else'"))
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM agreement_signatures"))
    db.rollback()


def test_operator_can_download_any_signed_copy(client, monkeypatch):
    owner = signup(client, "Signed Co")
    monkeypatch.setattr(get_settings(), "operator_emails", "ops@example.com")
    client.post("/api/auth/magic-link", json={"email": "ops@example.com"})
    token = client.post("/api/auth/verify", json={"token": last_link_token("ops@example.com")}).json()["token"]
    ops = {"Authorization": f"Bearer {token}"}
    detail = client.get(f"/api/admin/warehouses/{owner.warehouse_id}", headers=ops).json()
    assert detail["agreement"]["signed_current"] is True
    r = client.get(f"/api/admin/warehouses/{owner.warehouse_id}/agreement.pdf", headers=ops)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert client.get(f"/api/admin/warehouses/{owner.warehouse_id}/agreement.pdf", headers=owner.h).status_code == 403


def test_countersigner_is_stamped_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "agreement_countersigner_name", "Ryan Lee")
    monkeypatch.setattr(get_settings(), "agreement_countersigner_title", "Managing Member")
    details = agreement_svc.SignerDetails("Pat Owner", "Owner", "Acme", "1 Dock St", "v1", True)
    values = agreement_svc.field_values(details, agreement_svc.utcnow(), "UTC")
    assert values["autorack_name"] == "Ryan Lee" and values["autorack_title"] == "Managing Member"


def test_changed_pdf_without_new_version_is_refused(tmp_path, monkeypatch):
    for name in ("license-agreement-v1.json", "DancingScript-500.ttf"):
        (tmp_path / name).write_bytes((agreement_svc.LEGAL_DIR / name).read_bytes())
    (tmp_path / "license-agreement-v1.pdf").write_bytes(b"%PDF-1.3 edited")
    monkeypatch.setattr(agreement_svc, "LEGAL_DIR", tmp_path)
    agreement_svc.load.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="new version"):
            agreement_svc.load("v1")
    finally:
        agreement_svc.load.cache_clear()
