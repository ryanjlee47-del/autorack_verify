"""Owner sign-up and sign-in: Sign in with Google (services/google_auth.py)."""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import timedelta
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, UserContext, client_ip, current_owner, current_user
from ..errors import ApiError, forbidden, not_found
from ..models import Client, Membership, OAuthState, User, utcnow
from ..names import plain_name
from ..security import hash_token
from ..services import agreement as agreement_svc
from ..services import audit, google_auth, ratelimit
from ..services import auth as auth_svc
from ..services.access import evaluate
from ..services.audit import Actor
from .legal import SignIn

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupIn(SignIn):
    """A new warehouse and its first owner, who signs the license agreement
    as part of creating the account: no account exists unsigned. Google
    supplies the email (see /auth/google/callback)."""

    warehouse_name: str = Field(min_length=1, max_length=200)
    timezone: str = "UTC"


class VerifyIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    # The random value the sign-in page kept when it sent the browser to
    # Google. Without it the code is useless to anyone who intercepts or
    # plants a sign-in link.
    nonce: str | None = Field(default=None, max_length=200)


def _google_or_503() -> None:
    if not get_settings().google_enabled:
        raise ApiError(
            503, "google_not_configured", "Sign in with Google isn't set up on this server yet (GOOGLE_CLIENT_ID)."
        )


def _to_google(url: str, state: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        google_auth.COOKIE,
        state,
        max_age=int(google_auth.STATE_TTL.total_seconds()),
        httponly=True,
        secure=get_settings().is_production,
        samesite="lax",
        path="/api/auth/google",
    )
    return resp


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    """Step 1 of sign-up: check everything, then send the browser to Google.
    The account is created when Google says who this is."""
    if not get_settings().signup_enabled:
        raise ApiError(403, "signup_closed", "Sign-ups are by invitation right now. Contact us to start a pilot.")
    _google_or_503()
    ip = client_ip(request)
    ratelimit.check_db(db, "signup_ip", ip or "?", 10, timedelta(hours=1), "Too many sign-ups from here. Try later.")
    plain_name(body.warehouse_name, "warehouse name")
    agreement_svc.clean(body.details())  # refuse before going anywhere
    tz = body.timezone if body.timezone in available_timezones() else "UTC"
    payload = {
        "warehouse_name": body.warehouse_name.strip(),
        "timezone": tz,
        "agreement": body.model_dump(
            include={
                "signer_name",
                "signer_title",
                "company_name",
                "company_address",
                "agreement_version",
                "accept_agreement",
                "viewed_seconds",
            }
        ),
    }
    # A ticket the browser carries to /google/start (a top-level visit to
    # this API, which can set the state cookie; this call may be cross-site).
    ticket = secrets.token_urlsafe(24)
    db.add(
        OAuthState(
            state_hash=hash_token(ticket),
            code_verifier="-",
            intent="signup_ticket",
            payload=payload,
            ip=ip,
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db.commit()
    return {"ok": True, "redirect": f"{get_settings().api_url}/api/auth/google/start?signup={ticket}"}


@router.get("/google/start", include_in_schema=False)
def google_start(
    request: Request,
    next: str | None = Query(default=None, max_length=200),
    signup: str | None = Query(default=None, max_length=64),
    nonce: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ip = client_ip(request)
    if not get_settings().google_enabled:
        return RedirectResponse(
            google_auth.error_url(
                google_auth.GoogleError("google_not_configured", "Sign in with Google isn't set up on this server yet.")
            ),
            status_code=302,
        )
    ratelimit.check_db(db, "google_start_ip", ip or "?", 60, timedelta(minutes=15), "Too many sign-in attempts.")
    if not nonce or len(nonce) < 16:
        # Only our sign-in page starts a sign-in: it adds a nonce it keeps.
        return RedirectResponse(
            google_auth.error_url(google_auth.GoogleError("expired", "Start again from the sign-in page.")), 302
        )
    intent, payload = "signin", {}
    if signup:
        try:
            ticket = google_auth.take_state(db, signup)
        except google_auth.GoogleError as err:
            db.commit()
            return RedirectResponse(google_auth.error_url(err), status_code=302)
        if ticket.intent != "signup_ticket":
            return RedirectResponse(
                google_auth.error_url(google_auth.GoogleError("expired", "Start the sign-up again.")), 302
            )
        intent, payload = "signup", ticket.payload
    url, state = google_auth.begin(
        db,
        intent=intent,
        payload=payload,
        next_path=next,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
        nonce=nonce,
    )
    db.commit()
    return _to_google(url, state)


@router.get("/google/callback", include_in_schema=False)
def google_callback(
    request: Request,
    code: str | None = Query(default=None, max_length=2000),
    state: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Google sends the browser here. Everything ends in a redirect to the
    sign-in page: with a one-time code, or with an error it explains."""
    ip = client_ip(request)
    email_seen: str | None = None

    def fail(err: google_auth.GoogleError) -> RedirectResponse:
        db.rollback()
        resp = RedirectResponse(google_auth.error_url(err, email_seen), status_code=302)
        resp.delete_cookie(google_auth.COOKIE, path="/api/auth/google")
        return resp

    try:
        if error:
            raise google_auth.GoogleError("cancelled", "Sign-in was cancelled.")
        cookie = request.cookies.get(google_auth.COOKIE)
        if not state or not code or not cookie or not hmac.compare_digest(cookie, state):
            raise google_auth.GoogleError(
                "state_mismatch", "That sign-in didn't start in this browser. Start again from the sign-in page."
            )
        attempt = google_auth.take_state(db, state)
        if attempt.intent not in ("signin", "signup"):
            raise google_auth.GoogleError("expired", "Start again from the sign-in page.")
        who = google_auth.exchange(code, attempt.code_verifier)
        email_seen = who.email
        user = db.scalar(select(User).where(User.email == who.email))
        if attempt.intent == "signup":
            if user is not None:
                raise google_auth.GoogleError(
                    "account_exists", f"{who.email} already has an Autorack account. Sign in instead."
                )
            user = _create_from_signup(db, attempt, who, ip)
        else:
            if user is None and who.email in get_settings().operator_email_set:
                user = User(warehouse_id=None, email=who.email)  # an operator's first sign-in
                db.add(user)
                db.flush()
            if user is None:
                raise google_auth.GoogleError(
                    "no_account",
                    f"No Autorack account uses {who.email}. Ask whoever runs your warehouse to invite this "
                    "address, or start a free trial.",
                )
            if not user.active:
                raise google_auth.GoogleError("account_disabled", "This account is disabled.")
        google_auth.bind(db, user, who)
        google_auth.audit_login(db, user, ip)
        code_out = google_auth.login_code(db, user, ip, str((attempt.payload or {}).get("nonce_hash") or ""))
        db.commit()
    except google_auth.GoogleError as err:
        return fail(err)
    except ApiError as err:  # e.g. the agreement was refused at sign-up
        detail = err.detail if isinstance(err.detail, dict) else {}
        return fail(google_auth.GoogleError(err.code, str(detail.get("message", "Sign-up failed."))))
    resp = RedirectResponse(google_auth.finish_url(code_out, attempt.next_path), status_code=302)
    resp.delete_cookie(google_auth.COOKIE, path="/api/auth/google")
    return resp


def _create_from_signup(db: Session, attempt: OAuthState, who: google_auth.GoogleIdentity, ip: str | None) -> User:
    p = attempt.payload or {}
    details = SignIn(**(p.get("agreement") or {})).details()
    wh, user = auth_svc.create_warehouse(
        db,
        name=str(p.get("warehouse_name") or "My warehouse"),
        owner_email=who.email,
        timezone=str(p.get("timezone") or "UTC"),
        actor=Actor("user", None, who.email, ip),
    )
    agreement_svc.sign(db, wh, user, details, ip=attempt.ip, user_agent=attempt.user_agent)
    return user


@router.post("/verify")
def verify(body: VerifyIn, request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    """Swap the one-time code from the Google callback for a session."""
    token, user = auth_svc.verify_magic_link(
        db, body.token, client_ip(request), request.headers.get("user-agent"), body.nonce
    )
    return {"token": token, "user": {"id": str(user.id), "email": user.email}}


@router.post("/logout")
def logout(uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    auth_svc.revoke_owner_session(db, uctx.session)
    return {"ok": True}


def _pending(db: Session, uctx: UserContext, warehouse_id: uuid.UUID) -> Membership:
    m = db.scalar(
        select(Membership).where(
            Membership.user_id == uctx.user.id,
            Membership.warehouse_id == warehouse_id,
            Membership.active.is_(True),
            Membership.pending.is_(True),
        )
    )
    if not m:
        raise not_found("There's no invitation to that warehouse.")
    return m


@router.post("/invitations/{warehouse_id}/accept")
def accept_invitation(
    warehouse_id: uuid.UUID, uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, bool]:
    """Someone added you to their warehouse: it appears once you say yes."""
    m = _pending(db, uctx, warehouse_id)
    m.pending = False
    audit.record(
        db,
        uctx.actor,
        "team.invitation_accepted",
        warehouse_id=warehouse_id,
        target_type="user",
        target_id=uctx.user.id,
    )
    db.commit()
    return {"ok": True}


@router.post("/invitations/{warehouse_id}/decline")
def decline_invitation(
    warehouse_id: uuid.UUID, uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)
) -> dict[str, bool]:
    m = _pending(db, uctx, warehouse_id)
    m.active = False
    audit.record(
        db,
        uctx.actor,
        "team.invitation_declined",
        warehouse_id=warehouse_id,
        target_type="user",
        target_id=uctx.user.id,
    )
    db.commit()
    return {"ok": True}


@router.post("/logout-all")
def logout_all(uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    """Sign out on every computer and phone, this one included."""
    n = auth_svc.revoke_all_sessions(db, uctx.user)
    audit.record(
        db,
        uctx.actor,
        "user.logout_all",
        warehouse_id=uctx.user.warehouse_id,
        target_type="user",
        target_id=uctx.user.id,
        sessions=n,
    )
    db.commit()
    return {"ok": True, "sessions_ended": n}


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
        "invitations": [
            {"warehouse_id": str(w.id), "warehouse": w.name, "role": m.role.value}
            for m, w in auth_svc.pending_invitations(db, user)
        ],
        "warehouse": None,
        "membership": None,
        "access": None,
        "agreement": None,
    }
    if current:
        wh, m = current
        acc = evaluate(wh)
        out["user"]["role"] = m.role.value  # type: ignore[index]
        out["agreement"] = {
            "required": not agreement_svc.is_signed(db, wh.id),
            "can_sign": m.role.value == "owner",
            "version": agreement_svc.CURRENT_VERSION,
        }
        if m.role.value == "client" and m.client_id:
            c = db.get(Client, m.client_id)
            out["client"] = {"id": str(c.id), "name": c.name} if c else None
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
            "time_clock_enabled": wh.time_clock_enabled,
            "onboarding_dismissed": wh.onboarding_dismissed,
            "cost_per_error_cents": wh.cost_per_error_cents,
            "closed_at": wh.closed_at.isoformat() if wh.closed_at else None,
            "deletion_due_at": wh.deletion_due_at.isoformat() if wh.deletion_due_at else None,
            "retention_days": get_settings().account_retention_days,
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
            Membership.pending.is_(False),
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
    """Another site under the same sign-in. Billed separately ($29/month
    each), so only an owner of the current warehouse can add one."""
    if not ctx.is_owner:
        raise forbidden("Only an owner can add a warehouse.")
    name = plain_name(body.name, "warehouse name")
    owned = auth_svc.owned_warehouses(db, ctx.user)
    if len(owned) >= get_settings().max_warehouses_per_user:
        raise forbidden("You've reached the number of warehouses one sign-in can have. Contact us to add more.")
    tz = body.timezone if body.timezone in available_timezones() else ctx.warehouse.timezone
    wh, _ = auth_svc.create_warehouse(
        db,
        name=name,
        owner_email=ctx.warehouse.owner_email,
        timezone=tz,
        actor=ctx.actor,
        user=ctx.user,
        # A free trial is for trying Autorack, once: an owner whose earlier
        # trial ran out unpaid doesn't get a fresh one by adding a site.
        trial=not auth_svc.has_lapsed_trial(owned),
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
