"""Rate limiting.

Two flavours, for two different jobs:

* `check_db` -- a shared, windowed count over rows in `rate_limit_hits`. Used
  for anything security-relevant (PIN guesses, magic-link requests, signups),
  where every API process must share one budget and a restart must not reset
  it. A 4-digit PIN is only 10,000 guesses; this is what makes that enough.
* `MemoryLimiter` -- per-process sliding window for high-volume endpoints
  (scan sync, CSV import) where the goal is stopping a runaway client loop
  from flooding the database, not stopping an attacker. Not exact across
  processes, and does not need to be.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..errors import too_many
from ..models import RateLimitHit, utcnow


def count_db(db: Session, bucket: str, key: str, window: timedelta) -> int:
    since = utcnow() - window
    return (
        db.scalar(
            select(func.count())
            .select_from(RateLimitHit)
            .where(RateLimitHit.bucket == bucket, RateLimitHit.key == key, RateLimitHit.created_at >= since)
        )
        or 0
    )


def hit_db(db: Session, bucket: str, key: str) -> None:
    db.add(RateLimitHit(bucket=bucket, key=key))
    db.flush()


def check_db(
    db: Session, bucket: str, key: str, limit: int, window: timedelta, message: str, *, record: bool = True
) -> None:
    """Raise 429 if `key` has already used `limit` hits in `window`; else record one."""
    if count_db(db, bucket, key, window) >= limit:
        raise too_many(message, retry_after=int(window.total_seconds()))
    if record:
        hit_db(db, bucket, key)


def clear_db(db: Session, bucket: str, key: str) -> None:
    db.execute(delete(RateLimitHit).where(RateLimitHit.bucket == bucket, RateLimitHit.key == key))


def prune_db(db: Session, older_than: timedelta = timedelta(days=2)) -> int:
    res = db.execute(delete(RateLimitHit).where(RateLimitHit.created_at < utcnow() - older_than))
    return res.rowcount or 0  # type: ignore[attr-defined]


class MemoryLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: float, message: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                raise too_many(message, retry_after=max(1, int(window_seconds - (now - q[0]))))
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


memory_limiter = MemoryLimiter()
