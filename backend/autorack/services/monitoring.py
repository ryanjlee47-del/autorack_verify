"""Knowing when Autorack breaks, before a customer tells you.

Every unhandled server exception, failed background job and browser error
(dashboard, phone, operator console) is recorded in `error_events`, grouped
by a signature so repeats are counted rather than duplicated. The job runner
emails the operators (OPERATOR_EMAILS) a digest of anything new within a
minute, and at most hourly for a problem that keeps happening. Marking one
resolved in the operator console re-arms it: if it comes back, you hear.

Optional: set SENTRY_DSN to also send server exceptions to Sentry.

Recording never raises: a broken error reporter must not turn one failure
into two.
"""

from __future__ import annotations

import hashlib
import logging
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ErrorEvent, utcnow

log = logging.getLogger("autorack.monitoring")

PACKAGE_DIR = str(Path(__file__).resolve().parents[1])
REALERT_AFTER = timedelta(hours=1)
MAX_DETAIL = 8000

# Browser noise that isn't ours to fix.
IGNORED_BROWSER = (
    "ResizeObserver loop",
    "Script error.",
    "chrome-extension://",
    "moz-extension://",
    "safari-extension://",
    "No connection to the server",
    "Failed to fetch",
    "Load failed",
    "NetworkError when attempting to fetch",
    "The operation was aborted",
)


def _signature(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _where(exc: BaseException) -> str:
    """The deepest frame in our own code: stable across requests, specific
    enough that two different bugs don't share a signature."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if f.filename.startswith(PACKAGE_DIR)]
    f = (ours or frames or [None])[-1]
    if f is None:
        return "?"
    return f"{Path(f.filename).name}:{f.lineno}:{f.name}"


def _store(*, source: str, kind: str, message: str, where: str, detail: str | None, context: dict[str, Any]) -> None:
    from ..db import get_sessionmaker

    try:
        with get_sessionmaker()() as db:
            now = utcnow()
            stmt = insert(ErrorEvent).values(
                signature=_signature(source, kind, where),
                source=source,
                kind=kind[:200],
                message=message[:1000],
                detail=(detail or "")[-MAX_DETAIL:] or None,
                context=context,
                count=1,
                first_seen=now,
                last_seen=now,
                alerted_count=0,
            )
            db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ErrorEvent.signature],
                    set_={
                        "count": ErrorEvent.count + 1,
                        "last_seen": now,
                        "message": stmt.excluded.message,
                        "detail": stmt.excluded.detail,
                        "context": stmt.excluded.context,
                        # Came back after being marked resolved: alert again now.
                        "alerted_at": case((ErrorEvent.resolved_at.is_not(None), None), else_=ErrorEvent.alerted_at),
                        "alerted_count": case((ErrorEvent.resolved_at.is_not(None), 0), else_=ErrorEvent.alerted_count),
                        "resolved_at": None,
                    },
                )
            )
            db.commit()
    except Exception:
        log.exception("could not record an error event")


def record_exception(exc: BaseException, *, source: str = "server", context: dict[str, Any] | None = None) -> None:
    log.error("%s error: %r", source, exc, exc_info=exc)
    _store(
        source=source,
        kind=type(exc).__name__,
        message=str(exc) or type(exc).__name__,
        where=_where(exc),
        detail="".join(traceback.format_exception(exc)),
        context=context or {},
    )


def record_browser(payload: dict[str, Any], *, ip: str | None, user_agent: str | None) -> bool:
    """A JavaScript error one of our pages reported. Returns False if ignored."""
    message = str(payload.get("message") or "")[:1000]
    stack = str(payload.get("stack") or "")[:MAX_DETAIL]
    source_file = str(payload.get("source") or "")[:300]
    if not message or any(noise in message or noise in stack or noise in source_file for noise in IGNORED_BROWSER):
        return False
    app = str(payload.get("app") or "web")[:20]
    # Group by the first frame in our own scripts (URLs vary by host).
    frame = re.search(r"/(?:app|w|admin|shared)/[\w./-]+\.js:\d+", stack or source_file)
    where = frame.group(0) if frame else f"{source_file}:{payload.get('line', '')}"
    _store(
        source="browser",
        kind=f"{app}: {message.split(':')[0][:120]}",
        message=message,
        where=f"{app}|{where}|{message[:80]}",
        detail=stack or None,
        context={
            "app": app,
            "page": str(payload.get("page") or "")[:300],
            "user_agent": (user_agent or "")[:300],
            "ip": ip,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Alerts (run by the job runner every minute)
# ---------------------------------------------------------------------------


def due_alerts(db: Session, now: datetime) -> list[ErrorEvent]:
    return list(
        db.scalars(
            select(ErrorEvent)
            .where(
                ErrorEvent.resolved_at.is_(None),
                or_(
                    ErrorEvent.alerted_at.is_(None),
                    (ErrorEvent.count > ErrorEvent.alerted_count) & (ErrorEvent.alerted_at <= now - REALERT_AFTER),
                ),
            )
            .order_by(ErrorEvent.last_seen.desc())
            .limit(50)
        )
    )


def run_error_alerts(db: Session, now: datetime) -> int:
    from . import email

    events = due_alerts(db, now)
    if not events:
        return 0
    to = sorted(get_settings().operator_email_set)
    lines = []
    for e in events[:20]:
        new = e.count - e.alerted_count
        where = e.context.get("path") or e.context.get("page") or e.context.get("job") or ""
        lines.append(
            f"[{e.source}] {e.kind}: {e.message[:180]} — {new} new, {e.count} total{f' at {where}' if where else ''}"
        )
    if len(events) > 20:
        lines.append(f"…and {len(events) - 20} more.")
    if to:
        url = f"{get_settings().frontend_url.rstrip('/')}/admin/#/errors"
        try:
            for addr in to:
                email.send(
                    email.notice_email(
                        addr,
                        subject=f"Autorack: {len(events)} error{'s' if len(events) != 1 else ''} need a look",
                        heading="Something went wrong",
                        lines=["These errors happened in Autorack since the last alert:", *lines],
                        button_label="Open the error list",
                        url=url,
                        footer="Operator alert. You get at most one email an hour for an error that keeps "
                        "happening; mark it resolved and you'll hear if it comes back.",
                    )
                )
        except email.EmailError:
            log.exception("could not send the error alert; will retry")
            db.rollback()
            return 0
    else:
        log.error("errors need attention (no OPERATOR_EMAILS set):\n%s", "\n".join(lines))
    for e in events:
        e.alerted_at = now
        e.alerted_count = e.count
    db.commit()
    return len(events)


def init_sentry() -> bool:
    dsn = get_settings().sentry_dsn
    if not dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=get_settings().environment,
            traces_sample_rate=0.0,
            send_default_pii=False,  # no emails, IPs or cookies leave for Sentry
        )
        return True
    except Exception:
        log.exception("Sentry setup failed; continuing without it")
        return False
