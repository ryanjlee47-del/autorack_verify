"""Workers (PIN holders) and linked devices, managed from the dashboard."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import OwnerContext, current_owner, require_manager
from ..errors import bad_request, not_found
from ..models import Device, Worker, WorkerSession, utcnow
from ..security import is_valid_pin
from ..services import audit
from ..services import auth as auth_svc
from ..services import dashboard as dash

router = APIRouter(tags=["people"])


class WorkerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    pin: str | None = Field(default=None, description="Leave empty to generate one")


class WorkerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    active: bool | None = None


class PinReset(BaseModel):
    pin: str | None = None


def worker_dict(w: Worker) -> dict[str, Any]:
    return {"id": str(w.id), "name": w.name, "active": w.active, "created_at": w.created_at.isoformat()}


def _get_worker(db: Session, ctx: OwnerContext, worker_id: uuid.UUID) -> Worker:
    w = db.scalar(select(Worker).where(Worker.id == worker_id, Worker.warehouse_id == ctx.warehouse.id))
    if not w:
        raise not_found("Worker not found")
    return w


def _choose_pin(requested: str) -> str:
    pin = requested.strip()
    if not is_valid_pin(pin):
        raise bad_request("pin_invalid", "PINs are exactly 4 digits.")
    return pin


@router.get("/workers")
def list_workers(
    days: int = Query(7, ge=1, le=365), ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    stats = dash.worker_stats(db, ctx.warehouse, days)
    workers = {str(w.id): w for w in db.scalars(select(Worker).where(Worker.warehouse_id == ctx.warehouse.id))}
    listed = {r["worker_id"] for r in stats["workers"]}
    rows = stats["workers"] + [
        {"worker_id": wid, "name": w.name, "active": w.active, "scans": 0}
        for wid, w in workers.items()
        if wid not in listed
    ]
    for r in rows:
        r["created_at"] = workers[r["worker_id"]].created_at.isoformat()
    return {**stats, "workers": rows}


@router.post("/workers", status_code=201)
def create_worker(
    body: WorkerCreate, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise bad_request("name_required", "Enter the worker's name.")
    w = Worker(id=uuid.uuid4(), warehouse_id=ctx.warehouse.id, name=name, pin_hash="", pin_fingerprint="")
    pin = _choose_pin(body.pin) if body.pin else auth_svc.generate_unused_pin(db, ctx.warehouse.id)
    auth_svc.set_worker_pin(db, w, pin)  # raises 409 before anything is written
    db.add(w)
    db.flush()
    audit.record(
        db, ctx.actor, "worker.created", warehouse_id=ctx.warehouse.id, target_type="worker", target_id=w.id, name=name
    )
    db.commit()
    # The PIN is shown exactly once. We only keep a hash.
    return {**worker_dict(w), "pin": pin}


@router.patch("/workers/{worker_id}")
def update_worker(
    worker_id: uuid.UUID,
    body: WorkerUpdate,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    w = _get_worker(db, ctx, worker_id)
    changes = body.model_dump(exclude_none=True)
    if "name" in changes:
        w.name = changes["name"].strip() or w.name
    reactivated_pin: str | None = None
    if "active" in changes and changes["active"] != w.active:
        if changes["active"]:
            # Their old PIN may have been reissued while they were inactive.
            w.active = True
            reactivated_pin = auth_svc.generate_unused_pin(db, ctx.warehouse.id)
            auth_svc.set_worker_pin(db, w, reactivated_pin)
        else:
            w.active = False
            auth_svc.end_worker_sessions(db, w.id)
    audit.record(
        db,
        ctx.actor,
        "worker.updated",
        warehouse_id=ctx.warehouse.id,
        target_type="worker",
        target_id=w.id,
        changes=changes,
    )
    db.commit()
    out = worker_dict(w)
    if reactivated_pin:
        out["pin"] = reactivated_pin
    return out


@router.post("/workers/{worker_id}/reset-pin")
def reset_pin(
    worker_id: uuid.UUID, body: PinReset, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    w = _get_worker(db, ctx, worker_id)
    if not w.active:
        raise bad_request("worker_inactive", "Reactivate this worker first.")
    pin = _choose_pin(body.pin) if body.pin else auth_svc.generate_unused_pin(db, ctx.warehouse.id)
    auth_svc.set_worker_pin(db, w, pin)
    auth_svc.end_worker_sessions(db, w.id)
    audit.record(db, ctx.actor, "worker.pin_reset", warehouse_id=ctx.warehouse.id, target_type="worker", target_id=w.id)
    db.commit()
    return {**worker_dict(w), "pin": pin}


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class DeviceUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=100)


def device_dict(d: Device, current_worker: str | None) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "label": d.label,
        "user_agent": d.user_agent,
        "created_at": d.created_at.isoformat(),
        "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
        "current_worker": current_worker,
    }


@router.get("/devices")
def list_devices(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    now = utcnow()
    signed_in: dict[uuid.UUID, str] = {
        device_id: name
        for device_id, name in db.execute(
            select(WorkerSession.device_id, Worker.name)
            .join(Worker, Worker.id == WorkerSession.worker_id)
            .where(
                WorkerSession.warehouse_id == ctx.warehouse.id,
                WorkerSession.ended_at.is_(None),
                WorkerSession.expires_at > now,
            )
            .order_by(WorkerSession.created_at)
        )
    }
    devices = db.scalars(
        select(Device)
        .where(Device.warehouse_id == ctx.warehouse.id)
        .order_by(Device.revoked_at.is_not(None), Device.created_at.desc())
    )
    return [device_dict(d, signed_in.get(d.id) if not d.revoked_at else None) for d in devices]


def _get_device(db: Session, ctx: OwnerContext, device_id: uuid.UUID) -> Device:
    d = db.scalar(select(Device).where(Device.id == device_id, Device.warehouse_id == ctx.warehouse.id))
    if not d:
        raise not_found("Device not found")
    return d


@router.patch("/devices/{device_id}")
def rename_device(
    device_id: uuid.UUID,
    body: DeviceUpdate,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    d = _get_device(db, ctx, device_id)
    d.label = body.label.strip() or d.label
    audit.record(
        db,
        ctx.actor,
        "device.renamed",
        warehouse_id=ctx.warehouse.id,
        target_type="device",
        target_id=d.id,
        label=d.label,
    )
    db.commit()
    return device_dict(d, None)


@router.post("/devices/{device_id}/revoke")
def revoke_device(
    device_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    d = _get_device(db, ctx, device_id)
    if not d.revoked_at:
        d.revoked_at = utcnow()
        for s in db.scalars(
            select(WorkerSession).where(WorkerSession.device_id == d.id, WorkerSession.ended_at.is_(None))
        ):
            s.ended_at = d.revoked_at
        audit.record(
            db, ctx.actor, "device.revoked", warehouse_id=ctx.warehouse.id, target_type="device", target_id=d.id
        )
        db.commit()
    return device_dict(d, None)
