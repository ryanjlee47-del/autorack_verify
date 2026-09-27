"""Batch picking from the dashboard: group orders, one tote each."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import OwnerContext, current_owner, require_manager
from ..models import PickBatch
from ..services import batches, usage

router = APIRouter(tags=["batches"])


class BatchIn(BaseModel):
    order_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    worker_id: uuid.UUID | None = None


@router.get("/batches")
def list_batches(ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
    open_ = batches.open_batches(db, ctx.warehouse)
    recent = list(
        db.scalars(
            select(PickBatch)
            .where(PickBatch.warehouse_id == ctx.warehouse.id, PickBatch.closed_at.is_not(None))
            .order_by(PickBatch.closed_at.desc())
            .limit(10)
        )
    )
    db.commit()
    return {
        "open": [batches.batch_dict(db, ctx.warehouse, b) for b in open_],
        "recent": [batches.batch_dict(db, ctx.warehouse, b) for b in recent],
    }


@router.post("/batches", status_code=201)
def create_batch(
    body: BatchIn, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> dict[str, Any]:
    b = batches.create(db, ctx.warehouse, body.order_ids, body.worker_id, ctx.actor)
    usage.track(db, ctx.warehouse.id, "batches.create")
    db.commit()
    return batches.batch_dict(db, ctx.warehouse, b)


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: uuid.UUID, ctx: OwnerContext = Depends(current_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    b = batches.get(db, ctx.warehouse, batch_id)
    batches.close_if_done(db, b)
    db.commit()
    return batches.batch_dict(db, ctx.warehouse, b)


@router.delete("/batches/{batch_id}", status_code=204)
def release_batch(
    batch_id: uuid.UUID, ctx: OwnerContext = Depends(require_manager), db: Session = Depends(get_db)
) -> Response:
    b = batches.get(db, ctx.warehouse, batch_id)
    batches.release(db, ctx.warehouse, b, ctx.actor)
    db.commit()
    return Response(status_code=204)
