"""Scan ingestion: the single path by which picks reach the database.

Online or offline, every scan, undo ("void") and flag a phone produces goes
through `sync()`. A live scan is just a batch of one. That means the offline
path is not a separate, rarely exercised code path: it is the only path.

Guarantees:

* Idempotent. Event ids are generated on the phone; re-sending a batch after a
  dropped response inserts nothing new and returns the original outcomes.
* Authoritative. The phone decides match/mismatch locally for instant feedback,
  but the server re-derives the result with the same engine against the
  current order. The phone's claim is stored (`client_result`) for audit, never
  trusted.
* Ordered. Events are applied in the time order they happened on the phone
  (client timestamp, then the phone's own sequence number), one order at a
  time under a row lock, so two phones picking the same order cannot both
  claim the last unit.
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import matching
from ..errors import ApiError
from ..models import (
    Device,
    FlagReason,
    Order,
    OrderFlag,
    OrderLineItem,
    OrderStatus,
    ScanEvent,
    ScanResult,
    Warehouse,
    WorkerSession,
    utcnow,
)
from . import orders as order_svc

log = logging.getLogger("autorack.scans")

MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass
class SyncEvent:
    id: uuid.UUID
    kind: Literal["scan", "void", "flag"]
    order_id: uuid.UUID
    session_id: uuid.UUID
    client_scanned_at: datetime
    client_seq: int | None = None
    scanned_barcode: str | None = None
    intended_line_item_id: uuid.UUID | None = None
    client_result: str | None = None
    offline: bool = False
    target_scan_id: uuid.UUID | None = None
    line_item_id: uuid.UUID | None = None
    scan_event_id: uuid.UUID | None = None
    reason: FlagReason | None = None
    note: str | None = None


@dataclass
class EventOutcome:
    id: str
    kind: str
    status: Literal["applied", "duplicate", "error"]
    result: str | None = None
    line_item_id: str | None = None
    match_tier: int | None = None
    error: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class SyncOutcome:
    events: list[EventOutcome] = field(default_factory=list)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)


class EventError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sync(db: Session, device: Device, wh: Warehouse, events: list[SyncEvent]) -> SyncOutcome:
    now = utcnow()
    for ev in events:
        if ev.client_scanned_at.tzinfo is None:
            raise ApiError(422, "timestamp_invalid", "client_scanned_at must include a timezone.")
        # A phone with a wrong clock must not be able to pre- or post-date
        # history by more than a small margin in the future.
        if ev.client_scanned_at > now + MAX_CLOCK_SKEW:
            ev.client_scanned_at = now

    ordered = sorted(events, key=lambda e: (e.client_scanned_at, e.client_seq or 0))
    session_ids = {e.session_id for e in ordered}
    sessions = {
        s.id: s
        for s in db.scalars(
            select(WorkerSession).where(WorkerSession.id.in_(session_ids), WorkerSession.device_id == device.id)
        )
    }

    groups: OrderedDict[uuid.UUID, list[SyncEvent]] = OrderedDict()
    for ev in ordered:
        groups.setdefault(ev.order_id, []).append(ev)

    outcomes: dict[uuid.UUID, EventOutcome] = {}
    order_states: dict[str, dict[str, Any]] = {}
    for order_id, evs in groups.items():
        try:
            group_outcomes = _apply_order_group(db, device, wh, order_id, evs, sessions)
            order = db.get(Order, order_id)
            state = _order_state(db, order) if order and order.warehouse_id == wh.id else None
            db.commit()
            outcomes.update(group_outcomes)
            if state:
                order_states[str(order_id)] = state
        except EventError as e:
            db.rollback()
            for ev in evs:
                outcomes[ev.id] = EventOutcome(
                    str(ev.id), ev.kind, "error", error={"code": e.code, "message": e.message}
                )
        except Exception:
            db.rollback()
            log.exception("sync failed for order %s", order_id)
            raise

    return SyncOutcome(events=[outcomes[e.id] for e in events], orders=order_states)


def _order_state(db: Session, order: Order) -> dict[str, Any]:
    lines = order_svc.lines_for(db, order.id)
    return {
        "status": order.status.value,
        "version": order.version,
        "lines": {str(li.id): li.scanned_quantity for li in lines},
        "open_flags": order_svc.open_flag_count(db, order.id),
    }


def _apply_order_group(
    db: Session,
    device: Device,
    wh: Warehouse,
    order_id: uuid.UUID,
    evs: list[SyncEvent],
    sessions: dict[uuid.UUID, WorkerSession],
) -> dict[uuid.UUID, EventOutcome]:
    order = db.scalar(select(Order).where(Order.id == order_id, Order.warehouse_id == wh.id).with_for_update())
    if not order:
        raise EventError("order_not_found", "This order no longer exists.")
    lines = order_svc.lines_for(db, order.id)
    lines_by_id = {str(li.id): li for li in lines}
    index: matching.MatchIndex | None = None
    out: dict[uuid.UUID, EventOutcome] = {}
    touched = False

    for ev in evs:
        existing = _existing_outcome(db, wh, ev)
        if existing:
            out[ev.id] = existing
            continue
        sess = sessions.get(ev.session_id)
        if not sess:
            out[ev.id] = EventOutcome(
                str(ev.id),
                ev.kind,
                "error",
                error={"code": "session_unknown", "message": "Scan came from a session this phone doesn't own."},
            )
            continue
        try:
            with db.begin_nested():
                if ev.kind == "scan":
                    if index is None:
                        index = order_svc.build_index(db, wh, lines)
                    out[ev.id] = _apply_scan(db, device, wh, order, lines_by_id, index, sess, ev)
                elif ev.kind == "void":
                    out[ev.id] = _apply_void(db, device, wh, order, lines_by_id, sess, ev)
                else:
                    out[ev.id] = _apply_flag(db, wh, order, lines_by_id, sess, ev)
            touched = True
        except EventError as e:
            out[ev.id] = EventOutcome(str(ev.id), ev.kind, "error", error={"code": e.code, "message": e.message})

    if touched:
        order_svc.recompute_status(db, order, lines)
    return out


def _existing_outcome(db: Session, wh: Warehouse, ev: SyncEvent) -> EventOutcome | None:
    if ev.kind in ("scan", "void"):
        prior = db.get(ScanEvent, ev.id)
        if prior is None:
            return None
        if prior.warehouse_id != wh.id:
            return EventOutcome(str(ev.id), ev.kind, "error", error={"code": "id_conflict", "message": "Duplicate id."})
        return EventOutcome(
            str(ev.id),
            ev.kind,
            "duplicate",
            result=prior.result.value,
            line_item_id=str(prior.line_item_id) if prior.line_item_id else None,
            match_tier=prior.match_tier,
        )
    flag = db.get(OrderFlag, ev.id)
    if flag is None:
        return None
    if flag.warehouse_id != wh.id:
        return EventOutcome(str(ev.id), ev.kind, "error", error={"code": "id_conflict", "message": "Duplicate id."})
    return EventOutcome(str(ev.id), "flag", "duplicate", result="flagged")


def _mark_started(order: Order, at: datetime) -> None:
    if order.started_at is None:
        order.started_at = min(at, utcnow())


def _apply_scan(
    db: Session,
    device: Device,
    wh: Warehouse,
    order: Order,
    lines_by_id: dict[str, OrderLineItem],
    index: matching.MatchIndex,
    sess: WorkerSession,
    ev: SyncEvent,
) -> EventOutcome:
    if order.status == OrderStatus.cancelled:
        raise EventError("order_cancelled", "This order was cancelled. Put the items back.")
    raw = (ev.scanned_barcode or "")[:500]
    if not matching.normalized_key(raw):
        raise EventError("barcode_empty", "Empty barcode.")

    mr = index.match(raw)
    line: OrderLineItem | None = lines_by_id.get(str(mr.line_id)) if mr.line_id is not None else None
    if mr.is_resolved and line is not None and not mr.needs_confirmation:
        if line.scanned_quantity < line.expected_quantity:
            result = ScanResult.match
            line.scanned_quantity += 1
        else:
            result = ScanResult.over_pick
    elif mr.is_resolved or mr.ambiguous:
        # Low-confidence (suffix) hit, or several candidates: a human decides.
        result = ScanResult.review
    else:
        result = ScanResult.mismatch

    intended = str(ev.intended_line_item_id) if ev.intended_line_item_id else None
    _mark_started(order, ev.client_scanned_at)
    db.add(
        ScanEvent(
            id=ev.id,
            warehouse_id=wh.id,
            order_id=order.id,
            line_item_id=line.id if line is not None else None,
            intended_line_item_id=uuid.UUID(intended) if intended in lines_by_id else None,
            worker_id=sess.worker_id,
            device_id=device.id,
            worker_session_id=sess.id,
            scanned_barcode=raw,
            normalized_barcode=matching.normalized_key(raw)[:500],
            result=result,
            is_match=result == ScanResult.match,
            match_tier=int(mr.tier) if mr.tier is not None else None,
            client_result=(ev.client_result or None) and ev.client_result[:32],
            was_offline=ev.offline,
            client_seq=ev.client_seq,
            client_scanned_at=ev.client_scanned_at,
        )
    )
    db.flush()
    return EventOutcome(
        str(ev.id),
        "scan",
        "applied",
        result=result.value,
        line_item_id=str(line.id) if line is not None else None,
        match_tier=int(mr.tier) if mr.tier is not None else None,
    )


def _apply_void(
    db: Session,
    device: Device,
    wh: Warehouse,
    order: Order,
    lines_by_id: dict[str, OrderLineItem],
    sess: WorkerSession,
    ev: SyncEvent,
) -> EventOutcome:
    target = db.get(ScanEvent, ev.target_scan_id) if ev.target_scan_id else None
    if not target or target.warehouse_id != wh.id or target.order_id != order.id:
        raise EventError("void_target_missing", "That scan can't be undone.")
    if target.result != ScanResult.match:
        raise EventError("void_not_match", "Only a counted pick can be undone.")
    if db.scalar(select(ScanEvent.id).where(ScanEvent.voids_scan_id == target.id)):
        raise EventError("void_already", "That scan was already undone.")
    line = lines_by_id.get(str(target.line_item_id))
    if line is None:
        raise EventError("void_target_missing", "That scan can't be undone.")
    line.scanned_quantity = max(0, line.scanned_quantity - 1)
    db.add(
        ScanEvent(
            id=ev.id,
            warehouse_id=wh.id,
            order_id=order.id,
            line_item_id=line.id,
            worker_id=sess.worker_id,
            device_id=device.id,
            worker_session_id=sess.id,
            scanned_barcode=target.scanned_barcode,
            normalized_barcode=target.normalized_barcode,
            result=ScanResult.void,
            is_match=False,
            voids_scan_id=target.id,
            was_offline=ev.offline,
            client_seq=ev.client_seq,
            client_scanned_at=ev.client_scanned_at,
        )
    )
    db.flush()
    return EventOutcome(str(ev.id), "void", "applied", result="void", line_item_id=str(line.id))


def _apply_flag(
    db: Session,
    wh: Warehouse,
    order: Order,
    lines_by_id: dict[str, OrderLineItem],
    sess: WorkerSession,
    ev: SyncEvent,
) -> EventOutcome:
    if order.status == OrderStatus.cancelled:
        raise EventError("order_cancelled", "This order was cancelled.")
    line_id = str(ev.line_item_id) if ev.line_item_id else None
    scan_id = ev.scan_event_id
    if scan_id is not None:
        scan = db.get(ScanEvent, scan_id)
        if not scan or scan.order_id != order.id:
            scan_id = None
    db.add(
        OrderFlag(
            id=ev.id,
            warehouse_id=wh.id,
            order_id=order.id,
            line_item_id=uuid.UUID(line_id) if line_id in lines_by_id else None,
            scan_event_id=scan_id,
            worker_id=sess.worker_id,
            reason=ev.reason or FlagReason.other,
            note=(ev.note or "").strip()[:500] or None,
            created_at=min(ev.client_scanned_at, utcnow()),
        )
    )
    _mark_started(order, ev.client_scanned_at)
    db.flush()
    return EventOutcome(str(ev.id), "flag", "applied", result="flagged", line_item_id=line_id)
