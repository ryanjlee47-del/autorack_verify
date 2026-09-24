"""Operator command line: `python -m autorack.cli <command>`.

The things an operator does by hand -- provision a pilot, send a sign-in link
to someone who lost theirs, flip a warehouse to a free pilot, tidy expired
tokens -- without SQL and without a separate admin app.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_sessionmaker
from .errors import ApiError
from .models import (
    MagicLinkToken,
    Order,
    OwnerSession,
    SubscriptionStatus,
    User,
    Warehouse,
    Worker,
    WorkerSession,
    utcnow,
)
from .services import audit, ratelimit
from .services import auth as auth_svc
from .services import orders as order_svc
from .services.access import evaluate
from .services.audit import OPERATOR


def _find_warehouse(db: Session, ref: str) -> Warehouse:
    wh = None
    try:
        wh = db.get(Warehouse, uuid.UUID(ref))
    except ValueError:
        user = db.scalar(select(User).where(User.email == ref.strip().lower()))
        wh = db.get(Warehouse, user.warehouse_id) if user else None
    if not wh:
        sys.exit(f"No warehouse matches {ref!r} (use its id or an owner's email).")
    return wh


def cmd_create_warehouse(db: Session, a: argparse.Namespace) -> None:
    status = SubscriptionStatus.pilot if a.pilot else SubscriptionStatus.trialing
    wh, user = auth_svc.create_warehouse(
        db, name=a.name, owner_email=a.email, timezone=a.timezone, status=status, actor=OPERATOR
    )
    url = auth_svc.issue_magic_link(db, user, None)
    db.commit()
    print(f"Created {wh.name} ({wh.id}), status={wh.subscription_status.value}")
    print(f"Device setup code: {wh.join_code}")
    print(f"One-time sign-in link for {user.email} (expires in {get_settings().magic_link_ttl_minutes} min):\n{url}")


def cmd_login_link(db: Session, a: argparse.Namespace) -> None:
    user = db.scalar(select(User).where(User.email == a.email.strip().lower()))
    if not user or not user.active:
        sys.exit("No active user with that email.")
    url = auth_svc.issue_magic_link(db, user, None)
    audit.record(
        db, OPERATOR, "user.login_link_issued", warehouse_id=user.warehouse_id, target_type="user", target_id=user.id
    )
    db.commit()
    print(url)


def cmd_set_status(db: Session, a: argparse.Namespace) -> None:
    wh = _find_warehouse(db, a.warehouse)
    before = wh.subscription_status
    wh.subscription_status = SubscriptionStatus(a.status)
    if a.status == "trialing":
        wh.trial_ends_at = utcnow() + timedelta(days=a.trial_days or get_settings().trial_days)
    audit.record(
        db,
        OPERATOR,
        "billing.status_set",
        warehouse_id=wh.id,
        target_type="warehouse",
        target_id=wh.id,
        before=before.value,
        after=a.status,
    )
    db.commit()
    print(f"{wh.name}: {before.value} -> {wh.subscription_status.value} ({evaluate(wh).state})")


def cmd_list(db: Session, a: argparse.Namespace) -> None:
    rows = db.execute(
        select(Warehouse, func.count(func.distinct(Order.id)), func.count(func.distinct(Worker.id)))
        .outerjoin(Order, Order.warehouse_id == Warehouse.id)
        .outerjoin(Worker, Worker.warehouse_id == Warehouse.id)
        .group_by(Warehouse.id)
        .order_by(Warehouse.created_at)
    )
    print(f"{'id':36}  {'status':10}  {'access':14}  {'orders':>6}  {'workers':>7}  name / owner")
    for wh, orders, workers in rows:
        print(
            f"{wh.id}  {wh.subscription_status.value:10}  {evaluate(wh).state:14}  {orders:>6}  {workers:>7}  "
            f"{wh.name} / {wh.owner_email}"
        )


def cmd_prune(db: Session, a: argparse.Namespace) -> None:
    now = utcnow()
    n_links = db.execute(delete(MagicLinkToken).where(MagicLinkToken.expires_at < now - timedelta(days=1))).rowcount  # type: ignore[attr-defined]
    n_owner = db.execute(delete(OwnerSession).where(OwnerSession.expires_at < now - timedelta(days=30))).rowcount  # type: ignore[attr-defined]
    n_rate = ratelimit.prune_db(db)
    db.commit()
    print(f"Pruned {n_links} magic links, {n_owner} owner sessions, {n_rate} rate-limit rows.")


DEMO_ORDERS = [
    (
        "SO-1001",
        [
            ("012345678905", 2, "Blue widget (12 pk)", "WID-BLU-12", "A-01-03"),
            ("036000291452", 1, "Packing tape 48mm", "TAPE-48", "B-04-01"),
        ],
    ),
    (
        "SO-1002",
        [
            ("04963406", 3, "Cable ties 8in", "TIE-8", "C-02-07"),
            ("0075678164125", 1, "Label roll 4x6", "LBL-46", "C-02-09"),
            ("VND-88213", 2, "Vendor pallet wrap", "WRAP-18", "D-10-01"),
        ],
    ),
    ("SO-1003", [("012345678905", 1, "Blue widget (12 pk)", "WID-BLU-12", "A-01-03")]),
    (
        "SO-1004",
        [
            ("(01)00012345678905(10)LOT42", 4, "Blue widget case", "WID-BLU-CS", "A-01-01"),
            ("036000291452", 2, "Packing tape 48mm", "TAPE-48", "B-04-01"),
        ],
    ),
]
DEMO_WORKERS = [("Maria", "2580"), ("Devon", "1470"), ("Sam", "3690")]


def cmd_seed_demo(db: Session, a: argparse.Namespace) -> None:
    if get_settings().is_production and not a.force:
        sys.exit("Refusing to seed demo data in production (pass --force if you really mean it).")
    email = a.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        sys.exit(f"{email} already exists; demo data was seeded before.")
    wh, user = auth_svc.create_warehouse(
        db,
        name="Dockside Demo Warehouse",
        owner_email=email,
        timezone=a.timezone,
        status=SubscriptionStatus.pilot,
        actor=OPERATOR,
    )
    for name, pin in DEMO_WORKERS:
        w = Worker(id=uuid.uuid4(), warehouse_id=wh.id, name=name, pin_hash="", pin_fingerprint="")
        auth_svc.set_worker_pin(db, w, pin)
        db.add(w)
    for number, lines in DEMO_ORDERS:
        order_svc.create_order(
            db,
            wh,
            external_order_number=number,
            lines=[order_svc.LineInput(b, q, sku, d, loc) for b, q, d, sku, loc in lines],
        )
    url = auth_svc.issue_magic_link(db, user, None)
    db.commit()
    s = get_settings()
    print(f"Demo warehouse ready: {wh.name}")
    print(f"  Owner sign-in link (one use, {s.magic_link_ttl_minutes} min): {url}")
    print(f"  Or request a new link any time for {email} (console email backend prints it).")
    print(f"  Phone setup: {s.frontend_url.rstrip('/')}/w/?link={wh.join_code}   (code {wh.join_code})")
    print("  Worker PINs: " + ", ".join(f"{n} {p}" for n, p in DEMO_WORKERS))


def cmd_end_sessions(db: Session, a: argparse.Namespace) -> None:
    wh = _find_warehouse(db, a.warehouse)
    for s in db.scalars(
        select(WorkerSession).where(WorkerSession.warehouse_id == wh.id, WorkerSession.ended_at.is_(None))
    ):
        s.ended_at = utcnow()
    audit.record(db, OPERATOR, "worker.sessions_ended", warehouse_id=wh.id, target_type="warehouse", target_id=wh.id)
    db.commit()
    print("Ended all worker sessions.")


def cmd_check_config(db: Session, a: argparse.Namespace) -> None:
    s = get_settings()
    problems = s.validate_for_production()
    print(f"environment={s.environment} email={s.email_backend} stripe={'on' if s.stripe_enabled else 'off'}")
    print(f"frontend_url={s.frontend_url} cors={s.cors_origin_list}")
    if problems:
        print("PROBLEMS:\n- " + "\n- ".join(problems))
        sys.exit(1)
    print("OK")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m autorack.cli", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    commands: dict[str, Callable[[Session, argparse.Namespace], None]] = {}

    def add(name: str, fn: Callable[[Session, argparse.Namespace], None], help: str) -> argparse.ArgumentParser:
        commands[name] = fn
        return sub.add_parser(name, help=help)

    c = add("create-warehouse", cmd_create_warehouse, "Provision a warehouse and its first owner")
    c.add_argument("--name", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--timezone", default="America/New_York")
    c.add_argument("--pilot", action="store_true", help="Free pilot instead of a trial")

    c = add("login-link", cmd_login_link, "Print a fresh one-time sign-in link for a user")
    c.add_argument("--email", required=True)

    c = add("set-status", cmd_set_status, "Set a warehouse's subscription status (e.g. pilot)")
    c.add_argument("warehouse", help="warehouse id or an owner's email")
    c.add_argument("--status", required=True, choices=[s.value for s in SubscriptionStatus])
    c.add_argument("--trial-days", type=int)

    add("list-warehouses", cmd_list, "List warehouses and their access state")
    add("prune", cmd_prune, "Delete expired tokens and old rate-limit rows (run daily)")
    c = add("end-sessions", cmd_end_sessions, "Sign every worker out of a warehouse")
    c.add_argument("warehouse")
    add("check-config", cmd_check_config, "Validate configuration for production")

    c = add("seed-demo", cmd_seed_demo, "Create a demo warehouse with workers and orders")
    c.add_argument("--email", default="owner@dockside-demo.test")
    c.add_argument("--timezone", default="America/New_York")
    c.add_argument("--force", action="store_true")

    a = p.parse_args(argv)
    db = get_sessionmaker()()
    try:
        commands[a.cmd](db, a)
    except ApiError as e:
        sys.exit(f"Error: {e.detail['message'] if isinstance(e.detail, dict) else e.detail}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
