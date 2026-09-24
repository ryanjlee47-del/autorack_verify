"""FastAPI dependencies: who is calling, and which warehouse they belong to.

Tenant isolation rule: route handlers never take a warehouse id from the
client. They get it from the authenticated principal here, and every query
filters on it. tests/test_tenant_isolation.py sweeps every route to hold that.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import get_db
from .errors import ApiError, forbidden, unauthorized
from .models import Device, OwnerSession, User, UserRole, Warehouse, Worker, WorkerSession
from .services import access as access_svc
from .services import auth as auth_svc
from .services.audit import Actor


def client_ip(request: Request) -> str | None:
    # uvicorn runs with --proxy-headers behind the host's load balancer, so
    # request.client is already the real client address.
    return request.client.host if request.client else None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


@dataclass
class OwnerContext:
    user: User
    session: OwnerSession
    warehouse: Warehouse
    ip: str | None

    @property
    def actor(self) -> Actor:
        return Actor("user", str(self.user.id), self.user.email, self.ip)

    @property
    def is_owner(self) -> bool:
        return self.user.role == UserRole.owner


def current_owner(request: Request, db: Session = Depends(get_db)) -> OwnerContext:
    token = _bearer(request)
    if not token:
        raise unauthorized()
    resolved = auth_svc.resolve_owner_session(db, token)
    if not resolved:
        raise unauthorized()
    sess, user = resolved
    wh = db.get(Warehouse, user.warehouse_id)
    if not wh:
        raise unauthorized()
    return OwnerContext(user=user, session=sess, warehouse=wh, ip=client_ip(request))


def require_owner_role(ctx: OwnerContext = Depends(current_owner)) -> OwnerContext:
    if not ctx.is_owner:
        raise forbidden("Only an owner can do that.")
    return ctx


def require_owner_access(ctx: OwnerContext = Depends(current_owner)) -> OwnerContext:
    """For creating new work (orders, imports): needs a live subscription."""
    _enforce_access(ctx.warehouse)
    return ctx


def _enforce_access(wh: Warehouse) -> None:
    acc = access_svc.evaluate(wh)
    if not acc.allowed:
        raise ApiError(402, "subscription_inactive", acc.message, state=acc.state)


@dataclass
class DeviceContext:
    device: Device
    warehouse: Warehouse
    ip: str | None


def current_device(request: Request, db: Session = Depends(get_db)) -> DeviceContext:
    token = request.headers.get("x-device-token", "").strip()
    if not token:
        raise unauthorized("This phone isn't linked to a warehouse.", "device_unlinked")
    device = auth_svc.resolve_device(db, token)
    if not device:
        raise unauthorized("This phone was unlinked. Ask your manager for the setup code.", "device_unlinked")
    wh = db.get(Warehouse, device.warehouse_id)
    if not wh:
        raise unauthorized("This phone isn't linked to a warehouse.", "device_unlinked")
    return DeviceContext(device=device, warehouse=wh, ip=client_ip(request))


@dataclass
class WorkerContext:
    device: Device
    warehouse: Warehouse
    session: WorkerSession
    worker: Worker
    ip: str | None

    @property
    def actor(self) -> Actor:
        return Actor("worker", str(self.worker.id), self.worker.name, self.ip)


def current_worker(
    request: Request, dctx: DeviceContext = Depends(current_device), db: Session = Depends(get_db)
) -> WorkerContext:
    token = _bearer(request)
    sess = auth_svc.resolve_worker_session(db, token, dctx.device) if token else None
    if not sess:
        raise unauthorized("Your shift session ended. Enter your PIN again.", "worker_session_expired")
    worker = db.get(Worker, sess.worker_id)
    assert worker is not None
    return WorkerContext(device=dctx.device, warehouse=dctx.warehouse, session=sess, worker=worker, ip=dctx.ip)


def require_worker_access(ctx: WorkerContext = Depends(current_worker)) -> WorkerContext:
    _enforce_access(ctx.warehouse)
    return ctx
