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
from ..deps import OwnerContext, current_owner, require_owner_role
from ..errors import bad_request, conflict, not_found
from ..matching import MAX_SUFFIX_LEN, MIN_SUFFIX_LEN
from ..models import AuditLog, Order, OrderStatus, OwnerSession, User, UserRole, utcnow
from ..services import audit, email
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


def warehouse_dict(ctx: OwnerContext) -> dict[str, Any]:
    wh = ctx.warehouse
    return {
        "id": str(wh.id),
        "name": wh.name,
        "timezone": wh.timezone,
        "owner_email": wh.owner_email,
        "loose_match_enabled": wh.loose_match_enabled,
        "suffix_len": wh.suffix_len,
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


def member_dict(u: User) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email,
        "name": u.name,
        "role": u.role.value,
        "active": u.active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": u.created_at.isoformat(),
    }


@router.get("/team")
def list_team(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    users = db.scalars(select(User).where(User.warehouse_id == ctx.warehouse.id).order_by(User.created_at))
    return [member_dict(u) for u in users]


@router.post("/team", status_code=201)
def invite(
    body: InviteIn, ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    addr = auth_svc.normalize_email(str(body.email))
    if db.scalar(select(User).where(User.email == addr)):
        raise conflict("email_taken", "That email already belongs to an Autorack user.")
    user = User(warehouse_id=ctx.warehouse.id, email=addr, name=(body.name or "").strip() or None, role=body.role)
    db.add(user)
    db.flush()
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
    db.commit()
    # If sending fails they can still request a link from the sign-in page.
    with contextlib.suppress(email.EmailError):
        email.send(email.invite_email(addr, url, ctx.warehouse.name, ctx.user.name or ctx.user.email))
    return member_dict(user)


@router.patch("/team/{user_id}")
def update_member(
    user_id: uuid.UUID,
    body: MemberUpdate,
    ctx: OwnerContext = Depends(require_owner_role),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.id == user_id, User.warehouse_id == ctx.warehouse.id))
    if not user:
        raise not_found("Team member not found")
    demoting = body.role is not None and body.role != UserRole.owner and user.role == UserRole.owner
    deactivating = body.active is False and user.active and user.role == UserRole.owner
    if (demoting or deactivating) and auth_svc.active_owner_count(db, ctx.warehouse.id) <= 1:
        raise conflict("last_owner", "A warehouse needs at least one active owner.")
    if body.role is not None:
        user.role = body.role
    if body.active is not None:
        user.active = body.active
        if not body.active:
            for s in db.scalars(
                select(OwnerSession).where(OwnerSession.user_id == user.id, OwnerSession.revoked_at.is_(None))
            ):
                s.revoked_at = utcnow()
    if body.name is not None:
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
    return member_dict(user)


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
