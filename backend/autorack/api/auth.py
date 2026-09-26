"""Owner sign-up and sign-in (email magic link)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, UserContext, client_ip, current_owner, current_user
from ..errors import ApiError, bad_request, forbidden
from ..models import Membership
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
    return {"token": token, "user": {"id": str(user.id), "email": user.email}}


@router.post("/logout")
def logout(uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    auth_svc.revoke_owner_session(db, uctx.session)
    return {"ok": True}


@router.get("/me")
def me(uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    """Who is signed in, which warehouse they're looking at, and which others
    they can switch to. `warehouse` is null for an operator with none."""
    user = uctx.user
    memberships = auth_svc.memberships_for(db, user)
    current = auth_svc.session_warehouse(db, user, uctx.session)
    out: dict[str, object] = {
        "user": {"id": str(user.id), "email": user.email, "name": user.name, "role": None},
        "is_operator": uctx.is_operator,
        "warehouses": [{"id": str(w.id), "name": w.name, "role": m.role.value} for m, w in memberships],
        "warehouse": None,
        "membership": None,
        "access": None,
    }
    if current:
        wh, m = current
        acc = evaluate(wh)
        out["user"]["role"] = m.role.value  # type: ignore[index]
        out["membership"] = {
            "role": m.role.value,
            "email_daily_summary": m.email_daily_summary,
            "email_alerts": m.email_alerts,
        }
        out["warehouse"] = {
            "id": str(wh.id),
            "name": wh.name,
            "timezone": wh.timezone,
            "subscription_status": wh.subscription_status.value,
            "leaderboard_enabled": wh.leaderboard_enabled,
            "onboarding_dismissed": wh.onboarding_dismissed,
            "cost_per_error_cents": wh.cost_per_error_cents,
        }
        out["access"] = {
            "allowed": acc.allowed,
            "state": acc.state,
            "message": acc.message,
            "trial_days_left": acc.trial_days_left,
            "grace_ends_at": acc.grace_ends_at.isoformat() if acc.grace_ends_at else None,
        }
    return out


class SwitchIn(BaseModel):
    warehouse_id: uuid.UUID


@router.post("/switch")
def switch_warehouse(
    body: SwitchIn, uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, object]:
    m = db.scalar(
        select(Membership).where(
            Membership.user_id == uctx.user.id,
            Membership.warehouse_id == body.warehouse_id,
            Membership.active.is_(True),
        )
    )
    if not m:
        raise forbidden("You don't have access to that warehouse.")
    uctx.session.warehouse_id = body.warehouse_id
    db.commit()
    return {"ok": True}


class NewWarehouseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str | None = None


@router.post("/warehouses", status_code=201)
def add_warehouse(
    body: NewWarehouseIn, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, object]:
    """Another site under the same sign-in. Billed separately ($175/month
    each), so only an owner of the current warehouse can add one."""
    if not ctx.is_owner:
        raise forbidden("Only an owner can add a warehouse.")
    if not body.name.strip():
        raise bad_request("name_required", "Enter the warehouse's name.")
    tz = body.timezone if body.timezone in available_timezones() else ctx.warehouse.timezone
    wh, _ = auth_svc.create_warehouse(
        db,
        name=body.name,
        owner_email=ctx.warehouse.owner_email,
        timezone=tz,
        actor=ctx.actor,
        user=ctx.user,
    )
    ctx.session.warehouse_id = wh.id
    db.commit()
    return {"id": str(wh.id), "name": wh.name}


class PreferencesIn(BaseModel):
    email_daily_summary: bool | None = None
    email_alerts: bool | None = None
    name: str | None = Field(default=None, max_length=200)


@router.patch("/preferences")
def preferences(
    body: PreferencesIn, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, object]:
    """Your own email settings for the warehouse you're looking at."""
    if body.email_daily_summary is not None:
        ctx.membership.email_daily_summary = body.email_daily_summary
    if body.email_alerts is not None:
        ctx.membership.email_alerts = body.email_alerts
    if body.name is not None:
        ctx.user.name = body.name.strip() or None
    db.commit()
    return {
        "email_daily_summary": ctx.membership.email_daily_summary,
        "email_alerts": ctx.membership.email_alerts,
        "name": ctx.user.name,
    }
