"""The license agreement: read it, sign it, download the signed copy."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import OwnerContext, current_member
from ..errors import forbidden, not_found
from ..services import agreement as agreement_svc

router = APIRouter(tags=["legal"])


class SignIn(BaseModel):
    signer_name: str = Field(min_length=1, max_length=200)
    signer_title: str = Field(min_length=1, max_length=200)
    company_name: str = Field(min_length=1, max_length=300)
    company_address: str = Field(min_length=1, max_length=500)
    agreement_version: str = Field(max_length=32)
    accept_agreement: bool
    viewed_seconds: int | None = Field(default=None, ge=0, le=86_400)

    def details(self) -> agreement_svc.SignerDetails:
        return agreement_svc.SignerDetails(
            signer_name=self.signer_name,
            signer_title=self.signer_title,
            company_name=self.company_name,
            company_address=self.company_address,
            agreement_version=self.agreement_version,
            accepted=self.accept_agreement,
            viewed_seconds=self.viewed_seconds,
        )


def pdf_response(data: bytes, filename: str, *, download: bool) -> Response:
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"', "Cache-Control": "private, no-store"},
    )


@router.get("/legal/agreement")
def agreement_info() -> dict[str, Any]:
    """Public: the current agreement, for the sign-up page."""
    return agreement_svc.current().public()


@router.get("/legal/agreement.pdf", response_class=Response)
def agreement_pdf() -> Response:
    doc = agreement_svc.current()
    return pdf_response(doc.pdf, f"Autorack-License-Agreement-{doc.version}.pdf", download=False)


def status(ctx: OwnerContext, db: Session) -> dict[str, Any]:
    current = agreement_svc.signature_for(db, ctx.warehouse.id)
    latest = current or agreement_svc.signature_for_any(db, ctx.warehouse.id)
    return {
        "required": current is None,
        "can_sign": ctx.is_owner,
        "version": agreement_svc.CURRENT_VERSION,
        "signature": agreement_svc.signature_dict(latest),
        "signed_current": current is not None,
    }


@router.get("/agreement")
def agreement_status(ctx: OwnerContext = Depends(current_member), db: Session = Depends(get_db)) -> dict[str, Any]:
    return status(ctx, db)


@router.post("/agreement/sign")
def sign_agreement(
    body: SignIn, request: Request, ctx: OwnerContext = Depends(current_member), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """For warehouses that existed before the agreement, and for new
    versions of it: an owner signs from the dashboard."""
    if not ctx.is_owner:
        raise forbidden("Only an owner of this warehouse can sign the license agreement.")
    if agreement_svc.is_signed(db, ctx.warehouse.id):
        return status(ctx, db)
    agreement_svc.sign(
        db, ctx.warehouse, ctx.user, body.details(), ip=ctx.ip, user_agent=request.headers.get("user-agent")
    )
    db.commit()
    return status(ctx, db)


@router.get("/agreement/signed.pdf", response_class=Response)
def signed_copy(ctx: OwnerContext = Depends(current_member), db: Session = Depends(get_db)) -> Response:
    sig = agreement_svc.signature_for_any(db, ctx.warehouse.id)
    if not sig:
        raise not_found("This warehouse hasn't signed the agreement yet.")
    return pdf_response(sig.signed_pdf, agreement_svc.signed_filename(sig), download=True)
