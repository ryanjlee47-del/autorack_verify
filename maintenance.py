"""A process-wide maintenance gate, held while the database file is
replaced underneath the running application.

Why this exists
---------------
backup.restore_backup() overwrites the live database file. Its own
docstring states the precondition correctly -- "the caller is responsible
for making sure no other process holds the database open" -- but the
/restore route called it from inside the running Flask app, with
per-request connections open and match-index caches populated. Nothing
enforced the precondition; it was documented and then not met.

This module is the enforcement. It cannot stop a *separate* process
(gunicorn's other workers, an operator running a script) from holding the
file open -- that remains an operational requirement, and restore_backup's
docstring still says so. What it does cover is the failure mode that was
actually reachable from the /restore button:

  - in-flight requests in this process finish before the swap begins,
  - no new request starts while the file is being replaced,
  - every process-level cache derived from the old file is dropped
    afterwards, so nothing serves pre-restore data from memory.

Requests that arrive during the window get 503 with Retry-After rather
than a partially-restored read.
"""

from __future__ import annotations

import contextlib
import threading

# Held for the duration of a restore. Readers take it shared-in-spirit by
# checking `is_active()`; the writer holds the lock itself, so two restores
# cannot interleave.
_lock = threading.Lock()
_active = threading.Event()

# How long a client should wait before retrying, in seconds. A restore is a
# file copy of a database that fits on one host; this is deliberately short.
RETRY_AFTER_SECONDS = 5


def is_active() -> bool:
    """Whether a restore is currently replacing the database file."""
    return _active.is_set()


@contextlib.contextmanager
def restoring():
    """Hold the maintenance gate for the duration of a restore.

    Serialises concurrent restores and flips the flag that makes
    app.py's before_request hook return 503.
    """
    with _lock:
        _active.set()
        try:
            yield
        finally:
            _active.clear()
