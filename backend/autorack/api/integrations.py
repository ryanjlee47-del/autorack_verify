"""Connections: stores and spreadsheet links that feed orders in, the import
address (email + drop URL), and the public endpoints those two post to."""

from __future__ import annotations

import base64
import contextlib
import hmac
import re
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, client_ip, current_owner, require_manager, require_owner_role
from ..errors import ApiError, bad_request, not_found
from ..models import IntegrationKind, Membership, Order, OrderSource, User, utcnow
from ..services import access, email, integrations
from ..services.ratelimit import memory_limiter

router = APIRouter(tags=["integrations"])
inbound_router = APIRouter(prefix="/inbound", tags=["inbound"])


class ConnectBody(BaseModel):
    kind: IntegrationKind
    push_tracking: bool = True
    shop: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    api_secret: str | None = Field(default=None, max_length=500)
    store_url: str | None = Field(default=None, max_length=500)
    consumer_key: str | None = Field(default=None, max_length=500)
    consumer_secret: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2000)


CREDENTIAL_FIELDS = ("shop", "token", "api_key", "api_secret", "store_url", "consumer_key", "consumer_secret", "url")


class IntegrationUpdate(BaseModel):
    enabled: bool | None = None
    push_tracking: bool | None = None
    sync_minutes: int | None = Field(default=None, ge=5, le=1440)
    # Reconnect with new credentials (same fields as connecting).
    shop: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    api_secret: str | None = Field(default=None, max_length=500)
    store_url: str | None = Field(default=None, max_length=500)
    consumer_key: str | None = Field(default=None, max_length=500)
    consumer_secret: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2000)


def _require_access(ctx: OwnerContext) -> None:
    acc = access.evaluate(ctx.warehouse)
    if not acc.allowed:
        raise ApiError(402, "subscription_inactive", acc.message, state=acc.state)


@router.get("/integrations")
def list_integrations(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
    now = utcnow()
    return {
        "connections": [integrations.integration_dict(i, now) for i in integrations.active_for(db, ctx.warehouse.id)],
        "import_address": integrations.import_address(ctx.warehouse) if ctx.can_manage else None,
    }


@router.post("/integrations", status_code=201)
def connect(
    body: ConnectBody, ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_access(ctx)
    memory_limiter.check(f"connect:{ctx.warehouse.id}", 20, 3600, "Too many connection attempts. Try again later.")
    fields = body.model_dump(include=set(CREDENTIAL_FIELDS))
    integ, _ = integrations.connect(
        db,
        ctx.warehouse,
        body.kind,
        fields,
        push_tracking=body.push_tracking,
        actor=ctx.actor,
        user_id=ctx.user.id,
    )
    db.commit()
    # First pull straight away, so the owner sees their orders arrive.
    result = integrations.sync_one(db, integ)
    if result.get("ok") and integrations.products_due(integ, utcnow()):
        result["products"] = integrations.sync_products(db, integ)
    return {"connection": integrations.integration_dict(integ), "sync": result}


@router.patch("/integrations/{integration_id}")
def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationUpdate,
    ctx: OwnerContext = Depends(require_owner_role),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    integ = integrations.get(db, ctx.warehouse.id, integration_id)
    creds = {k: v for k, v in body.model_dump(include=set(CREDENTIAL_FIELDS)).items() if v}
    if creds:
        integrations.update_credentials(db, integ, creds, ctx.actor)
    if body.enabled is not None:
        integ.enabled = body.enabled
        if body.enabled:
            integ.failures = 0
            integ.last_error = None
    if body.push_tracking is not None:
        integ.push_tracking = body.push_tracking and integ.kind != IntegrationKind.sheet
    if body.sync_minutes is not None:
        integ.sync_minutes = body.sync_minutes
    from ..services import audit

    audit.record(
        db,
        ctx.actor,
        "integration.updated",
        warehouse_id=ctx.warehouse.id,
        target_type="integration",
        target_id=integ.id,
        changes=sorted(k for k, v in body.model_dump(exclude=set(CREDENTIAL_FIELDS)).items() if v is not None)
        + (["credentials"] if creds else []),
    )
    db.commit()
    return integrations.integration_dict(integ)


@router.delete("/integrations/{integration_id}", status_code=204)
def disconnect(
    integration_id: uuid.UUID, ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> None:
    integrations.disconnect(db, integrations.get(db, ctx.warehouse.id, integration_id), ctx.actor)
    db.commit()


@router.post("/integrations/{integration_id}/sync")
def sync_now(
    integration_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_access(ctx)
    integ = integrations.get(db, ctx.warehouse.id, integration_id)
    if integ.last_sync_at and utcnow() - integ.last_sync_at < timedelta(seconds=20):
        raise ApiError(429, "too_soon", "It just synced. Give it a few seconds.")
    result = integrations.sync_one(db, integ)
    return {"connection": integrations.integration_dict(integ), "sync": result}


@router.post("/integrations/{integration_id}/products")
def import_store_products(
    integration_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Pull the store's products (names, SKUs, barcodes, pictures) into the
    catalog now. It also happens by itself once a day."""
    _require_access(ctx)
    integ = integrations.get(db, ctx.warehouse.id, integration_id)
    if integ.kind == IntegrationKind.sheet:
        raise bad_request("no_products", "A spreadsheet link has no product catalog.")
    memory_limiter.check(f"products:{integ.id}", 6, 3600, "Products were just imported. Try again later.")
    return integrations.sync_products(db, integ)


@router.post("/integrations/import-address")
def import_address(ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create (once) and show the warehouse's import email and drop URL."""
    integrations.ensure_import_token(db, ctx.warehouse)
    db.commit()
    return integrations.import_address(ctx.warehouse)


@router.post("/integrations/import-address/rotate")
def rotate_import_address(
    ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    integrations.rotate_import_token(db, ctx.warehouse, ctx.actor)
    db.commit()
    return integrations.import_address(ctx.warehouse)


@router.post("/orders/{order_id}/push-tracking")
def retry_push(
    order_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = db.scalar(select(Order).where(Order.id == order_id, Order.warehouse_id == ctx.warehouse.id))
    if not order:
        raise not_found("Order not found")
    if not order.integration_id or not order.tracking_number:
        raise bad_request("nothing_to_push", "This order didn't come from a connected store, or hasn't shipped.")
    if order.tracking_push_status == "done":
        return {"ok": True, "result": "already sent"}
    order.tracking_push_attempts = 0
    return integrations.push_one(db, order)


# ---------------------------------------------------------------------------
# Public: where CSVs arrive by themselves
# ---------------------------------------------------------------------------


def _batch_result(batch: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "orders_created": batch.orders_created,
        "orders_skipped": batch.orders_skipped,
        "warnings": list(batch.warnings or [])[:20],
    }


@inbound_router.post("/drop")
async def drop(
    request: Request, x_import_key: str = Header(default="", max_length=64), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Post a CSV here (multipart field `file`, or the raw CSV as the body),
    with the warehouse's import key in the X-Import-Key header. Used by the
    watched-folder script, Zapier, cron + curl... The key travels in a
    header, not the URL, so it doesn't end up in proxy and access logs."""
    token = x_import_key
    memory_limiter.check(f"drop-ip:{client_ip(request)}", 120, 3600, "Too many uploads. Try again later.")
    wh = integrations.warehouse_for_token(db, token)
    if not wh:
        raise not_found("Unknown import address. Copy it again from Autorack → Connections.")
    memory_limiter.check(f"drop:{wh.id}", 60, 3600, "Too many uploads. Try again later.")
    ctype = request.headers.get("content-type", "")
    filename = request.headers.get("x-filename") or "upload.csv"
    max_bytes = get_settings().max_import_bytes
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile) and not hasattr(upload, "read"):
            raise bad_request("file_required", "Send the CSV in a form field named 'file'.")
        content = await upload.read(max_bytes + 1)  # type: ignore[union-attr]
        filename = getattr(upload, "filename", None) or filename
    else:
        content = await request.body()
    if len(content) > max_bytes:
        raise bad_request("file_too_large", "That file is too large.")
    batch = integrations.import_file(db, wh, content, filename[:200], OrderSource.drop, "import URL")
    return _batch_result(batch)


TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def _recipients(payload: dict[str, Any]) -> list[str]:
    """Every address this email was sent to, across providers' formats."""
    out: list[str] = []
    for key in ("OriginalRecipient", "To", "Cc", "recipient", "to", "envelope"):
        v = payload.get(key)
        if isinstance(v, str):
            out.append(v)
    for key in ("ToFull", "CcFull"):
        for r in payload.get(key) or []:
            if isinstance(r, dict) and r.get("Email"):
                out.append(str(r["Email"]))
    return out


def _sender(payload: dict[str, Any]) -> str:
    v = payload.get("From") or payload.get("from") or payload.get("sender") or ""
    m = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", str(v))
    return m.group(0).lower() if m else ""


def _csv_attachments(payload: dict[str, Any], files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    found = [(n, c) for n, c in files if n.lower().endswith((".csv", ".txt", ".tsv"))]
    for a in payload.get("Attachments") or []:  # Postmark
        name = str(a.get("Name") or "")
        if name.lower().endswith((".csv", ".txt", ".tsv")) or "csv" in str(a.get("ContentType", "")):
            try:
                found.append((name or "attachment.csv", base64.b64decode(a.get("Content") or "")))
            except ValueError:
                continue
    return found


def _basic_password(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not value:
        return ""
    try:
        return base64.b64decode(value.strip()).decode("utf-8", "replace").partition(":")[2]
    except ValueError:
        return ""


@inbound_router.post("/email")
async def inbound_email(
    request: Request, key: str = Query(default=""), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Webhook for an inbound-email provider (Postmark JSON, or Mailgun /
    SendGrid multipart). Always answers 200 for well-authenticated calls, so
    the provider doesn't retry an email that can never be imported."""
    s = get_settings()
    # The secret belongs in a header (X-Inbound-Key) or HTTP Basic auth
    # (https://inbound:SECRET@host/..., which Postmark and Mailgun support):
    # both stay out of access logs. ?key= still works for providers that
    # can only call a bare URL.
    presented = request.headers.get("x-inbound-key") or _basic_password(request) or key
    if not s.inbound_email_enabled or not hmac.compare_digest(presented.encode(), s.inbound_email_secret.encode()):
        raise not_found()
    payload: dict[str, Any] = {}
    files: list[tuple[str, bytes]] = []
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        payload = dict(await request.json())
    else:
        form = await request.form(max_part_size=s.max_import_bytes)
        for k, v in form.multi_items():
            if hasattr(v, "read"):
                files.append((getattr(v, "filename", "") or k, await v.read(s.max_import_bytes + 1)))  # type: ignore[union-attr]
            else:
                payload.setdefault(k, str(v))
    wh = None
    for addr in _recipients(payload):
        for tok in TOKEN_RE.findall(addr.lower()):
            wh = integrations.warehouse_for_token(db, tok)
            if wh:
                break
        if wh:
            break
    if not wh:
        return {"ok": False, "reason": "unknown address"}
    sender = _sender(payload)
    attachments = [(n, c) for n, c in _csv_attachments(payload, files) if len(c) <= s.max_import_bytes]
    results: list[str] = []
    for name, content in attachments[:5]:
        try:
            batch = integrations.import_file(db, wh, content, name[:200], OrderSource.email, f"email from {sender}")
            results.append(
                f"{name}: {batch.orders_created} new order(s)"
                + (f", {batch.orders_skipped} already in Autorack" if batch.orders_skipped else "")
            )
        except ApiError as exc:
            db.rollback()
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.append(f"{name}: not imported. {detail.get('message', exc.code)}")
    if not attachments:
        results.append("No CSV file was attached, so nothing was imported.")
    # Tell the sender how it went -- only if they're on this warehouse's team,
    # so a forged From can't make us email strangers.
    is_member = sender and db.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.warehouse_id == wh.id,
            Membership.active.is_(True),
            Membership.pending.is_(False),
            User.email == sender,
        )
    )
    if is_member:
        with contextlib.suppress(email.EmailError):
            email.send(
                email.notice_email(
                    sender,
                    subject=f"Autorack import: {results[0][:80]}",
                    heading="Your emailed orders",
                    lines=results,
                    button_label="Open orders",
                    url=f"{s.frontend_url.rstrip('/')}/app/#/orders",
                    footer=f"You emailed {wh.name}'s Autorack import address.",
                )
            )
    return {"ok": bool(attachments), "results": results}
