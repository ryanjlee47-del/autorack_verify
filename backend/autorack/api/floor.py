"""Managing the floor from the dashboard: 3PL clients, pack inserts, restock
tasks, and the time clock."""

from __future__ import annotations

import contextlib
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, current_owner, require_manager
from ..downloads import attachment, csv_text
from ..errors import bad_request, conflict, not_found
from ..models import (
    Client,
    Membership,
    OwnerSession,
    PackInsert,
    Product,
    RestockTask,
    Shift,
    User,
    UserRole,
    Worker,
    utcnow,
)
from ..services import audit, client_billing, email, floor, monthly, usage
from ..services import auth as auth_svc
from ..services import dashboard as dash

router = APIRouter(tags=["floor"])


# ---------------------------------------------------------------------------
# Clients (3PL brands)
# ---------------------------------------------------------------------------


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    contact_email: EmailStr | None = None


class ClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    contact_email: EmailStr | None = None
    active: bool | None = None
    rates: dict[str, int] | None = None


class ClientUserIn(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=200)


def client_dict(c: Client) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "code": c.code,
        "contact_email": c.contact_email,
        "active": c.active,
        "rates": c.rates or {},
        "created_at": c.created_at.isoformat(),
    }


def _client(db: Session, ctx: OwnerContext, client_id: uuid.UUID) -> Client:
    c = db.scalar(select(Client).where(Client.id == client_id, Client.warehouse_id == ctx.warehouse.id))
    if not c:
        raise not_found("Client not found.")
    return c


def _name_taken(db: Session, ctx: OwnerContext, name: str, exclude: uuid.UUID | None = None) -> bool:
    stmt = select(Client.id).where(Client.warehouse_id == ctx.warehouse.id, func.lower(Client.name) == name.lower())
    if exclude:
        stmt = stmt.where(Client.id != exclude)
    return db.scalar(stmt) is not None


@router.get("/clients")
def list_clients(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Client).where(Client.warehouse_id == ctx.warehouse.id).order_by(func.lower(Client.name)))
    return [client_dict(c) for c in rows]


@router.post("/clients", status_code=201)
def create_client(
    body: ClientIn, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    name = body.name.strip()
    if _name_taken(db, ctx, name):
        raise conflict("client_exists", f"There's already a client called {name}.")
    c = Client(
        warehouse_id=ctx.warehouse.id,
        name=name,
        code=(body.code or "").strip() or None,
        contact_email=str(body.contact_email).lower() if body.contact_email else None,
    )
    db.add(c)
    db.flush()
    audit.record(db, ctx.actor, "client.created", warehouse_id=ctx.warehouse.id, target_type="client", target_id=c.id)
    db.commit()
    return client_dict(c)


@router.patch("/clients/{client_id}")
def update_client(
    client_id: uuid.UUID,
    body: ClientUpdate,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    c = _client(db, ctx, client_id)
    if body.name is not None:
        name = body.name.strip()
        if _name_taken(db, ctx, name, exclude=c.id):
            raise conflict("client_exists", f"There's already a client called {name}.")
        c.name = name
    if body.code is not None:
        c.code = body.code.strip() or None
    if body.contact_email is not None:
        c.contact_email = str(body.contact_email).lower()
    if body.active is not None:
        c.active = body.active
    if body.rates is not None:
        try:
            c.rates = client_billing.clean_rates(body.rates)
        except ValueError as e:
            raise bad_request("rates_invalid", str(e)) from None
    audit.record(db, ctx.actor, "client.updated", warehouse_id=ctx.warehouse.id, target_type="client", target_id=c.id)
    db.commit()
    return client_dict(c)


def _month(ctx: OwnerContext, month: str | None) -> tuple[int, int]:
    today = utcnow().astimezone(dash.tz_of(ctx.warehouse)).date()
    if not month:
        return today.year, today.month
    try:
        return monthly.parse_month(month, today)
    except ValueError:
        raise bad_request("month_invalid", "Months look like 2026-09.") from None


@router.get("/clients/{client_id}/statement")
def client_statement(
    client_id: uuid.UUID,
    month: str | None = Query(None, description="YYYY-MM; this month if left out"),
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    y, m = _month(ctx, month)
    return client_billing.statement(db, ctx.warehouse, _client(db, ctx, client_id), y, m)


@router.get("/clients/{client_id}/statement.csv", response_class=PlainTextResponse)
def client_statement_csv(
    client_id: uuid.UUID,
    month: str | None = Query(None),
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    y, m = _month(ctx, month)
    st = client_billing.statement(db, ctx.warehouse, _client(db, ctx, client_id), y, m)
    rows = [
        [
            st["client"]["name"],
            st["month"],
            line["label"],
            line["quantity"],
            f"{line['rate_cents'] / 100:.2f}",
            f"{line['amount_cents'] / 100:.2f}",
        ]
        for line in st["lines"]
    ]
    rows.append([st["client"]["name"], st["month"], "Total", "", "", f"{st['total_cents'] / 100:.2f}"])
    text = csv_text(["client", "month", "item", "quantity", "rate", "amount"], rows)
    slug = "".join(ch for ch in (st["client"]["code"] or st["client"]["name"]) if ch.isalnum())[:30] or "client"
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": attachment(f"autorack-{slug}-{st['month']}.csv")},
    )


@router.get("/billing/clients")
def all_statements(
    month: str | None = Query(None),
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Every client's total for the month, for invoicing day."""
    y, m = _month(ctx, month)
    rows = []
    for c in db.scalars(
        select(Client).where(Client.warehouse_id == ctx.warehouse.id).order_by(func.lower(Client.name))
    ):
        st = client_billing.statement(db, ctx.warehouse, c, y, m)
        counts = {line["key"]: line["quantity"] for line in st["lines"]}
        rows.append(
            {
                "client": st["client"],
                "orders": counts.get("per_order", 0),
                "units": counts.get("per_unit", 0),
                "returns": counts.get("per_return", 0),
                "total_cents": st["total_cents"],
                "rates_set": st["rates_set"],
            }
        )
    return {"month": f"{y:04d}-{m:02d}", "clients": rows, "rate_labels": {r.key: r.label for r in client_billing.RATES}}


# ---------------------------------------------------------------------------
# Client portal logins
# ---------------------------------------------------------------------------


def _client_login(u: User, m: Membership) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email,
        "name": u.name,
        "active": m.active and u.active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.get("/clients/{client_id}/users")
def list_client_users(
    client_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    c = _client(db, ctx, client_id)
    rows = db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.warehouse_id == ctx.warehouse.id, Membership.client_id == c.id)
        .order_by(Membership.created_at)
    )
    return [_client_login(u, m) for u, m in rows]


@router.post("/clients/{client_id}/users", status_code=201)
def add_client_user(
    client_id: uuid.UUID,
    body: ClientUserIn,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Give someone at the client a portal login: they sign in with Google
    and see only this client's orders, returns, reports and statements."""
    c = _client(db, ctx, client_id)
    addr = auth_svc.normalize_email(str(body.email))
    user = db.scalar(select(User).where(User.email == addr))
    if user is not None:
        existing = db.scalar(
            select(Membership).where(Membership.user_id == user.id, Membership.warehouse_id == ctx.warehouse.id)
        )
        if existing and existing.role != UserRole.client and existing.active:
            raise conflict("team_member", "That person is on your team. Use a different address for the portal.")
        if not user.active:
            raise conflict("account_disabled", "That account is disabled.")
    else:
        user = User(warehouse_id=ctx.warehouse.id, email=addr, name=(body.name or "").strip() or None)
        db.add(user)
        db.flush()
    m = auth_svc.add_membership(db, user, ctx.warehouse.id, UserRole.client)
    m.client_id, m.email_daily_summary, m.email_alerts = c.id, False, False
    audit.record(
        db,
        ctx.actor,
        "client.login_added",
        warehouse_id=ctx.warehouse.id,
        target_type="client",
        target_id=c.id,
        email=addr,
    )
    db.commit()
    url = f"{get_settings().frontend_url.rstrip('/')}/app/login.html"
    with contextlib.suppress(email.EmailError):
        email.send(email.portal_invite_email(addr, url, ctx.warehouse.name, c.name))
    return _client_login(user, m)


@router.delete("/clients/{client_id}/users/{user_id}", status_code=204)
def remove_client_user(
    client_id: uuid.UUID,
    user_id: uuid.UUID,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> Response:
    c = _client(db, ctx, client_id)
    m = db.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.warehouse_id == ctx.warehouse.id, Membership.client_id == c.id
        )
    )
    if not m:
        raise not_found("Login not found.")
    m.active = False
    for s in db.scalars(
        select(OwnerSession).where(
            OwnerSession.user_id == user_id,
            OwnerSession.revoked_at.is_(None),
            OwnerSession.warehouse_id == ctx.warehouse.id,
        )
    ):
        s.warehouse_id = None
    audit.record(
        db, ctx.actor, "client.login_removed", warehouse_id=ctx.warehouse.id, target_type="user", target_id=user_id
    )
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Pack inserts
# ---------------------------------------------------------------------------


class InsertIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    barcode: str | None = Field(default=None, max_length=200)
    scan_required: bool = False
    client_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None


class InsertUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    barcode: str | None = Field(default=None, max_length=200)
    scan_required: bool | None = None
    client_id: uuid.UUID | None = None
    clear_client: bool = False
    product_id: uuid.UUID | None = None
    clear_product: bool = False
    active: bool | None = None


def _insert(db: Session, ctx: OwnerContext, insert_id: uuid.UUID) -> PackInsert:
    i = db.scalar(select(PackInsert).where(PackInsert.id == insert_id, PackInsert.warehouse_id == ctx.warehouse.id))
    if not i:
        raise not_found("Insert not found.")
    return i


def _check_scope(db: Session, ctx: OwnerContext, client_id: uuid.UUID | None, product_id: uuid.UUID | None) -> None:
    if client_id:
        _client(db, ctx, client_id)
    if product_id and not db.scalar(
        select(Product.id).where(Product.id == product_id, Product.warehouse_id == ctx.warehouse.id)
    ):
        raise bad_request("product_invalid", "That product isn't in this warehouse's catalog.")


def _check_barcode(i: PackInsert) -> None:
    if i.scan_required and not i.normalized_barcode:
        raise bad_request("barcode_required", "Give the insert a barcode, or let packers tick it off by hand.")


def _bump_open_orders(db: Session, ctx: OwnerContext) -> None:
    """Phones cache the insert list with each order."""
    from ..models import Order, OrderKind, OrderStatus

    for o in db.scalars(
        select(Order).where(
            Order.warehouse_id == ctx.warehouse.id,
            Order.kind == OrderKind.pick,
            Order.status.in_(
                [OrderStatus.pending, OrderStatus.in_progress, OrderStatus.flagged, OrderStatus.completed]
            ),
        )
    ):
        o.version += 1


@router.get("/inserts")
def list_inserts(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(PackInsert).where(PackInsert.warehouse_id == ctx.warehouse.id).order_by(PackInsert.created_at)
    )
    return [floor.insert_dict(i) for i in rows]


@router.post("/inserts", status_code=201)
def create_insert(
    body: InsertIn, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _check_scope(db, ctx, body.client_id, body.product_id)
    barcode = (body.barcode or "").strip() or None
    i = PackInsert(
        warehouse_id=ctx.warehouse.id,
        name=body.name.strip(),
        barcode=barcode,
        normalized_barcode=floor.normalized(barcode),
        scan_required=body.scan_required,
        client_id=body.client_id,
        product_id=body.product_id,
    )
    _check_barcode(i)
    db.add(i)
    db.flush()
    _bump_open_orders(db, ctx)
    audit.record(db, ctx.actor, "insert.created", warehouse_id=ctx.warehouse.id, target_type="insert", target_id=i.id)
    db.commit()
    return floor.insert_dict(i)


@router.patch("/inserts/{insert_id}")
def update_insert(
    insert_id: uuid.UUID,
    body: InsertUpdate,
    ctx: OwnerContext = Depends(require_manager),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    i = _insert(db, ctx, insert_id)
    _check_scope(db, ctx, body.client_id, body.product_id)
    if body.name is not None:
        i.name = body.name.strip()
    if body.barcode is not None:
        i.barcode = body.barcode.strip() or None
        i.normalized_barcode = floor.normalized(i.barcode)
    if body.scan_required is not None:
        i.scan_required = body.scan_required
    if body.clear_client:
        i.client_id = None
    elif body.client_id:
        i.client_id = body.client_id
    if body.clear_product:
        i.product_id = None
    elif body.product_id:
        i.product_id = body.product_id
    if body.active is not None:
        i.active = body.active
    _check_barcode(i)
    _bump_open_orders(db, ctx)
    audit.record(db, ctx.actor, "insert.updated", warehouse_id=ctx.warehouse.id, target_type="insert", target_id=i.id)
    db.commit()
    return floor.insert_dict(i)


@router.delete("/inserts/{insert_id}", status_code=204)
def delete_insert(
    insert_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> Response:
    """Stops asking for it. Orders that already had it checked keep the record."""
    i = _insert(db, ctx, insert_id)
    i.active = False
    _bump_open_orders(db, ctx)
    audit.record(db, ctx.actor, "insert.retired", warehouse_id=ctx.warehouse.id, target_type="insert", target_id=i.id)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Restock tasks
# ---------------------------------------------------------------------------


@router.get("/restock")
def list_restock(
    status: str = Query("open", description="open | done | cancelled | all"),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(RestockTask).where(RestockTask.warehouse_id == ctx.warehouse.id)
    if status != "all":
        stmt = stmt.where(RestockTask.status == status)
    order = (
        (RestockTask.location.nulls_last(), RestockTask.created_at)
        if status == "open"
        else (RestockTask.created_at.desc(),)
    )
    workers = dash.worker_names(db, ctx.warehouse.id)
    return {"tasks": [floor.restock_dict(t, workers) for t in db.scalars(stmt.order_by(*order).limit(300))]}


def _restock(db: Session, ctx: OwnerContext, task_id: uuid.UUID) -> RestockTask:
    t = db.scalar(select(RestockTask).where(RestockTask.id == task_id, RestockTask.warehouse_id == ctx.warehouse.id))
    if not t:
        raise not_found("Restock task not found.")
    return t


@router.post("/restock/{task_id}/done")
def restock_done(
    task_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    t = _restock(db, ctx, task_id)
    if t.status == "open":
        t.status, t.done_at, t.done_by_user_id = "done", utcnow(), ctx.user.id
        db.commit()
    return floor.restock_dict(t, dash.worker_names(db, ctx.warehouse.id))


@router.post("/restock/{task_id}/cancel")
def restock_cancel(
    task_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    t = _restock(db, ctx, task_id)
    if t.status == "open":
        t.status, t.done_at, t.done_by_user_id = "cancelled", utcnow(), ctx.user.id
        db.commit()
    return floor.restock_dict(t, dash.worker_names(db, ctx.warehouse.id))


# ---------------------------------------------------------------------------
# Time clock
# ---------------------------------------------------------------------------


class ShiftUpdate(BaseModel):
    clock_in: datetime | None = None
    clock_out: datetime | None = None


def _range(ctx: OwnerContext, start: date | None, end: date | None) -> tuple[datetime, datetime]:
    today = utcnow().astimezone(dash.tz_of(ctx.warehouse)).date()
    end = end or today
    start = start or end - timedelta(days=6)
    if start > end or (end - start).days > 92:
        raise bad_request("range_invalid", "Pick a range of at most 3 months.")
    a, _, _ = dash.day_bounds(ctx.warehouse, start)
    _, b, _ = dash.day_bounds(ctx.warehouse, end)
    return a, b


def _shifts(db: Session, ctx: OwnerContext, a: datetime, b: datetime, worker_id: uuid.UUID | None) -> list[Shift]:
    stmt = select(Shift).where(Shift.warehouse_id == ctx.warehouse.id, Shift.clock_in < b)
    stmt = stmt.where((Shift.clock_out.is_(None)) | (Shift.clock_out > a))
    if worker_id:
        stmt = stmt.where(Shift.worker_id == worker_id)
    return list(db.scalars(stmt.order_by(Shift.clock_in)))


@router.get("/shifts")
def list_shifts(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    worker_id: uuid.UUID | None = None,
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    a, b = _range(ctx, start, end)
    workers = dash.worker_names(db, ctx.warehouse.id)
    rows = [floor.shift_dict(s, workers) for s in _shifts(db, ctx, a, b, worker_id)]
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["worker"] or "?"] = round(totals.get(r["worker"] or "?", 0) + r["hours"], 2)
    return {
        "enabled": ctx.warehouse.time_clock_enabled,
        "from": a.isoformat(),
        "to": b.isoformat(),
        "shifts": rows[::-1],
        "totals": [{"worker": k, "hours": v} for k, v in sorted(totals.items())],
    }


@router.patch("/shifts/{shift_id}")
def edit_shift(
    shift_id: uuid.UUID, body: ShiftUpdate, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """A manager fixes a forgotten clock-out (or a wrong clock-in). Recorded."""
    s = db.scalar(select(Shift).where(Shift.id == shift_id, Shift.warehouse_id == ctx.warehouse.id))
    if not s:
        raise not_found("Shift not found.")
    before = floor.shift_dict(s)
    if body.clock_in is not None:
        s.clock_in = body.clock_in
    if body.clock_out is not None:
        s.clock_out, s.closed_by = body.clock_out, "manager"
    if s.clock_out and s.clock_out <= s.clock_in:
        raise bad_request("shift_invalid", "Clock-out has to be after clock-in.")
    if s.clock_in > utcnow() or (s.clock_out and s.clock_out > utcnow() + timedelta(minutes=5)):
        raise bad_request("shift_invalid", "Shifts can't be in the future.")
    s.edited_by_user_id = ctx.user.id
    audit.record(
        db,
        ctx.actor,
        "shift.edited",
        warehouse_id=ctx.warehouse.id,
        target_type="shift",
        target_id=s.id,
        before={"clock_in": before["clock_in"], "clock_out": before["clock_out"]},
    )
    db.commit()
    return floor.shift_dict(s, dash.worker_names(db, ctx.warehouse.id))


@router.get("/exports/timesheet.csv", response_class=PlainTextResponse)
def timesheet_csv(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    ctx: OwnerContext = Depends(current_owner),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    a, b = _range(ctx, start, end)
    tz = dash.tz_of(ctx.warehouse)
    workers = {w.id: w for w in db.scalars(select(Worker).where(Worker.warehouse_id == ctx.warehouse.id))}
    rows = []
    for s in _shifts(db, ctx, a, b, None):
        d = floor.shift_dict(s)
        w = workers.get(s.worker_id)
        rows.append(
            [
                w.name if w else "",
                s.clock_in.astimezone(tz).date().isoformat(),
                s.clock_in.astimezone(tz).isoformat(timespec="minutes"),
                s.clock_out.astimezone(tz).isoformat(timespec="minutes") if s.clock_out else "(still on the clock)",
                d["hours"],
                s.closed_by or "",
                "yes" if d["edited"] else "",
            ]
        )
    text = csv_text(["worker", "date", "clock_in", "clock_out", "hours", "closed_by", "edited_by_manager"], rows)
    usage.track(db, ctx.warehouse.id, "exports.timesheet")
    db.commit()
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": attachment(f"autorack-timesheet-{a:%Y%m%d}.csv")},
    )
