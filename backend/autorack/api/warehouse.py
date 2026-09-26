"""Warehouse settings, device-linking code, team members, activity log."""

from __future__ import annotations

import contextlib
import uuid
from typing import Any
from zoneinfo import available_timezones

import segno
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, current_owner, require_manager, require_owner_access, require_owner_role
from ..errors import bad_request, conflict, not_found
from ..matching import MAX_SUFFIX_LEN, MIN_SUFFIX_LEN
from ..models import AuditLog, Membership, Order, OrderStatus, OwnerSession, User, UserRole, utcnow
from ..services import audit, email, onboarding, usage
from ..services import auth as auth_svc

router = APIRouter(tags=["warehouse"])


def qr_svg(text: str) -> str:
    return segno.make(text, error="m").svg_inline(scale=1, border=2, omitsize=True)


def device_link_url(join_code: str) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/w/?link={join_code}"


class WarehouseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None
    loose_match_enabled: bool | None = None
    suffix_len: int | None = Field(default=None, ge=MIN_SUFFIX_LEN, le=MAX_SUFFIX_LEN)
    owner_email: EmailStr | None = None
    cost_per_error_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    daily_summary_enabled: bool | None = None
    daily_summary_hour: int | None = Field(default=None, ge=0, le=23)
    alert_on_flag: bool | None = None
    alert_error_rate: bool | None = None
    leaderboard_enabled: bool | None = None
    require_ship_scan: bool | None = None
    onboarding_dismissed: bool | None = None


SETTING_FIELDS = (
    "cost_per_error_cents",
    "daily_summary_enabled",
    "daily_summary_hour",
    "alert_on_flag",
    "alert_error_rate",
    "leaderboard_enabled",
    "require_ship_scan",
    "onboarding_dismissed",
)


def warehouse_dict(ctx: OwnerContext) -> dict[str, Any]:
    wh = ctx.warehouse
    return {
        "id": str(wh.id),
        "name": wh.name,
        "timezone": wh.timezone,
        "owner_email": wh.owner_email,
        "loose_match_enabled": wh.loose_match_enabled,
        "suffix_len": wh.suffix_len,
        **{f: getattr(wh, f) for f in SETTING_FIELDS},
        "created_at": wh.created_at.isoformat(),
    }


@router.get("/warehouse")
def get_warehouse(ctx: OwnerContext = Depends(current_owner)) -> dict[str, Any]:
    return warehouse_dict(ctx)


@router.patch("/warehouse")
def update_warehouse(
    body: WarehouseUpdate, ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    wh = ctx.warehouse
    changes = body.model_dump(exclude_none=True)
    if "timezone" in changes and changes["timezone"] not in available_timezones():
        raise bad_request("timezone_invalid", "Unknown timezone.")
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise bad_request("name_required", "Warehouse name can't be blank.")
    if "owner_email" in changes:
        changes["owner_email"] = auth_svc.normalize_email(str(changes["owner_email"]))
    for k, v in changes.items():
        setattr(wh, k, v)
    if {"loose_match_enabled", "suffix_len"} & changes.keys():
        # Matching rules changed: every cached order on every phone is stale.
        for order in db.scalars(
            select(Order).where(
                Order.warehouse_id == wh.id,
                Order.status.in_([OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged]),
            )
        ):
            order.version += 1
    audit.record(
        db,
        ctx.actor,
        "warehouse.updated",
        warehouse_id=wh.id,
        target_type="warehouse",
        target_id=wh.id,
        changes=changes,
    )
    usage.track(db, wh.id, "settings.update")
    db.commit()
    return warehouse_dict(ctx)


@router.get("/warehouse/device-link")
def device_link(ctx: OwnerContext = Depends(current_owner)) -> dict[str, Any]:
    url = device_link_url(ctx.warehouse.join_code)
    return {"join_code": ctx.warehouse.join_code, "url": url, "qr_svg": qr_svg(url)}


@router.post("/warehouse/device-link/rotate")
def rotate_device_link(
    ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    auth_svc.rotate_join_code(db, ctx.warehouse, ctx.actor)
    db.commit()
    url = device_link_url(ctx.warehouse.join_code)
    return {"join_code": ctx.warehouse.join_code, "url": url, "qr_svg": qr_svg(url)}


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


class InviteIn(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=200)
    role: UserRole = UserRole.manager


class MemberUpdate(BaseModel):
    role: UserRole | None = None
    active: bool | None = None
    name: str | None = Field(default=None, max_length=200)


def member_dict(u: User, m: Membership) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email,
        "name": u.name,
        "role": m.role.value,
        "active": m.active and u.active,
        "email_daily_summary": m.email_daily_summary,
        "email_alerts": m.email_alerts,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": m.created_at.isoformat(),
    }


@router.get("/team")
def list_team(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.warehouse_id == ctx.warehouse.id)
        .order_by(Membership.created_at)
    )
    return [member_dict(u, m) for u, m in rows]


@router.post("/team", status_code=201)
def invite(
    body: InviteIn, ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    addr = auth_svc.normalize_email(str(body.email))
    user = db.scalar(select(User).where(User.email == addr))
    if user is not None:
        existing = db.scalar(
            select(Membership).where(Membership.user_id == user.id, Membership.warehouse_id == ctx.warehouse.id)
        )
        if existing and existing.active:
            raise conflict("already_member", "That person is already on this warehouse's team.")
        if not user.active:
            raise conflict("account_disabled", "That account is disabled.")
    else:
        user = User(warehouse_id=ctx.warehouse.id, email=addr, name=(body.name or "").strip() or None)
        db.add(user)
        db.flush()
    membership = auth_svc.add_membership(db, user, ctx.warehouse.id, body.role)
    url = auth_svc.issue_magic_link(db, user, ctx.ip)
    audit.record(
        db,
        ctx.actor,
        "team.invited",
        warehouse_id=ctx.warehouse.id,
        target_type="user",
        target_id=user.id,
        email=addr,
        role=body.role.value,
    )
    usage.track(db, ctx.warehouse.id, "team.invite")
    db.commit()
    # If sending fails they can still request a link from the sign-in page.
    with contextlib.suppress(email.EmailError):
        email.send(email.invite_email(addr, url, ctx.warehouse.name, ctx.user.name or ctx.user.email))
    return member_dict(user, membership)


@router.patch("/team/{user_id}")
def update_member(
    user_id: uuid.UUID,
    body: MemberUpdate,
    ctx: OwnerContext = Depends(require_owner_role),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(User.id == user_id, Membership.warehouse_id == ctx.warehouse.id)
    ).first()
    if not row:
        raise not_found("Team member not found")
    user, m = row
    demoting = body.role is not None and body.role != UserRole.owner and m.role == UserRole.owner
    deactivating = body.active is False and m.active and m.role == UserRole.owner
    if (demoting or deactivating) and auth_svc.active_owner_count(db, ctx.warehouse.id) <= 1:
        raise conflict("last_owner", "A warehouse needs at least one active owner.")
    if body.role is not None:
        m.role = body.role
    if body.active is not None:
        m.active = body.active
        if not body.active:
            # Only sessions looking at this warehouse; they may run others.
            for s in db.scalars(
                select(OwnerSession).where(
                    OwnerSession.user_id == user.id,
                    OwnerSession.revoked_at.is_(None),
                    OwnerSession.warehouse_id == ctx.warehouse.id,
                )
            ):
                s.warehouse_id = None
            if not db.scalar(
                select(Membership.id).where(
                    Membership.user_id == user.id, Membership.active.is_(True), Membership.id != m.id
                )
            ):
                for s in db.scalars(
                    select(OwnerSession).where(OwnerSession.user_id == user.id, OwnerSession.revoked_at.is_(None))
                ):
                    s.revoked_at = utcnow()
    if body.name is not None and user.warehouse_id == ctx.warehouse.id:
        user.name = body.name.strip() or None
    audit.record(
        db,
        ctx.actor,
        "team.updated",
        warehouse_id=ctx.warehouse.id,
        target_type="user",
        target_id=user.id,
        changes=body.model_dump(exclude_none=True),
    )
    db.commit()
    return member_dict(user, m)


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


@router.get("/audit")
def audit_log(
    limit: int = Query(100, ge=1, le=500),
    before_id: int | None = None,
    ctx: OwnerContext = Depends(require_owner_role),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(AuditLog).where(AuditLog.warehouse_id == ctx.warehouse.id)
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit))
    return [
        {
            "id": r.id,
            "at": r.created_at.isoformat(),
            "actor_type": r.actor_type,
            "actor": r.actor_label,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "details": r.details,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Getting started
# ---------------------------------------------------------------------------


@router.get("/onboarding")
def onboarding_status(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
    return onboarding.checklist(db, ctx.warehouse)


@router.post("/onboarding/sample", status_code=201)
def load_sample_orders(
    ctx: OwnerContext = Depends(require_owner_access), db: Session = Depends(get_db)
) -> dict[str, Any]:
    result = onboarding.load_sample(db, ctx.warehouse, ctx.actor, ctx.user.id)
    return {**result, "checklist": onboarding.checklist(db, ctx.warehouse)}


@router.post("/onboarding/dismiss")
def dismiss_onboarding(ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    ctx.warehouse.onboarding_dismissed = True
    db.commit()
    return onboarding.checklist(db, ctx.warehouse)
