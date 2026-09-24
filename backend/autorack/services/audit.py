"""Audit log writes. Append-only; the table's trigger rejects edits."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog


@dataclass(frozen=True)
class Actor:
    type: str  # user | worker | device | system | stripe | operator
    id: str | None = None
    label: str | None = None
    ip: str | None = None


SYSTEM = Actor("system")
OPERATOR = Actor("operator", label="cli")


def record(
    db: Session,
    actor: Actor,
    action: str,
    *,
    warehouse_id: uuid.UUID | None,
    target_type: str | None = None,
    target_id: object | None = None,
    **details: Any,
) -> None:
    """Add an audit row to the current transaction.

    It commits (or rolls back) with the change it describes, so the log never
    records something that did not happen.
    """
    db.add(
        AuditLog(
            warehouse_id=warehouse_id,
            actor_type=actor.type,
            actor_id=actor.id,
            actor_label=actor.label,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details={k: _jsonable(v) for k, v in details.items()},
            ip=actor.ip,
        )
    )


def _jsonable(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value"):  # enums
        return v.value
    return v
