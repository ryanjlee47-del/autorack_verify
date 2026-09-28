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
from collections import deque
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
    """Per-process sliding windows. Keys can come from outside (an address, a
    token prefix), so the table is swept of idle keys and capped in size:
    a stream of made-up keys can't grow memory without bound."""

    MAX_KEYS = 50_000
    SWEEP_EVERY = 1_000

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._windows: dict[str, float] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def check(self, key: str, limit: int, window_seconds: float, message: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._calls += 1
            if self._calls % self.SWEEP_EVERY == 0 or len(self._hits) >= self.MAX_KEYS:
                self._sweep(now)
            q = self._hits.get(key)
            if q is None:
                q = self._hits[key] = deque()
            self._windows[key] = window_seconds
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                raise too_many(message, retry_after=max(1, int(window_seconds - (now - q[0]))))
            q.append(now)

    def _sweep(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - self._windows.get(k, 3600)]:
            del self._hits[k]
            self._windows.pop(k, None)
        if len(self._hits) >= self.MAX_KEYS:
            # Still full of live keys: drop the ones idle longest.
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1] if self._hits[k] else 0)[: self.MAX_KEYS // 5]:
                del self._hits[k]
                self._windows.pop(k, None)

    def size(self) -> int:
        return len(self._hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._windows.clear()


memory_limiter = MemoryLimiter()
