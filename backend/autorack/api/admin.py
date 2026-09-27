"""The operator's side: every warehouse at once.

For the people running Autorack (emails listed in OPERATOR_EMAILS): who signed
up, whose trial is ending, which pilots actually use it, which features get
used, and the photos workers take. This is the one place that crosses tenant
boundaries on purpose, so every route requires the operator check, and every
change is written to the target warehouse's audit log under the operator's
own name.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import UserContext, require_operator
from ..errors import ApiError, bad_request, not_found
from ..models import (
    AuditLog,
    Device,
    Membership,
    Order,
    OrderFlag,
    Photo,
    ScanEvent,
    ScanResult,
    SubscriptionStatus,
    User,
    Warehouse,
    Worker,
    utcnow,
)
from ..services import account as account_svc
from ..services import agreement as agreement_svc
from ..services import audit, email, jobs, onboarding, usage
from ..services import dashboard as dash
from ..services.access import evaluate
from ..services.audit import Actor
from .account import zip_response
from .orders import photo_response

router = APIRouter(prefix="/admin", tags=["admin"])
cron_router = APIRouter(tags=["admin"])


def _actor(uctx: UserContext) -> Actor:
    return Actor("operator", str(uctx.user.id), uctx.user.email, uctx.ip)


def _warehouse_rows(db: Session, whs: list[Warehouse]) -> list[dict[str, Any]]:
    if not whs:
        return []
    ids = [w.id for w in whs]
    now = utcnow()
    week = now - timedelta(days=7)
    is_error = ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick])
    scans = {
        wid: (int(n7), int(e7), last)
        for wid, n7, e7, last in db.execute(
            select(
                ScanEvent.warehouse_id,
                func.sum(case((ScanEvent.client_scanned_at >= week, 1), else_=0)),
                func.sum(case(((ScanEvent.client_scanned_at >= week) & is_error, 1), else_=0)),
                func.max(ScanEvent.client_scanned_at),
            )
            .where(ScanEvent.warehouse_id.in_(ids))
            .group_by(ScanEvent.warehouse_id)
        )
    }
    orders7 = {
        wid: int(n)
        for wid, n in db.execute(
            select(Order.warehouse_id, func.count())
            .where(Order.warehouse_id.in_(ids), Order.created_at >= week)
            .group_by(Order.warehouse_id)
        )
    }
    workers = {
        wid: int(n)
        for wid, n in db.execute(
            select(Worker.warehouse_id, func.count())
            .where(Worker.warehouse_id.in_(ids), Worker.active.is_(True))
            .group_by(Worker.warehouse_id)
        )
    }
    devices = {
        wid: int(n)
        for wid, n in db.execute(
            select(Device.warehouse_id, func.count())
            .where(Device.warehouse_id.in_(ids), Device.revoked_at.is_(None))
            .group_by(Device.warehouse_id)
        )
    }
    team = {
        wid: int(n)
        for wid, n in db.execute(
            select(Membership.warehouse_id, func.count())
            .where(Membership.warehouse_id.in_(ids), Membership.active.is_(True))
            .group_by(Membership.warehouse_id)
        )
    }
    photos = {
        wid: int(n)
        for wid, n in db.execute(
            select(Photo.warehouse_id, func.count()).where(Photo.warehouse_id.in_(ids)).group_by(Photo.warehouse_id)
        )
    }
    logins = {
        wid: last
        for wid, last in db.execute(
            select(Membership.warehouse_id, func.max(User.last_login_at))
            .join(User, User.id == Membership.user_id)
            .where(Membership.warehouse_id.in_(ids))
            .group_by(Membership.warehouse_id)
        )
    }
    out = []
    for w in whs:
        n7, e7, last_scan = scans.get(w.id, (0, 0, None))
        acc = evaluate(w, now)
        out.append(
            {
                "id": str(w.id),
                "name": w.name,
                "owner_email": w.owner_email,
                "status": w.subscription_status.value,
                "access": acc.state,
                "allowed": acc.allowed,
                "trial_ends_at": w.trial_ends_at.isoformat() if w.trial_ends_at else None,
                "trial_days_left": acc.trial_days_left,
                "created_at": w.created_at.isoformat(),
                "timezone": w.timezone,
                "scans_7d": n7,
                "errors_7d": e7,
                "orders_7d": orders7.get(w.id, 0),
                "last_scan_at": last_scan.isoformat() if last_scan else None,
                "last_login_at": logins[w.id].isoformat() if logins.get(w.id) else None,
                "workers": workers.get(w.id, 0),
                "phones": devices.get(w.id, 0),
                "team": team.get(w.id, 0),
                "photos": photos.get(w.id, 0),
                "has_stripe": bool(w.stripe_subscription_id),
                "closed_at": w.closed_at.isoformat() if w.closed_at else None,
                "deletion_due_at": w.deletion_due_at.isoformat() if w.deletion_due_at else None,
                "close_reason": w.close_reason,
                "closed_by": w.closed_by,
            }
        )
    return out


def health_label(row: dict[str, Any]) -> str:
    """At a glance: is this account alive?"""
    if row["scans_7d"] >= 50:
        return "active"
    if row["scans_7d"] > 0:
        return "light"
    if row["last_scan_at"]:
        return "idle"
    return "not_started"


@router.get("/overview")
def overview(uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)) -> dict[str, Any]:
    now = utcnow()
    whs = list(db.scalars(select(Warehouse).where(Warehouse.purged_at.is_(None)).order_by(Warehouse.created_at.desc())))
    rows = _warehouse_rows(db, whs)
    for r in rows:
        r["health"] = health_label(r)
    by_status: dict[str, int] = {}
    for w in whs:
        by_status[w.subscription_status.value] = by_status.get(w.subscription_status.value, 0) + 1
    day = timedelta(days=1)
    signups = []
    today = now.date()
    counts = {
        d: int(n)
        for d, n in db.execute(
            select(func.date(Warehouse.created_at), func.count())
            .where(Warehouse.created_at >= now - 30 * day)
            .group_by(func.date(Warehouse.created_at))
        )
    }
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        signups.append({"date": d.isoformat(), "count": counts.get(d, 0)})
    trials_ending = sorted(
        (
            r
            for r in rows
            if r["status"] == "trialing"
            and not r["closed_at"]
            and r["trial_days_left"] is not None
            and r["trial_days_left"] <= 7
        ),
        key=lambda r: r["trial_ends_at"] or "",
    )
    pilots = [r for r in rows if r["status"] == "pilot"]
    week = now - 7 * day
    scans_7d, errors_7d = db.execute(
        select(
            func.count(),
            func.coalesce(
                func.sum(case((ScanEvent.result.in_([ScanResult.mismatch, ScanResult.over_pick]), 1), else_=0)), 0
            ),
        ).where(ScanEvent.client_scanned_at >= week)
    ).one()
    return {
        "totals": {
            "warehouses": len(whs),
            "signups_7d": sum(1 for w in whs if w.created_at >= week),
            "signups_30d": sum(1 for w in whs if w.created_at >= now - 30 * day),
            "active_7d": sum(1 for r in rows if r["scans_7d"] > 0),
            "paying": by_status.get("active", 0),
            "mrr_cents": by_status.get("active", 0) * get_settings().plan_price_cents,
            "trials_ending_7d": len(trials_ending),
            "scans_7d": int(scans_7d),
            "errors_caught_7d": int(errors_7d),
        },
        "by_status": by_status,
        "signups": signups,
        "trials_ending": trials_ending,
        "pilots": pilots,
        "warehouses": rows,
        "generated_at": now.isoformat(),
    }


@router.get("/warehouses/{warehouse_id}")
def warehouse_detail(
    warehouse_id: uuid.UUID, uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> dict[str, Any]:
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        raise not_found("Warehouse not found")
    row = _warehouse_rows(db, [wh])[0]
    row["health"] = health_label(row)
    team = [
        {
            "email": u.email,
            "name": u.name,
            "role": m.role.value,
            "active": m.active and u.active,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        }
        for u, m in db.execute(
            select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.warehouse_id == wh.id)
            .order_by(Membership.created_at)
        )
    ]
    events = [
        {
            "at": a.created_at.isoformat(),
            "actor": a.actor_label or a.actor_type,
            "action": a.action,
            "details": a.details,
        }
        for a in db.scalars(
            select(AuditLog).where(AuditLog.warehouse_id == wh.id).order_by(AuditLog.id.desc()).limit(40)
        )
    ]
    return {
        **row,
        "settings": {
            "cost_per_error_cents": wh.cost_per_error_cents,
            "daily_summary_enabled": wh.daily_summary_enabled,
            "leaderboard_enabled": wh.leaderboard_enabled,
            "require_ship_scan": wh.require_ship_scan,
            "loose_match_enabled": wh.loose_match_enabled,
        },
        "onboarding": onboarding.checklist(db, wh),
        "agreement": {
            "signed_current": agreement_svc.is_signed(db, wh.id),
            "version": agreement_svc.CURRENT_VERSION,
            "signature": agreement_svc.signature_dict(agreement_svc.signature_for_any(db, wh.id)),
        },
        "summary": dash.summary(db, wh),
        "trend": dash.trend(db, wh, 14)["series"],
        "usage": [u for u in usage.totals(db, 30, wh.id) if u["count"]],
        "members": team,
        "photos": _photo_rows(db, warehouse_id=wh.id, limit=24),
        "events": events,
    }


class StatusIn(BaseModel):
    status: Literal["pilot", "trialing", "canceled"]
    trial_days: int | None = Field(default=None, ge=1, le=365)
    note: str | None = Field(default=None, max_length=300)


@router.post("/warehouses/{warehouse_id}/status")
def set_status(
    warehouse_id: uuid.UUID,
    body: StatusIn,
    uctx: UserContext = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Make a warehouse a free pilot, extend its trial, or cancel it.
    Paid subscriptions are Stripe's to change, not ours."""
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        raise not_found("Warehouse not found")
    if wh.stripe_subscription_id and wh.subscription_status in (
        SubscriptionStatus.active,
        SubscriptionStatus.past_due,
    ):
        raise bad_request("stripe_managed", "This warehouse pays through Stripe. Change it in the Stripe dashboard.")
    before = wh.subscription_status.value
    wh.subscription_status = SubscriptionStatus(body.status)
    if body.status == "trialing":
        wh.trial_ends_at = utcnow() + timedelta(days=body.trial_days or 14)
    audit.record(
        db,
        _actor(uctx),
        "warehouse.status_set",
        warehouse_id=wh.id,
        target_type="warehouse",
        target_id=wh.id,
        before=before,
        after=body.status,
        trial_days=body.trial_days,
        note=body.note,
    )
    db.commit()
    return warehouse_detail(warehouse_id, uctx, db)


@router.get("/warehouses/{warehouse_id}/agreement.pdf", response_class=Response)
def warehouse_agreement(
    warehouse_id: uuid.UUID, uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> Response:
    sig = agreement_svc.signature_for_any(db, warehouse_id)
    if not sig:
        raise not_found("This warehouse hasn't signed the agreement.")
    return Response(
        content=sig.signed_pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{agreement_svc.signed_filename(sig)}"',
            "Cache-Control": "private, no-store",
        },
    )


class CloseAccountIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def _wh(db: Session, warehouse_id: uuid.UUID) -> Warehouse:
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        raise not_found("Warehouse not found")
    return wh


@router.post("/warehouses/{warehouse_id}/close")
def close_warehouse(
    warehouse_id: uuid.UUID,
    body: CloseAccountIn,
    uctx: UserContext = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Termination by Autorack (Section 9): same effect as an owner closing."""
    account_svc.close(db, _wh(db, warehouse_id), _actor(uctx), reason=body.reason)
    db.commit()
    return warehouse_detail(warehouse_id, uctx, db)


@router.post("/warehouses/{warehouse_id}/reopen")
def reopen_warehouse(
    warehouse_id: uuid.UUID, uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> dict[str, Any]:
    account_svc.reopen(db, _wh(db, warehouse_id), _actor(uctx))
    db.commit()
    return warehouse_detail(warehouse_id, uctx, db)


class PurgeIn(BaseModel):
    confirm: str = Field(max_length=20)


@router.post("/warehouses/{warehouse_id}/purge")
def purge_warehouse(
    warehouse_id: uuid.UUID,
    body: PurgeIn,
    uctx: UserContext = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete a closed account's data now (e.g. the customer asked for it)
    instead of waiting for the retention date."""
    if body.confirm != "DELETE":
        raise bad_request("confirm_required", "Type DELETE to confirm.")
    counts = account_svc.purge(db, _wh(db, warehouse_id), _actor(uctx))
    db.commit()
    return {"deleted": counts}


@router.get("/warehouses/{warehouse_id}/export.zip")
def export_warehouse(
    warehouse_id: uuid.UUID, uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> Any:
    wh = _wh(db, warehouse_id)
    fh = account_svc.export_zip(db, wh)
    audit.record(db, _actor(uctx), "account.exported", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id)
    db.commit()
    return zip_response(fh, account_svc.export_filename(wh))


# ---------------------------------------------------------------------------
# Notices to customers (incidents, maintenance, policy changes)
# ---------------------------------------------------------------------------


class NoticeIn(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=10, max_length=10_000)
    warehouse_ids: list[uuid.UUID] | None = None  # None: every open account
    send: bool = False  # False: preview who would get it


@router.post("/notices")
def send_notice(
    body: NoticeIn, uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Email the owners of every account (or chosen ones): a security
    incident, planned maintenance, a change to the terms. Logged in each
    warehouse's activity log. With send=false, only counts recipients."""
    stmt = select(Warehouse).where(Warehouse.purged_at.is_(None))
    if body.warehouse_ids:
        stmt = stmt.where(Warehouse.id.in_(body.warehouse_ids))
    else:
        stmt = stmt.where(Warehouse.closed_at.is_(None))
    targets = [(wh, account_svc.owners(db, wh)) for wh in db.scalars(stmt.order_by(Warehouse.name))]
    recipients = sorted({addr for _, addrs in targets for addr in addrs})
    if not body.send:
        return {"warehouses": len(targets), "recipients": len(recipients), "sent": 0}
    paragraphs = [p.strip() for p in body.message.split("\n\n") if p.strip()]
    sent = failed = 0
    for addr in recipients:
        try:
            email.send(
                email.notice_email(
                    addr,
                    subject=body.subject,
                    heading=body.subject,
                    lines=paragraphs,
                    button_label="Open Autorack",
                    url=f"{get_settings().frontend_url.rstrip('/')}/app/",
                    footer="An important notice from Autorack about your account. Questions? Just reply.",
                )
            )
            sent += 1
        except email.EmailError:
            failed += 1
    actor = _actor(uctx)
    for wh, addrs in targets:
        audit.record(
            db,
            actor,
            "notice.sent",
            warehouse_id=wh.id,
            target_type="warehouse",
            target_id=wh.id,
            subject=body.subject,
            recipients=len(addrs),
        )
    audit.record(
        db,
        actor,
        "operator.notice",
        warehouse_id=None,
        subject=body.subject,
        message=body.message[:2000],
        warehouses=len(targets),
        recipients=len(recipients),
        failed=failed,
    )
    db.commit()
    return {"warehouses": len(targets), "recipients": len(recipients), "sent": sent, "failed": failed}


@router.get("/notices")
def list_notices(uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [
        {"at": a.created_at.isoformat(), "by": a.actor_label, **a.details}
        for a in db.scalars(
            select(AuditLog).where(AuditLog.action == "operator.notice").order_by(AuditLog.id.desc()).limit(50)
        )
    ]


@router.get("/usage")
def feature_usage(
    days: int = Query(30, ge=1, le=365), uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return {"days": days, "features": usage.totals(db, days)}


def _photo_rows(db: Session, warehouse_id: uuid.UUID | None = None, limit: int = 60) -> list[dict[str, Any]]:
    stmt = (
        select(
            Photo.id,
            Photo.created_at,
            Photo.size_bytes,
            Photo.warehouse_id,
            Warehouse.name,
            Worker.name,
            OrderFlag.reason,
            OrderFlag.note,
            OrderFlag.resolved_at,
            Order.external_order_number,
        )
        .join(Warehouse, Warehouse.id == Photo.warehouse_id)
        .join(OrderFlag, OrderFlag.id == Photo.flag_id)
        .join(Order, Order.id == Photo.order_id)
        .outerjoin(Worker, Worker.id == Photo.worker_id)
        .order_by(Photo.created_at.desc())
        .limit(limit)
    )
    if warehouse_id:
        stmt = stmt.where(Photo.warehouse_id == warehouse_id)
    return [
        {
            "id": str(pid),
            "at": at.isoformat(),
            "size_bytes": size,
            "warehouse_id": str(wid),
            "warehouse": wname,
            "worker": worker,
            "reason": reason.value,
            "note": note,
            "resolved": resolved is not None,
            "order_number": number,
        }
        for pid, at, size, wid, wname, worker, reason, note, resolved, number in db.execute(stmt)
    ]


@router.get("/photos")
def photos(
    warehouse_id: uuid.UUID | None = None,
    limit: int = Query(60, ge=1, le=200),
    uctx: UserContext = Depends(require_operator),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return _photo_rows(db, warehouse_id=warehouse_id, limit=limit)


@router.get("/photos/{photo_id}", response_class=Response)
def photo(
    photo_id: uuid.UUID, uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)
) -> Response:
    p = db.get(Photo, photo_id)
    if not p:
        raise not_found("Photo not found")
    return photo_response(p)


@router.get("/activity")
def activity(
    limit: int = Query(100, ge=1, le=500),
    q: str | None = Query(None, max_length=100),
    uctx: UserContext = Depends(require_operator),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Notable events across every warehouse: sign-ups, first logins,
    imports, billing changes. Not scans (see the per-warehouse numbers)."""
    notable = [
        "warehouse.created",
        "agreement.signed",
        "user.login",
        "orders.imported",
        "team.invited",
        "billing.status_changed",
        "warehouse.status_set",
        "account.closed",
        "account.reopened",
        "account.purged",
        "device.linked",
        "worker.created",
    ]
    stmt = (
        select(AuditLog, Warehouse.name)
        .outerjoin(Warehouse, Warehouse.id == AuditLog.warehouse_id)
        .where(AuditLog.action.in_(notable))
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Warehouse.name.ilike(like), AuditLog.actor_label.ilike(like)))
    return [
        {
            "at": a.created_at.isoformat(),
            "warehouse_id": str(a.warehouse_id) if a.warehouse_id else None,
            "warehouse": name,
            "actor": a.actor_label or a.actor_type,
            "action": a.action,
            "details": a.details,
        }
        for a, name in db.execute(stmt)
    ]


@router.post("/jobs/run")
def run_jobs_now(uctx: UserContext = Depends(require_operator), db: Session = Depends(get_db)) -> dict[str, Any]:
    return jobs.run_all(db)


@cron_router.post("/cron/run", include_in_schema=False)
def cron_run(
    x_cron_secret: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """For an external scheduler (cron-job.org, a GitHub Actions schedule):
    wakes a sleeping free-tier host and runs the jobs."""
    secret = get_settings().cron_secret
    if not secret:
        raise not_found("Not found")
    if not hmac.compare_digest(x_cron_secret.encode(), secret.encode()):
        raise ApiError(403, "forbidden", "Bad cron secret.")
    return jobs.run_all(db)
