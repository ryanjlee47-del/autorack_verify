"""Owner-facing account lifecycle: download everything, close, reopen."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import OwnerContext, require_owner_role
from ..downloads import attachment
from ..errors import bad_request
from ..services import account as account_svc
from ..services import audit
from ..services.ratelimit import memory_limiter

router = APIRouter(prefix="/account", tags=["account"])


def account_state(ctx: OwnerContext) -> dict[str, Any]:
    wh = ctx.warehouse
    return {
        "closed_at": wh.closed_at.isoformat() if wh.closed_at else None,
        "closed_by": wh.closed_by,
        "close_reason": wh.close_reason,
        "deletion_due_at": wh.deletion_due_at.isoformat() if wh.deletion_due_at else None,
        "retention_days": account_svc.retention_days(),
    }


def zip_response(fh: Any, filename: str) -> StreamingResponse:
    return StreamingResponse(
        account_svc.stream(fh),
        media_type="application/zip",
        headers={"Content-Disposition": attachment(filename), "Cache-Control": "private, no-store"},
    )


@router.get("/export.zip")
def export(ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)) -> StreamingResponse:
    """Everything this warehouse has in Autorack, as CSVs, photos and PDFs."""
    memory_limiter.check(f"export:{ctx.warehouse.id}", 3, 300, "Exports are limited to 3 every 5 minutes.")
    fh = account_svc.export_zip(db, ctx.warehouse)
    audit.record(
        db,
        ctx.actor,
        "account.exported",
        warehouse_id=ctx.warehouse.id,
        target_type="warehouse",
        target_id=ctx.warehouse.id,
    )
    db.commit()
    return zip_response(fh, account_svc.export_filename(ctx.warehouse))


@router.get("")
def state(ctx: OwnerContext = Depends(require_owner_role)) -> dict[str, Any]:
    return account_state(ctx)


class CloseIn(BaseModel):
    confirm_name: str = Field(max_length=200)
    reason: str | None = Field(default=None, max_length=500)


@router.post("/close")
def close_account(
    body: CloseIn, ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)
) -> dict[str, Any]:
    if body.confirm_name.strip().casefold() != ctx.warehouse.name.strip().casefold():
        raise bad_request("confirm_name_mismatch", "Type the warehouse's name exactly to confirm.")
    account_svc.close(db, ctx.warehouse, ctx.actor, reason=body.reason)
    db.commit()
    return account_state(ctx)


@router.post("/reopen")
def reopen_account(ctx: OwnerContext = Depends(require_owner_role), db: Session = Depends(get_db)) -> dict[str, Any]:
    account_svc.reopen(db, ctx.warehouse, ctx.actor)
    db.commit()
    return account_state(ctx)
