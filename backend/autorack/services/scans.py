"""Scan ingestion: the single path by which picks reach the database.

Online or offline, every scan, undo ("void"), flag, short pick ("found only 2
of 3") and shipping-label scan ("ship") a phone produces goes
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

import calendar
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import matching
from ..errors import ApiError
from ..models import (
    TALLY_KINDS,
    Device,
    FlagReason,
    Order,
    OrderFlag,
    OrderKind,
    OrderLineItem,
    OrderStatus,
    ScanEvent,
    ScanResult,
    ShortReason,
    Warehouse,
    WorkerSession,
    utcnow,
)
from . import audit, integrations
from . import orders as order_svc
from .audit import Actor
from .dashboard import tz_of

log = logging.getLogger("autorack.scans")

MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass
class SyncEvent:
    id: uuid.UUID
    kind: Literal["scan", "void", "flag", "short", "ship", "finish"]
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
    quantity: int | None = None
    short_reason: ShortReason | None = None
    tracking_number: str | None = None
    lot: str | None = None
    serial: str | None = None
    expiry: date | None = None


@dataclass
class EventOutcome:
    id: str
    kind: str
    status: Literal["applied", "duplicate", "error"]
    result: str | None = None
    line_item_id: str | None = None
    match_tier: int | None = None
    error: dict[str, str] | None = None
    problem: str | None = None

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
        "short": {str(li.id): li.short_quantity for li in lines if li.short_quantity},
        "open_flags": order_svc.open_flag_count(db, order.id),
        "tracking_number": order.tracking_number,
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
                elif ev.kind == "short":
                    _pick_only(order)
                    out[ev.id] = _apply_short(db, wh, order, lines_by_id, sess, ev)
                elif ev.kind == "ship":
                    _pick_only(order)
                    order_svc.recompute_status(db, order, lines)
                    out[ev.id] = _apply_ship(db, wh, order, lines, sess, ev)
                elif ev.kind == "finish":
                    out[ev.id] = _apply_finish(db, wh, order, sess, ev)
                else:
                    out[ev.id] = _apply_flag(db, wh, order, lines_by_id, sess, ev)
            touched = True
        except EventError as e:
            out[ev.id] = EventOutcome(str(ev.id), ev.kind, "error", error={"code": e.code, "message": e.message})

    if touched:
        order_svc.recompute_status(db, order, lines)
    return out


def _existing_outcome(db: Session, wh: Warehouse, ev: SyncEvent) -> EventOutcome | None:
    if ev.kind == "finish":
        order = db.get(Order, ev.order_id)
        if order is not None and order.warehouse_id == wh.id and order.kind in TALLY_KINDS and order.completed_at:
            return EventOutcome(str(ev.id), "finish", "duplicate", result="finished")
        return None
    if ev.kind == "ship":
        order = db.get(Order, ev.order_id)
        tracking = order_svc.normalize_tracking(ev.tracking_number or "")
        shipped = order is not None and order.warehouse_id == wh.id and order.status == OrderStatus.shipped
        if shipped and order is not None and order.tracking_number == tracking:
            return EventOutcome(str(ev.id), "ship", "duplicate", result="shipped")
        return None
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
    return EventOutcome(str(ev.id), ev.kind, "duplicate", result="flagged")


def _mark_started(order: Order, at: datetime) -> None:
    if order.started_at is None:
        order.started_at = min(at, utcnow())


def _pick_only(order: Order) -> None:
    if order.kind != OrderKind.pick:
        raise EventError("not_for_task", "That only applies to picking orders.")


def _ensure_open(order: Order) -> None:
    if order.status == OrderStatus.cancelled:
        raise EventError("order_cancelled", "This order was cancelled. Put the items back.")
    if order.kind in TALLY_KINDS and order.completed_at is not None:
        raise EventError("task_finished", "This was already finished. Ask a manager to reopen it.")
    if order.status == OrderStatus.shipped:
        raise EventError("order_shipped", "This order has already shipped.")


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
    _ensure_open(order)
    raw = (ev.scanned_barcode or "")[:500]
    if not matching.normalized_key(raw):
        raise EventError("barcode_empty", "Empty barcode.")

    mr = index.match(raw)
    line: OrderLineItem | None = lines_by_id.get(str(mr.line_id)) if mr.line_id is not None else None
    tally = order.kind in TALLY_KINDS
    details = unit_details(raw, ev)
    problem: str | None = None
    if tally and mr.is_resolved and line is not None and not mr.needs_confirmation:
        # Receiving, returns, counts: record what's there, past the expected
        # quantity too -- the difference is the point.
        result = ScanResult.counted
        line.scanned_quantity += 1
    elif tally and not (mr.is_resolved or mr.ambiguous):
        result = ScanResult.extra
    elif mr.is_resolved and line is not None and not mr.needs_confirmation:
        if order_svc.line_remaining(line) > 0:
            problem = trace_problem(db, wh, line, details)
            if problem == "details_missing":
                result = ScanResult.review
            elif problem:
                result = ScanResult.mismatch  # the right product, but it mustn't ship
            else:
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
            line_item_id=line.id if line is not None and result != ScanResult.extra else None,
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
            lot=details.lot,
            serial=details.serial,
            expiry=details.expiry,
            problem=problem,
        )
    )
    db.flush()
    return EventOutcome(
        str(ev.id),
        "scan",
        "applied",
        result=result.value,
        line_item_id=str(line.id) if line is not None and result != ScanResult.extra else None,
        match_tier=int(mr.tier) if mr.tier is not None else None,
        problem=problem,
    )


# ---------------------------------------------------------------------------
# Lot / serial / expiry
# ---------------------------------------------------------------------------


@dataclass
class UnitDetails:
    lot: str | None = None
    serial: str | None = None
    expiry: date | None = None


def gs1_date(yymmdd: str | None) -> date | None:
    """GS1 dates are YYMMDD; day 00 means the last day of that month."""
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    y, m, d = 2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    if not 1 <= m <= 12:
        return None
    if d == 0:
        d = calendar.monthrange(y, m)[1]
    try:
        return date(y, m, d)
    except ValueError:
        return None


def unit_details(raw: str, ev: SyncEvent) -> UnitDetails:
    """What the barcode itself says (GS1 AIs 10, 21, 17/15) wins over what
    was typed: it can't be mistyped."""
    gs1 = matching.parse_gs1(raw)
    lot = (gs1.lot if gs1 else None) or ev.lot
    serial = (gs1.serial if gs1 else None) or ev.serial
    expiry = (gs1_date(gs1.extra.get("17") or gs1.extra.get("15")) if gs1 else None) or ev.expiry
    clean = order_svc.clean
    return UnitDetails(clean(lot, 100), clean(serial, 100), expiry)


def trace_problem(db: Session, wh: Warehouse, line: OrderLineItem, d: UnitDetails) -> str | None:
    """Why this unit of the right product must not ship, if it mustn't."""
    if (line.track_lot and not d.lot) or (line.track_serial and not d.serial) or (line.track_expiry and not d.expiry):
        return "details_missing"
    if line.required_lot and (d.lot or "").strip().upper() != line.required_lot.strip().upper():
        return "wrong_lot"
    if d.expiry and d.expiry < utcnow().astimezone(tz_of(wh)).date():
        return "expired"
    if line.track_serial and d.serial:
        voided = select(ScanEvent.voids_scan_id).where(
            ScanEvent.warehouse_id == wh.id, ScanEvent.voids_scan_id.is_not(None)
        )
        repeat = db.scalar(
            select(ScanEvent.id)
            .join(OrderLineItem, OrderLineItem.id == ScanEvent.line_item_id)
            .join(Order, Order.id == ScanEvent.order_id)
            .where(
                ScanEvent.warehouse_id == wh.id,
                ScanEvent.serial == d.serial,
                ScanEvent.result == ScanResult.match,
                ScanEvent.id.not_in(voided),
                OrderLineItem.normalized_barcode == line.normalized_barcode,
                Order.status != OrderStatus.cancelled,
            )
            .limit(1)
        )
        if repeat:
            return "serial_repeat"
    return None


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
    tally = order.kind in TALLY_KINDS
    undoable = (ScanResult.counted, ScanResult.extra) if tally else (ScanResult.match,)
    if target.result not in undoable:
        raise EventError("void_not_match", "Only a counted pick can be undone.")
    if order.status == OrderStatus.shipped:
        raise EventError("order_shipped", "This order has already shipped.")
    if tally:
        _ensure_open(order)
    if db.scalar(select(ScanEvent.id).where(ScanEvent.voids_scan_id == target.id)):
        raise EventError("void_already", "That scan was already undone.")
    line = lines_by_id.get(str(target.line_item_id)) if target.line_item_id else None
    if line is None and target.result != ScanResult.extra:
        raise EventError("void_target_missing", "That scan can't be undone.")
    if line is not None:
        line.scanned_quantity = max(0, line.scanned_quantity - 1)
    db.add(
        ScanEvent(
            id=ev.id,
            warehouse_id=wh.id,
            order_id=order.id,
            line_item_id=line.id if line is not None else None,
            worker_id=sess.worker_id,
            device_id=device.id,
            worker_session_id=sess.id,
            scanned_barcode=target.scanned_barcode,
            normalized_barcode=target.normalized_barcode,
            result=ScanResult.uncounted if tally else ScanResult.void,
            is_match=False,
            voids_scan_id=target.id,
            was_offline=ev.offline,
            client_seq=ev.client_seq,
            client_scanned_at=ev.client_scanned_at,
        )
    )
    db.flush()
    return EventOutcome(
        str(ev.id),
        "void",
        "applied",
        result="uncounted" if tally else "void",
        line_item_id=str(line.id) if line is not None else None,
    )


def _apply_finish(db: Session, wh: Warehouse, order: Order, sess: WorkerSession, ev: SyncEvent) -> EventOutcome:
    """The worker says the delivery / return / location is done. What was
    counted against what was expected is the result; nothing is "short"."""
    if order.kind not in TALLY_KINDS:
        raise EventError("not_for_task", "Picking orders finish by themselves when every item is scanned.")
    _ensure_open(order)
    order.completed_at = min(ev.client_scanned_at, utcnow())
    order.finished_by_worker_id = sess.worker_id
    _mark_started(order, ev.client_scanned_at)
    audit.record(
        db,
        Actor("worker", str(sess.worker_id), None, None),
        "task.finished",
        warehouse_id=wh.id,
        target_type="order",
        target_id=order.id,
        kind=order.kind.value,
    )
    order_svc.bump(order)
    db.flush()
    return EventOutcome(str(ev.id), "finish", "applied", result="finished")


def _apply_flag(
    db: Session,
    wh: Warehouse,
    order: Order,
    lines_by_id: dict[str, OrderLineItem],
    sess: WorkerSession,
    ev: SyncEvent,
) -> EventOutcome:
    _ensure_open(order)
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


def _apply_short(
    db: Session,
    wh: Warehouse,
    order: Order,
    lines_by_id: dict[str, OrderLineItem],
    sess: WorkerSession,
    ev: SyncEvent,
) -> EventOutcome:
    """'Found only 2 of 3': the rest of the line is reported missing.

    Recorded as a flag (so the order waits for a human) that remembers how
    many units and why. The line counts as done while the flag stands; a
    manager either accepts it (ship short) or sends it back to be picked.
    """
    _ensure_open(order)
    line = lines_by_id.get(str(ev.line_item_id)) if ev.line_item_id else None
    if line is None:
        raise EventError("line_missing", "That item isn't on this order any more.")
    remaining = order_svc.line_remaining(line)
    if remaining <= 0:
        raise EventError("line_complete", "That item is already fully picked.")
    qty = min(max(1, ev.quantity or remaining), remaining)
    line.short_quantity += qty
    db.add(
        OrderFlag(
            id=ev.id,
            warehouse_id=wh.id,
            order_id=order.id,
            line_item_id=line.id,
            worker_id=sess.worker_id,
            reason=FlagReason.short_pick,
            short_quantity=qty,
            short_reason=ev.short_reason or ShortReason.not_found,
            note=(ev.note or "").strip()[:500] or None,
            created_at=min(ev.client_scanned_at, utcnow()),
        )
    )
    _mark_started(order, ev.client_scanned_at)
    order_svc.bump(order)
    db.flush()
    return EventOutcome(str(ev.id), "short", "applied", result="flagged", line_item_id=str(line.id))


def _apply_ship(
    db: Session,
    wh: Warehouse,
    order: Order,
    lines: list[OrderLineItem],
    sess: WorkerSession,
    ev: SyncEvent,
) -> EventOutcome:
    """The shipping label on the packed box: ties this order to a tracking
    number, which is the proof of what went out in which parcel."""
    if order.status == OrderStatus.cancelled:
        raise EventError("order_cancelled", "This order was cancelled. Don't ship it.")
    if order.status == OrderStatus.shipped:
        raise EventError("order_shipped", f"Already shipped with tracking {order.tracking_number}.")
    if order.status == OrderStatus.flagged:
        raise EventError("order_flagged", "This order has an open problem. A manager must clear it first.")
    if order.status != OrderStatus.completed:
        raise EventError("order_incomplete", "Finish picking every item before scanning the label.")
    tracking = order_svc.normalize_tracking(ev.tracking_number or "")
    if len(tracking) < 8:
        raise EventError("tracking_invalid", "That doesn't look like a shipping label. Scan the big tracking barcode.")
    keys = {li.normalized_barcode for li in lines}
    if matching.normalized_key(tracking) in keys or matching.normalized_key(ev.tracking_number or "") in keys:
        raise EventError("tracking_is_product", "That's a product barcode. Scan the shipping label.")
    other = order_svc.tracking_in_use(db, wh.id, tracking, order.id)
    if other:
        label = other.external_order_number or str(other.id)[:8]
        raise EventError("tracking_used", f"That label is already on order {label}. Check the box.")
    order.tracking_number = tracking
    order.carrier = order_svc.guess_carrier(tracking)
    order.shipped_at = min(ev.client_scanned_at, utcnow())
    order.shipped_by_worker_id = sess.worker_id
    order.status = OrderStatus.shipped
    integrations.queue_tracking(order)
    order_svc.bump(order)
    db.flush()
    return EventOutcome(str(ev.id), "ship", "applied", result="shipped")
