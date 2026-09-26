"""Owner (magic link) and worker (device + PIN) authentication."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError, bad_request, conflict, unauthorized
from ..models import (
    Device,
    MagicLinkToken,
    Membership,
    OwnerSession,
    SubscriptionStatus,
    User,
    UserRole,
    Warehouse,
    Worker,
    WorkerSession,
    utcnow,
)
from ..security import (
    generate_pin,
    hash_pin,
    hash_token,
    new_join_code,
    new_token,
    pin_fingerprint,
    verify_pin,
)
from . import audit, email, ratelimit
from .audit import Actor

log = logging.getLogger("autorack.auth")


def normalize_email(addr: str) -> str:
    return addr.strip().lower()


# ---------------------------------------------------------------------------
# Warehouses and owners
# ---------------------------------------------------------------------------


def create_warehouse(
    db: Session,
    *,
    name: str,
    owner_email: str,
    timezone: str = "UTC",
    status: SubscriptionStatus = SubscriptionStatus.trialing,
    actor: Actor,
    user: User | None = None,
) -> tuple[Warehouse, User]:
    """A new warehouse, owned by a new user, or by `user` (adding a site)."""
    owner_email = normalize_email(owner_email)
    if user is None and db.scalar(select(User).where(User.email == owner_email)):
        raise conflict("email_taken", "That email already has an Autorack account. Sign in instead.")
    s = get_settings()
    wh = Warehouse(
        name=name.strip(),
        owner_email=owner_email,
        timezone=timezone,
        subscription_status=status,
        trial_ends_at=utcnow() + timedelta(days=s.trial_days) if status == SubscriptionStatus.trialing else None,
        join_code=_unique_join_code(db),
    )
    db.add(wh)
    db.flush()
    if user is None:
        user = User(warehouse_id=wh.id, email=owner_email)
        db.add(user)
        db.flush()
    elif user.warehouse_id is None:
        user.warehouse_id = wh.id
    add_membership(db, user, wh.id, UserRole.owner)
    audit.record(
        db, actor, "warehouse.created", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id, name=wh.name
    )
    return wh, user


def add_membership(db: Session, user: User, warehouse_id: uuid.UUID, role: UserRole) -> Membership:
    m = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.warehouse_id == warehouse_id))
    if m is None:
        is_owner = role == UserRole.owner
        m = Membership(
            user_id=user.id,
            warehouse_id=warehouse_id,
            role=role,
            active=True,
            email_daily_summary=is_owner,
            email_alerts=role != UserRole.supervisor,
        )
        db.add(m)
    else:
        m.role = role
        m.active = True
    db.flush()
    return m


def memberships_for(db: Session, user: User) -> list[tuple[Membership, Warehouse]]:
    rows = db.execute(
        select(Membership, Warehouse)
        .join(Warehouse, Warehouse.id == Membership.warehouse_id)
        .where(Membership.user_id == user.id, Membership.active.is_(True))
        .order_by(Warehouse.name, Warehouse.created_at)
    )
    return [(m, w) for m, w in rows]


def session_warehouse(db: Session, user: User, sess: OwnerSession) -> tuple[Warehouse, Membership] | None:
    """The warehouse this session is looking at, if the user may still see it.

    Falls back to the home warehouse, then to any other: a user removed from
    the warehouse they were viewing lands somewhere they're allowed to be.
    """
    for wid in (sess.warehouse_id, user.warehouse_id):
        if wid is None:
            continue
        m = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id, Membership.warehouse_id == wid, Membership.active.is_(True)
            )
        )
        if m:
            wh = db.get(Warehouse, wid)
            if wh:
                return wh, m
    others = memberships_for(db, user)
    if not others:
        return None
    m, wh = others[0]
    return wh, m


def _unique_join_code(db: Session) -> str:
    for _ in range(20):
        code = new_join_code()
        if not db.scalar(select(Warehouse.id).where(Warehouse.join_code == code)):
            return code
    raise RuntimeError("Could not allocate a join code")


def rotate_join_code(db: Session, wh: Warehouse, actor: Actor) -> str:
    wh.join_code = _unique_join_code(db)
    audit.record(db, actor, "warehouse.join_code_rotated", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id)
    return wh.join_code


def issue_magic_link(db: Session, user: User, ip: str | None) -> str:
    """Create a single-use login token and return the URL that carries it.

    The token rides in the URL *fragment* (after #), which browsers never send
    to a server: it stays out of access logs, proxies and Referer headers.
    """
    s = get_settings()
    token = new_token()
    db.add(
        MagicLinkToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(minutes=s.magic_link_ttl_minutes),
            requested_ip=ip,
        )
    )
    db.flush()
    return f"{s.frontend_url.rstrip('/')}/app/login.html#token={quote(token)}"


def request_magic_link(db: Session, email_addr: str, ip: str | None) -> None:
    """Email a sign-in link if the address belongs to an active user.

    Always behaves the same from the outside, whether or not the address
    exists, so the endpoint cannot be used to discover customers.
    """
    addr = normalize_email(email_addr)
    ratelimit.check_db(
        db, "magic_link_ip", ip or "?", 20, timedelta(minutes=15), "Too many sign-in requests. Try again shortly."
    )
    ratelimit.check_db(
        db, "magic_link_email", addr, 5, timedelta(minutes=15), "Too many sign-in links requested for this email."
    )
    user = db.scalar(select(User).where(User.email == addr, User.active.is_(True)))
    if not user and addr in get_settings().operator_email_set and not db.scalar(select(User).where(User.email == addr)):
        # First sign-in of an operator who runs no warehouse themselves.
        user = User(warehouse_id=None, email=addr)
        db.add(user)
        db.flush()
    if not user:
        db.commit()  # keep the rate-limit hits
        return
    wh = db.get(Warehouse, user.warehouse_id) if user.warehouse_id else None
    url = issue_magic_link(db, user, ip)
    db.commit()
    try:
        email.send(email.magic_link_email(user.email, url, wh.name if wh else "Autorack"))
    except email.EmailError:
        log.exception("Failed to send magic link to %s", user.email)
        raise ApiError(503, "email_failed", "We couldn't send the email just now. Please try again.") from None


def verify_magic_link(db: Session, token: str, ip: str | None, user_agent: str | None) -> tuple[str, User]:
    ratelimit.check_db(
        db, "magic_verify_ip", ip or "?", 30, timedelta(minutes=15), "Too many attempts. Try again shortly."
    )
    now = utcnow()
    # Single-use enforced atomically: whichever request flips used_at wins.
    row = db.execute(
        update(MagicLinkToken)
        .where(
            MagicLinkToken.token_hash == hash_token(token),
            MagicLinkToken.used_at.is_(None),
            MagicLinkToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(MagicLinkToken.user_id)
    ).first()
    if not row:
        db.commit()
        raise unauthorized("This sign-in link is invalid, expired, or already used. Request a new one.", "link_invalid")
    user = db.get(User, row[0])
    if not user or not user.active:
        db.commit()
        raise unauthorized("This account is disabled.", "account_disabled")
    session_token = new_token()
    db.add(
        OwnerSession(
            user_id=user.id,
            token_hash=hash_token(session_token),
            expires_at=now + timedelta(days=get_settings().owner_session_days),
            last_seen_at=now,
            user_agent=(user_agent or "")[:300] or None,
            warehouse_id=user.warehouse_id,
        )
    )
    user.last_login_at = now
    audit.record(
        db,
        Actor("user", str(user.id), user.email, ip),
        "user.login",
        warehouse_id=user.warehouse_id,
        target_type="user",
        target_id=user.id,
    )
    db.commit()
    return session_token, user


def resolve_owner_session(db: Session, token: str) -> tuple[OwnerSession, User] | None:
    now = utcnow()
    sess = db.scalar(select(OwnerSession).where(OwnerSession.token_hash == hash_token(token)))
    if not sess or sess.revoked_at or sess.expires_at <= now:
        return None
    user = db.get(User, sess.user_id)
    if not user or not user.active:
        return None
    # Only write last_seen occasionally; the dashboard polls every few seconds.
    if not sess.last_seen_at or now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        db.commit()
    return sess, user


def revoke_owner_session(db: Session, sess: OwnerSession) -> None:
    sess.revoked_at = utcnow()
    db.commit()


# ---------------------------------------------------------------------------
# Devices and workers
# ---------------------------------------------------------------------------


def link_device(db: Session, join_code: str, label: str, ip: str | None, user_agent: str | None) -> tuple[str, Device]:
    ratelimit.check_db(
        db, "device_link_ip", ip or "?", 10, timedelta(minutes=15), "Too many attempts. Wait a few minutes."
    )
    wh = db.scalar(select(Warehouse).where(Warehouse.join_code == join_code))
    if not wh:
        db.commit()
        raise bad_request("join_code_invalid", "That setup code isn't valid. Check it with your manager.")
    token = new_token()
    device = Device(
        warehouse_id=wh.id,
        label=(label.strip() or "Phone")[:100],
        token_hash=hash_token(token),
        user_agent=(user_agent or "")[:300] or None,
        last_seen_at=utcnow(),
    )
    db.add(device)
    db.flush()
    audit.record(
        db,
        Actor("device", str(device.id), device.label, ip),
        "device.linked",
        warehouse_id=wh.id,
        target_type="device",
        target_id=device.id,
    )
    db.commit()
    return token, device


def resolve_device(db: Session, token: str) -> Device | None:
    device = db.scalar(select(Device).where(Device.token_hash == hash_token(token)))
    if not device or device.revoked_at:
        return None
    now = utcnow()
    if not device.last_seen_at or now - device.last_seen_at > timedelta(minutes=5):
        device.last_seen_at = now
        db.commit()
    return device


def _pin_lockout_check(db: Session, device: Device) -> None:
    s = get_settings()
    window = timedelta(minutes=s.pin_lockout_minutes)
    msg = f"Too many wrong PINs. Try again in {s.pin_lockout_minutes} minutes."
    if ratelimit.count_db(db, "pin_fail_device", str(device.id), window) >= s.pin_max_failures_per_device:
        raise ApiError(429, "pin_locked", msg, retry_after=int(window.total_seconds()))
    if ratelimit.count_db(db, "pin_fail_wh", str(device.warehouse_id), window) >= s.pin_max_failures_per_warehouse:
        raise ApiError(429, "pin_locked", msg, retry_after=int(window.total_seconds()))


def worker_login(db: Session, device: Device, pin: str, ip: str | None) -> tuple[str, WorkerSession, Worker]:
    _pin_lockout_check(db, device)
    worker = db.scalar(
        select(Worker).where(
            Worker.warehouse_id == device.warehouse_id,
            Worker.pin_fingerprint == pin_fingerprint(device.warehouse_id, pin),
            Worker.active.is_(True),
        )
    )
    if not worker or not verify_pin(pin, worker.pin_hash):
        ratelimit.hit_db(db, "pin_fail_device", str(device.id))
        ratelimit.hit_db(db, "pin_fail_wh", str(device.warehouse_id))
        audit.record(
            db,
            Actor("device", str(device.id), device.label, ip),
            "worker.login_failed",
            warehouse_id=device.warehouse_id,
            target_type="device",
            target_id=device.id,
        )
        db.commit()
        raise unauthorized("Wrong PIN.", "pin_invalid")
    ratelimit.clear_db(db, "pin_fail_device", str(device.id))
    token = new_token()
    sess = WorkerSession(
        warehouse_id=device.warehouse_id,
        worker_id=worker.id,
        device_id=device.id,
        token_hash=hash_token(token),
        expires_at=utcnow() + timedelta(hours=get_settings().worker_session_hours),
    )
    db.add(sess)
    db.flush()
    audit.record(
        db,
        Actor("worker", str(worker.id), worker.name, ip),
        "worker.login",
        warehouse_id=device.warehouse_id,
        target_type="device",
        target_id=device.id,
    )
    db.commit()
    return token, sess, worker


def resolve_worker_session(db: Session, token: str, device: Device) -> WorkerSession | None:
    sess = db.scalar(select(WorkerSession).where(WorkerSession.token_hash == hash_token(token)))
    if not sess or sess.device_id != device.id or sess.ended_at or sess.expires_at <= utcnow():
        return None
    worker = db.get(Worker, sess.worker_id)
    if not worker or not worker.active:
        return None
    return sess


# ---------------------------------------------------------------------------
# Worker records
# ---------------------------------------------------------------------------


def generate_unused_pin(db: Session, warehouse_id: uuid.UUID) -> str:
    for _ in range(200):
        pin = generate_pin()
        taken = db.scalar(
            select(Worker.id).where(
                Worker.warehouse_id == warehouse_id,
                Worker.pin_fingerprint == pin_fingerprint(warehouse_id, pin),
                Worker.active.is_(True),
            )
        )
        if not taken:
            return pin
    raise conflict("pins_exhausted", "Couldn't find a free PIN. Deactivate unused workers and try again.")


def set_worker_pin(db: Session, worker: Worker, pin: str) -> None:
    """Give `worker` this PIN, or raise 409 if an active colleague holds it."""
    fp = pin_fingerprint(worker.warehouse_id, pin)
    clash = db.scalar(
        select(Worker.id).where(
            Worker.warehouse_id == worker.warehouse_id,
            Worker.pin_fingerprint == fp,
            Worker.active.is_(True),
            Worker.id != worker.id,
        )
    )
    if clash:
        raise conflict("pin_taken", "Another worker at this warehouse already uses that PIN.")
    worker.pin_hash = hash_pin(pin)
    worker.pin_fingerprint = fp


def end_worker_sessions(db: Session, worker_id: uuid.UUID) -> None:
    db.execute(
        update(WorkerSession)
        .where(WorkerSession.worker_id == worker_id, WorkerSession.ended_at.is_(None))
        .values(ended_at=utcnow())
    )


def active_owner_count(db: Session, warehouse_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.warehouse_id == warehouse_id,
                Membership.role == UserRole.owner,
                Membership.active.is_(True),
                User.active.is_(True),
            )
        )
        or 0
    )
