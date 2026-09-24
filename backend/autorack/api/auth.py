"""Owner sign-up and sign-in (email magic link)."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, client_ip, current_owner
from ..errors import ApiError, bad_request
from ..services import auth as auth_svc
from ..services import email, ratelimit
from ..services.access import evaluate
from ..services.audit import Actor

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupIn(BaseModel):
    warehouse_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    timezone: str = "UTC"


class MagicLinkIn(BaseModel):
    email: EmailStr


class VerifyIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    if not get_settings().signup_enabled:
        raise ApiError(403, "signup_closed", "Sign-ups are by invitation right now. Contact us to start a pilot.")
    ip = client_ip(request)
    ratelimit.check_db(db, "signup_ip", ip or "?", 5, timedelta(hours=1), "Too many sign-ups from here. Try later.")
    tz = body.timezone if body.timezone in available_timezones() else "UTC"
    if not body.warehouse_name.strip():
        raise bad_request("name_required", "Enter your warehouse's name.")
    wh, user = auth_svc.create_warehouse(
        db,
        name=body.warehouse_name,
        owner_email=str(body.email),
        timezone=tz,
        actor=Actor("user", None, str(body.email), ip),
    )
    url = auth_svc.issue_magic_link(db, user, ip)
    db.commit()
    try:
        email.send(email.magic_link_email(user.email, url, wh.name))
    except email.EmailError:
        raise ApiError(
            503, "email_failed", "Your account was created, but we couldn't send the sign-in email. Try signing in."
        ) from None
    return {"ok": True, "message": f"Check {user.email} for your sign-in link."}


@router.post("/magic-link")
def magic_link(body: MagicLinkIn, request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    auth_svc.request_magic_link(db, str(body.email), client_ip(request))
    return {"ok": True, "message": "If that email has an account, a sign-in link is on its way."}


@router.post("/verify")
def verify(body: VerifyIn, request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    token, user = auth_svc.verify_magic_link(db, body.token, client_ip(request), request.headers.get("user-agent"))
    return {"token": token, "user": {"id": str(user.id), "email": user.email, "role": user.role.value}}


@router.post("/logout")
def logout(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> dict[str, bool]:
    auth_svc.revoke_owner_session(db, ctx.session)
    return {"ok": True}


@router.get("/me")
def me(ctx: OwnerContext = Depends(current_owner)) -> dict[str, object]:
    wh = ctx.warehouse
    acc = evaluate(wh)
    return {
        "user": {
            "id": str(ctx.user.id),
            "email": ctx.user.email,
            "name": ctx.user.name,
            "role": ctx.user.role.value,
        },
        "warehouse": {
            "id": str(wh.id),
            "name": wh.name,
            "timezone": wh.timezone,
            "subscription_status": wh.subscription_status.value,
        },
        "access": {
            "allowed": acc.allowed,
            "state": acc.state,
            "message": acc.message,
            "trial_days_left": acc.trial_days_left,
            "grace_ends_at": acc.grace_ends_at.isoformat() if acc.grace_ends_at else None,
        },
    }
