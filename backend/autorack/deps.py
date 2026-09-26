"""FastAPI dependencies: who is calling, and which warehouse they belong to.

Tenant isolation rule: route handlers never take a warehouse id from the
client. They get it from the authenticated principal here, and every query
filters on it. tests/test_tenant_isolation.py sweeps every route to hold that.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .errors import ApiError, forbidden, unauthorized
from .models import Device, Membership, OwnerSession, User, UserRole, Warehouse, Worker, WorkerSession
from .services import access as access_svc
from .services import auth as auth_svc
from .services.audit import Actor


def client_ip(request: Request) -> str | None:
    # uvicorn runs with --proxy-headers behind the host's load balancer, so
    # request.client is normally the real client address. Served over a unix
    # socket (PythonAnywhere) there is no peer address at all; fall back to
    # what the host's proxy forwarded, or every visitor would share one
    # rate-limit bucket.
    if request.client and request.client.host:
        return request.client.host
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real[:64]
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first[:64] or None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def is_operator(user: User) -> bool:
    return user.email.lower() in get_settings().operator_email_set


@dataclass
class UserContext:
    """Signed in, whatever warehouse (if any) they're looking at."""

    user: User
    session: OwnerSession
    ip: str | None

    @property
    def actor(self) -> Actor:
        return Actor("user", str(self.user.id), self.user.email, self.ip)

    @property
    def is_operator(self) -> bool:
        return is_operator(self.user)


def current_user(request: Request, db: Session = Depends(get_db)) -> UserContext:
    token = _bearer(request)
    if not token:
        raise unauthorized()
    resolved = auth_svc.resolve_owner_session(db, token)
    if not resolved:
        raise unauthorized()
    sess, user = resolved
    return UserContext(user=user, session=sess, ip=client_ip(request))


def require_operator(uctx: UserContext = Depends(current_user)) -> UserContext:
    if not uctx.is_operator:
        raise forbidden("This page is for Autorack staff.")
    return uctx


@dataclass
class OwnerContext:
    """A dashboard user acting on one warehouse, in one role."""

    user: User
    session: OwnerSession
    warehouse: Warehouse
    membership: Membership
    ip: str | None

    @property
    def actor(self) -> Actor:
        return Actor("user", str(self.user.id), self.user.email, self.ip)

    @property
    def role(self) -> UserRole:
        return self.membership.role

    @property
    def is_owner(self) -> bool:
        return self.role == UserRole.owner

    @property
    def can_manage(self) -> bool:
        """Create and edit orders, workers, phones and barcode rules."""
        return self.role in (UserRole.owner, UserRole.manager)


def current_owner(uctx: UserContext = Depends(current_user), db: Session = Depends(get_db)) -> OwnerContext:
    resolved = auth_svc.session_warehouse(db, uctx.user, uctx.session)
    if not resolved:
        raise forbidden("You don't have access to any warehouse.", "no_warehouse")
    wh, membership = resolved
    return OwnerContext(user=uctx.user, session=uctx.session, warehouse=wh, membership=membership, ip=uctx.ip)


def require_owner_role(ctx: OwnerContext = Depends(current_owner)) -> OwnerContext:
    if not ctx.is_owner:
        raise forbidden("Only an owner can do that.")
    return ctx


def require_manager(ctx: OwnerContext = Depends(current_owner)) -> OwnerContext:
    if not ctx.can_manage:
        raise forbidden("Supervisors can view and resolve problems, but not change this. Ask a manager.")
    return ctx


def require_owner_access(ctx: OwnerContext = Depends(require_manager)) -> OwnerContext:
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
