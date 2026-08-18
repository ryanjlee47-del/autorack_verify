"""Thin SQLite access layer. Explicit SQL only -- no ORM.

Every other module (manifest_ingest.py, billing.py, app.py, admin_gui.py)
goes through here to touch the database. This file owns: connecting in
WAL mode with foreign keys on, running migrations, and a set of small,
explicit CRUD functions per table. It does not know about barcode
normalization or billing rules -- those live in barcode.py,
manifest_ingest.py and billing.py, which call these primitives.

The query text itself lives in sql/*.sql and is addressed by name through
`SQL["<file>.<name>"]` -- see sqlstore.py for why, and for the rule about
the few statements that legitimately stay in Python. Functions here are
the typed, named entry points; the SQL files are what those entry points
actually send.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlstore import SQL

BASE_DIR = Path(__file__).parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "app.db"
MIGRATIONS_DIR = BASE_DIR / "migrations"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# SQLite authorizer actions that cannot modify the database. Anything not
# on this list is denied when a connection is put in read-only mode.
_READ_ONLY_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

# PRAGMA needs a name allowlist rather than an action check. The
# authorizer reports a pragma's *argument* in arg2 (`PRAGMA
# table_info(users)` -> arg2='users'), which is indistinguishable from an
# assigned value (`PRAGMA writable_schema=ON` -> arg2='ON'), so "does it
# have an argument" cannot tell a read from a write. These are the
# introspection pragmas the console needs; everything else -- notably
# writable_schema, journal_mode, and foreign_keys -- is denied.
_READ_ONLY_PRAGMAS = frozenset(
    {
        "collation_list",
        "compile_options",
        "database_list",
        "foreign_key_check",
        "foreign_key_list",
        "freelist_count",
        "function_list",
        "index_info",
        "index_list",
        "index_xinfo",
        "integrity_check",
        "module_list",
        "page_count",
        "pragma_list",
        "quick_check",
        "schema_version",
        "table_info",
        "table_list",
        "table_xinfo",
        "user_version",
    }
)


def set_read_only(conn: sqlite3.Connection, read_only: bool = True) -> None:
    """Enforce read-only at the SQLite authorizer level rather than by
    inspecting the SQL string.

    A prefix check ("does it start with SELECT?") is not a security
    boundary: SQLite lets a CTE prefix data-modifying statements, so
    `WITH x AS (SELECT 1) DELETE FROM users` reads as a "WITH" query and
    then deletes rows. The authorizer sees the actual parsed operations
    and cannot be talked around by how the statement is spelled.
    """
    if not read_only:
        conn.set_authorizer(None)
        return

    def authorizer(action, arg1, arg2, db_name, trigger_name):
        if action == sqlite3.SQLITE_PRAGMA:
            return (
                sqlite3.SQLITE_OK
                if (arg1 or "").lower() in _READ_ONLY_PRAGMAS
                else sqlite3.SQLITE_DENY
            )
        return sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Group several writes into one atomic unit.

    Connections run with isolation_level=None (autocommit), so without
    this every statement commits on its own and a failure halfway through
    a multi-write operation leaves the database in a partial state -- a
    scan with no exception row, a manifest with only some of its keys.
    Re-entrant: a nested `with transaction(conn)` joins the outer one
    rather than failing on SQLite's "cannot start a transaction within a
    transaction".
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply any migration in migrations/*.sql not yet recorded. Returns
    the list of migration filenames that were newly applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )
    applied = {row["version"] for row in conn.execute(SQL["meta.applied_versions"])}
    newly_applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text()
        # executescript() always commits any pending transaction before it
        # runs, which fights with manual "BEGIN"/"COMMIT" strings under
        # autocommit (isolation_level=None) connections. Python 3.12's
        # explicit `autocommit` attribute gives real manual transaction
        # control that DDL participates in, so the whole migration file
        # applies atomically -- a mid-script failure rolls back cleanly.
        conn.autocommit = False
        try:
            conn.executescript(sql)
            conn.execute(SQL["meta.record_applied_version"], (path.name,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
        newly_applied.append(path.name)
    return newly_applied


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
    row = conn.execute(sql, params).fetchone()
    return row


def query_one_required(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> sqlite3.Row:
    """query_one for statements that cannot legitimately return no row.

    Aggregates (COUNT, MAX, COALESCE(SUM(...))) always produce exactly one
    row, and a lookup by a primary key the caller just wrote is guaranteed
    to find it. Those call sites index the result directly, which is
    correct but untypeable against query_one's honest `Row | None`.

    Raising here rather than sprinkling `assert row is not None` keeps the
    failure legible: if this ever fires, the schema or the statement
    changed underneath the caller, and the message says which statement.
    Asserts would also vanish under `python -O`.
    """
    row = query_one(conn, sql, params)
    if row is None:
        raise LookupError(f"expected exactly one row, got none:\n{sql}")
    return row


def execute(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> int:
    """Run a statement and return the inserted rowid (0 for non-inserts).

    sqlite3 types `lastrowid` as int | None because it is None when the
    statement was not an INSERT. Callers that want an id always follow
    an INSERT; callers that do not, ignore the return value. Collapsing
    None to 0 keeps the signature honest for both without making every
    UPDATE call site handle an Optional it will never see."""
    cur = conn.execute(sql, params)
    return cur.lastrowid or 0


def executemany(conn: sqlite3.Connection, sql: str, seq_of_params: Iterable[Sequence]) -> None:
    conn.executemany(sql, seq_of_params)


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def create_account(
    conn,
    name: str,
    price_per_catch_cents: int,
    free_allowance: int,
    plan: str = "standard",
    loose_match_enabled: bool = False,
    loose_suffix_len: int = 8,
    worker_self_resolve: bool = False,
    timezone: str = "UTC",
) -> int:
    return execute(
        conn,
        SQL["accounts.create_account"],
        (
            name,
            plan,
            price_per_catch_cents,
            free_allowance,
            int(loose_match_enabled),
            loose_suffix_len,
            int(worker_self_resolve),
            timezone,
        ),
    )


def get_account(conn, account_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["accounts.get_account"], (account_id,))


def list_accounts(conn) -> list[sqlite3.Row]:
    return query(conn, SQL["accounts.list_accounts"])


# Columns update_account_fields is allowed to write. This is one of the
# few statements that cannot live in sql/: the SET clause is assembled
# from whichever subset of fields a caller passed, so there is no fixed
# statement text to name. Interpolating caller-supplied keys straight into
# SQL would be an injection primitive, so every key is checked against
# this tuple first -- the allowlist deliberately sits next to the code
# that builds the statement rather than in a distant config.
_ACCOUNT_UPDATABLE_COLUMNS = (
    "name",
    "plan",
    "price_per_catch_cents",
    "free_allowance",
    "loose_match_enabled",
    "loose_suffix_len",
    "worker_self_resolve",
    "timezone",
    "status",
)


def update_account_fields(conn, account_id: int, **fields) -> None:
    if not fields:
        return
    unknown = set(fields) - set(_ACCOUNT_UPDATABLE_COLUMNS)
    if unknown:
        raise ValueError(f"not an updatable account column: {', '.join(sorted(unknown))}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(conn, f"UPDATE accounts SET {cols} WHERE id = ?", (*fields.values(), account_id))


def set_account_status(conn, account_id: int, status: str) -> None:
    execute(conn, SQL["accounts.set_account_status"], (status, account_id))


# ---------------------------------------------------------------------------
# users / auth
# ---------------------------------------------------------------------------


def create_user(conn, account_id: int, email: str, pw_hash: str, role: str = "owner") -> int:
    return execute(
        conn,
        SQL["users.create_user"],
        (account_id, email.lower(), pw_hash, role),
    )


def get_user_by_email(conn, email: str) -> sqlite3.Row | None:
    return query_one(conn, SQL["users.get_user_by_email"], (email.lower(),))


def get_user(conn, user_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["users.get_user"], (user_id,))


def list_users_for_account(conn, account_id: int) -> list[sqlite3.Row]:
    return query(conn, SQL["users.list_users_for_account"], (account_id,))


def update_user_password(conn, user_id: int, pw_hash: str) -> None:
    execute(conn, SQL["users.update_user_password"], (pw_hash, user_id))


def update_user_role(conn, user_id: int, role: str) -> None:
    execute(conn, SQL["users.update_user_role"], (role, user_id))


def update_user_email(conn, user_id: int, email: str) -> None:
    execute(conn, SQL["users.update_user_email"], (email.lower(), user_id))


def count_owners_for_account(conn, account_id: int) -> int:
    row = query_one(conn, SQL["users.count_owners_for_account"], (account_id,))
    return row["n"] if row else 0


def delete_user(conn, user_id: int) -> None:
    """Delete a user and every row that references them directly.

    Refuses to delete the last `owner` on an account -- doing so would
    orphan the account (no one left who can log in to it, and deleting
    the *account* itself is a separate, un-built action). `web_sessions`
    are session state, safe to drop outright;
    `impersonation_tokens.user_id` is nullable and these rows are a
    historical record of an operator action, so they're detached rather
    than deleted.
    """
    user = get_user(conn, user_id)
    if not user:
        return
    if user["role"] == "owner" and count_owners_for_account(conn, user["account_id"]) <= 1:
        raise ValueError("cannot delete the last owner of an account")
    with transaction(conn):
        execute(conn, SQL["users.delete_web_sessions_for_user"], (user_id,))
        execute(conn, SQL["users.detach_impersonation_tokens_for_user"], (user_id,))
        execute(conn, SQL["users.delete_user"], (user_id,))


def create_web_session(conn, user_id: int, account_id: int, token: str, expires_at: str) -> None:
    execute(
        conn,
        SQL["users.create_web_session"],
        (token, user_id, account_id, expires_at),
    )


def get_web_session(conn, token: str) -> sqlite3.Row | None:
    return query_one(conn, SQL["users.get_web_session"], (token,))


def delete_web_session(conn, token: str) -> None:
    execute(conn, SQL["users.delete_web_session"], (token,))


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------


def get_or_create_worker(conn, account_id: int, display_name: str) -> int:
    row = query_one(
        conn,
        SQL["workers.find_worker_by_name"],
        (account_id, display_name),
    )
    if row:
        execute(
            conn,
            SQL["workers.touch_worker_last_seen"],
            (row["id"],),
        )
        return row["id"]
    return execute(
        conn,
        SQL["workers.insert_worker"],
        (account_id, display_name),
    )


def worker_stats_for_account(conn, account_id: int) -> list[sqlite3.Row]:
    """Per-worker scan breakdown and $ billed attributable to their
    rejects -- a worker with a high reject rate is directly costing the
    account per-catch fees, and this is meant to be visible, not a
    hidden surveillance metric (see the "Workers" owner page and the
    end-of-shift summary the worker themselves sees)."""
    return query(
        conn,
        SQL["workers.worker_stats_for_account"],
        (account_id,),
    )


def worker_session_stats(conn, session_id: int) -> sqlite3.Row:
    """Tally for one session -- used for the worker's own end-of-shift
    summary (factual, not dollarized -- see app.py's /w/session-summary).

    Always returns a row: the statement is a bare aggregate, so a session
    with no scans yields zeroes rather than nothing."""
    return query_one_required(
        conn,
        SQL["workers.worker_session_stats"],
        (session_id,),
    )


def get_worker(conn, worker_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["workers.get_worker"], (worker_id,))


def rename_worker(conn, worker_id: int, new_display_name: str) -> None:
    execute(conn, SQL["workers.rename_worker"], (new_display_name, worker_id))


def set_worker_active(conn, worker_id: int, active: bool) -> None:
    execute(conn, SQL["workers.set_worker_active"], (int(active), worker_id))


def merge_workers(conn, from_worker_id: int, into_worker_id: int) -> None:
    """Fold a duplicate worker (e.g. a name-typo second row for the same
    person) into another: every session -- and therefore every scan and
    every billing event, which key off session_id, not worker_id directly
    -- gets reattributed, then the now-empty `from` worker row is deleted.
    Scans/billing_events themselves are never touched (still append-only);
    only the sessions.worker_id foreign key moves."""
    if from_worker_id == into_worker_id:
        raise ValueError("cannot merge a worker into itself")
    # Atomic: reattributing the sessions but failing to delete the source
    # worker leaves a phantom zero-scan worker, and the reverse would
    # orphan every session that pointed at the deleted row.
    with transaction(conn):
        execute(
            conn,
            SQL["workers.reassign_sessions_to_worker"],
            (into_worker_id, from_worker_id),
        )
        execute(conn, SQL["workers.delete_worker"], (from_worker_id,))


# ---------------------------------------------------------------------------
# manifests / manifest_lines / line_keys
# ---------------------------------------------------------------------------


def create_manifest(conn, account_id: int, ref: str, source_filename: str | None) -> int:
    return execute(
        conn,
        SQL["manifests.create_manifest"],
        (account_id, ref, source_filename),
    )


def get_manifest(conn, manifest_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["manifests.get_manifest"], (manifest_id,))


def list_manifests(conn, account_id: int) -> list[sqlite3.Row]:
    return query(conn, SQL["manifests.list_manifests"], (account_id,))


def list_committed_manifests(conn, account_id: int) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["manifests.list_committed_manifests"],
        (account_id,),
    )


def get_manifest_lines_for_account(conn, account_id: int, limit: int = 500) -> list[sqlite3.Row]:
    """Lines across all committed manifests for an account. Capped and
    unfiltered -- superseded by search_manifest_lines_for_account for the
    exception-resolution picker, which needs to scale past 500 lines;
    kept here for callers that just want "some lines," not a search."""
    return query(
        conn,
        SQL["manifests.get_manifest_lines_for_account"],
        (account_id, limit),
    )


def _escape_like(term: str) -> str:
    """Escape SQLite LIKE wildcards in user input so a SKU/barcode that
    genuinely contains '%' or '_' is matched literally instead of those
    characters being treated as pattern metacharacters."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_manifest_lines_for_account(
    conn, account_id: int, search_term: str, limit: int = 20
) -> list[sqlite3.Row]:
    """Type-ahead search for the exception-resolution picker -- matches
    SKU, description, or raw barcode by substring, scoped to committed
    manifests. This is what lets the resolution picker scale past the
    flat cap on get_manifest_lines_for_account (previously a plain
    <select>, capped at 500 lines -- unusable against a real 15k-line
    manifest)."""
    like = f"%{_escape_like(search_term)}%"
    return query(
        conn,
        SQL["manifests.search_manifest_lines_for_account"],
        (account_id, like, like, like, limit),
    )


def insert_manifest_lines(conn, manifest_id: int, rows: list[dict]) -> list[int]:
    ids = []
    for row in rows:
        line_id = execute(
            conn,
            SQL["manifests.insert_manifest_lines"],
            (
                manifest_id,
                row["line_no"],
                row.get("sku"),
                row.get("description"),
                row.get("qty_expected", 1),
                row["raw_barcode"],
            ),
        )
        ids.append(line_id)
    return ids


def get_manifest_lines(conn, manifest_id: int) -> list[sqlite3.Row]:
    return query(conn, SQL["manifests.get_manifest_lines"], (manifest_id,))


def get_manifest_lines_page(
    conn, manifest_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[sqlite3.Row], int]:
    """Paginated in SQL -- a committed manifest can have thousands of
    lines; the edit view must never load them all into memory at once."""
    total = query_one_required(conn, SQL["manifests.count_manifest_lines"], (manifest_id,))["n"]
    rows = query(
        conn,
        SQL["manifests.page_manifest_lines"],
        (manifest_id, limit, offset),
    )
    return rows, total


def get_manifest_line(conn, line_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["manifests.get_manifest_line"], (line_id,))


def next_manifest_line_no(conn, manifest_id: int) -> int:
    row = query_one_required(
        conn,
        SQL["manifests.next_manifest_line_no"],
        (manifest_id,),
    )
    return row["max_no"] + 1


def add_manifest_line(
    conn,
    manifest_id: int,
    sku: str | None,
    description: str | None,
    qty_expected: int,
    raw_barcode: str,
) -> int:
    line_no = next_manifest_line_no(conn, manifest_id)
    line_id = execute(
        conn,
        SQL["manifests.insert_manifest_line"],
        (manifest_id, line_no, sku, description, qty_expected, raw_barcode),
    )
    execute(conn, SQL["manifests.increment_manifest_line_count"], (manifest_id,))
    return line_id


def update_manifest_line(
    conn,
    line_id: int,
    sku: str | None,
    description: str | None,
    qty_expected: int,
    raw_barcode: str,
) -> None:
    execute(
        conn,
        SQL["manifests.update_manifest_line"],
        (sku, description, qty_expected, raw_barcode, line_id),
    )


def delete_manifest_line(conn, line_id: int) -> None:
    line = get_manifest_line(conn, line_id)
    if not line:
        return
    execute(conn, SQL["manifests.delete_line_keys_for_line"], (line_id,))
    execute(conn, SQL["manifests.delete_manifest_line"], (line_id,))
    execute(
        conn,
        SQL["manifests.decrement_manifest_line_count"],
        (line["manifest_id"],),
    )


def get_manifest_line_ids_by_sku(conn, manifest_ids: list[int]) -> dict[str, list[int]]:
    """sku -> [manifest_line_id, ...] across the given manifests, used to
    apply account-learned aliases (which are keyed by sku) to whichever
    manifest lines are active in the current shift bundle."""
    if not manifest_ids:
        return {}
    placeholders = ",".join("?" for _ in manifest_ids)
    rows = query(
        conn,
        f"SELECT id, sku FROM manifest_lines WHERE manifest_id IN ({placeholders}) AND sku IS NOT NULL",
        manifest_ids,
    )
    by_sku: dict[str, list[int]] = {}
    for row in rows:
        by_sku.setdefault(row["sku"], []).append(row["id"])
    return by_sku


def insert_line_keys(conn, rows: list[tuple]) -> None:
    """rows: list of (manifest_line_id, tier, key, collision)"""
    executemany(
        conn,
        SQL["manifests.insert_line_keys"],
        rows,
    )


def get_line_keys_for_manifests(conn, manifest_ids: list[int]) -> list[sqlite3.Row]:
    if not manifest_ids:
        return []
    placeholders = ",".join("?" for _ in manifest_ids)
    return query(
        conn,
        f"SELECT lk.manifest_line_id, lk.tier, lk.key, lk.collision "
        f"FROM line_keys lk JOIN manifest_lines ml ON ml.id = lk.manifest_line_id "
        f"WHERE ml.manifest_id IN ({placeholders})",
        manifest_ids,
    )


def set_manifest_status(conn, manifest_id: int, status: str, line_count: int | None = None) -> None:
    if line_count is not None:
        execute(
            conn,
            SQL["manifests.set_manifest_status_and_count"],
            (status, line_count, manifest_id),
        )
    else:
        execute(conn, SQL["manifests.set_manifest_status"], (status, manifest_id))


def bump_manifest_bundle_version(conn, manifest_id: int) -> int:
    execute(
        conn,
        SQL["manifests.bump_manifest_bundle_version"],
        (manifest_id,),
    )
    # The row is guaranteed: the UPDATE above just touched it.
    return query_one_required(conn, SQL["manifests.get_manifest"], (manifest_id,))["bundle_version"]


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------


def add_alias(conn, account_id: int, normalized_key: str, sku: str, confirmed_by: str) -> int:
    return execute(
        conn,
        SQL["aliases.add_alias"],
        (account_id, normalized_key, sku, confirmed_by),
    )


def list_aliases(conn, account_id: int) -> list[sqlite3.Row]:
    return query(conn, SQL["aliases.list_aliases"], (account_id,))


# ---------------------------------------------------------------------------
# shifts / shift_manifests / sessions
# ---------------------------------------------------------------------------


def create_shift(
    conn,
    account_id: int,
    label: str,
    date: str,
    token: str,
    token_expires_at: str,
    bundle_hash: str,
    bundle_version: int,
) -> int:
    return execute(
        conn,
        SQL["shifts.create_shift"],
        (account_id, label, date, token, token_expires_at, bundle_hash, bundle_version),
    )


def get_shift(conn, shift_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["shifts.get_shift"], (shift_id,))


def get_shift_by_token(conn, token: str) -> sqlite3.Row | None:
    return query_one(conn, SQL["shifts.get_shift_by_token"], (token,))


def list_shifts(conn, account_id: int) -> list[sqlite3.Row]:
    return query(conn, SQL["shifts.list_shifts"], (account_id,))


def revoke_shift(conn, shift_id: int) -> None:
    execute(
        conn,
        SQL["shifts.revoke_shift"],
        (shift_id,),
    )


def link_shift_manifest(conn, shift_id: int, manifest_id: int) -> None:
    execute(
        conn,
        SQL["shifts.link_shift_manifest"],
        (shift_id, manifest_id),
    )


def get_shift_manifest_ids(conn, shift_id: int) -> list[int]:
    rows = query(conn, SQL["shifts.get_shift_manifest_ids"], (shift_id,))
    return [r["manifest_id"] for r in rows]


def get_shifts_for_manifest(conn, manifest_id: int) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["shifts.get_shifts_for_manifest"],
        (manifest_id,),
    )


def bump_shift_bundle_version(conn, shift_id: int) -> int:
    execute(conn, SQL["shifts.bump_shift_bundle_version"], (shift_id,))
    # The row is guaranteed: the UPDATE above just touched it.
    return query_one_required(conn, SQL["shifts.get_shift"], (shift_id,))["bundle_version"]


def list_sessions_for_shift(conn, shift_id: int) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["shifts.list_sessions_for_shift"],
        (shift_id,),
    )


def create_session(conn, shift_id: int, worker_id: int, device_ua: str) -> int:
    token = secrets.token_urlsafe(24)
    return execute(
        conn,
        SQL["shifts.create_session"],
        (shift_id, worker_id, device_ua, token),
    )


def get_session(conn, session_id: int) -> sqlite3.Row | None:
    return query_one(conn, SQL["shifts.get_session"], (session_id,))


def get_session_by_token(conn, token: str) -> sqlite3.Row | None:
    """Session lookup for worker-facing routes -- the client's URLs/JSON
    carry this opaque token, never the raw integer id, so a guessed or
    incremented id can't be used to act as someone else's scan session."""
    return query_one(conn, SQL["shifts.get_session_by_token"], (token,))


def touch_session(conn, session_id: int, clock_skew_ms: int | None = None) -> None:
    if clock_skew_ms is not None:
        execute(
            conn,
            SQL["shifts.touch_session_with_skew"],
            (clock_skew_ms, session_id),
        )
    else:
        execute(
            conn,
            SQL["shifts.touch_session"],
            (session_id,),
        )


# ---------------------------------------------------------------------------
# scans (append-only, idempotent on uuid)
# ---------------------------------------------------------------------------


def scan_exists(conn, uuid: str) -> bool:
    return query_one(conn, SQL["shifts.scan_exists"], (uuid,)) is not None


def insert_scan(conn, scan: dict) -> bool:
    """Idempotent insert keyed on uuid. Returns True if a new row was
    inserted, False if this uuid was already present (a replay)."""
    if scan_exists(conn, scan["uuid"]):
        return False
    execute(
        conn,
        SQL["shifts.insert_scan"],
        (
            scan["uuid"],
            scan["session_id"],
            scan.get("manifest_line_id"),
            scan["raw_payload"],
            scan["normalized"],
            scan.get("matched_tier"),
            scan["result"],
            scan.get("decode_ms"),
            scan.get("match_ms"),
            scan["ts_client"],
            scan.get("ts_server"),
            scan["bundle_version"],
            scan.get("seq"),
            scan.get("device_ua"),
        ),
    )
    return True


def get_scan(conn, uuid: str) -> sqlite3.Row | None:
    return query_one(conn, SQL["shifts.get_scan"], (uuid,))


def scans_for_line(conn, manifest_line_id: int) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["shifts.scans_for_line"],
        (manifest_line_id,),
    )


def count_scans_today(conn, account_id: int, day_start: str, day_end: str) -> int:
    """Scans in the half-open UTC range [day_start, day_end).

    The caller supplies the bounds (see tz.local_day_bounds_utc) because
    "today" depends on the account's timezone -- comparing
    date(ts_server) to date('now') buckets every account by the UTC day,
    which rolls over mid-shift for anyone west of Greenwich.
    """
    row = query_one(
        conn,
        SQL["shifts.count_scans_today"],
        (account_id, day_start, day_end),
    )
    return row["n"] if row else 0


def count_active_shifts(conn, account_id: int) -> int:
    row = query_one(
        conn,
        SQL["shifts.count_active_shifts"],
        (account_id,),
    )
    return row["n"] if row else 0


def count_open_exceptions(conn, account_id: int) -> int:
    row = query_one(
        conn,
        SQL["shifts.count_open_exceptions"],
        (account_id,),
    )
    return row["n"] if row else 0


def recent_scans(
    conn, account_id: int, limit: int = 200, after_id: str | None = None
) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["shifts.recent_scans"],
        (account_id, limit),
    )


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------


def create_exception(conn, scan_uuid: str, kind: str, manifest_line_id: int | None = None) -> int:
    return execute(
        conn,
        SQL["exceptions.create_exception"],
        (scan_uuid, kind, manifest_line_id),
    )


def create_worker_appeal(
    conn, scan_uuid: str, photo_path: str, worker_note: str | None
) -> int | None:
    """Idempotent: a worker's phone may retry the upload (it's queued the
    same way outbound scans are, for offline safety), so if a
    'worker_reported' exception already exists for this scan, this is a
    no-op rather than a duplicate row."""
    existing = query_one(
        conn,
        SQL["exceptions.find_existing_worker_appeal"],
        (scan_uuid,),
    )
    if existing:
        return None
    return execute(
        conn,
        SQL["exceptions.insert_worker_appeal"],
        (scan_uuid, photo_path, worker_note),
    )


def resolve_exception(
    conn, exception_id: int, resolved_by: str, note: str, manifest_line_id: int | None = None
) -> None:
    execute(
        conn,
        SQL["exceptions.resolve_exception"],
        (resolved_by, note, manifest_line_id, exception_id),
    )


def list_open_exceptions(conn, account_id: int) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["exceptions.list_open_exceptions"],
        (account_id,),
    )


def get_exception_for_scan(conn, scan_uuid: str) -> sqlite3.Row | None:
    return query_one(conn, SQL["exceptions.get_exception_for_scan"], (scan_uuid,))


# ---------------------------------------------------------------------------
# billing_events / account_credits
# ---------------------------------------------------------------------------


def insert_billing_event(
    conn, account_id: int, scan_uuid: str, kind: str, cents: int, billable: bool = True
) -> int:
    return execute(
        conn,
        SQL["billing.insert_billing_event"],
        (account_id, scan_uuid, kind, cents, int(billable)),
    )


def billing_event_exists_for_scan(conn, scan_uuid: str, kind: str = "catch") -> bool:
    return (
        query_one(conn, SQL["billing.billing_event_exists_for_scan"], (scan_uuid, kind)) is not None
    )


def billing_events_for_account(
    conn, account_id: int, start: str | None = None, end: str | None = None
) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["billing.billing_events_for_account"],
        (account_id, start, start, end, end),
    )


def count_billable_catches(conn, account_id: int) -> int:
    """Catches that still stand -- i.e. excluding any that were later
    reversed.

    A reversal means the scan turned out NOT to be a mis-ship (an approved
    worker appeal, or an owner correcting a mistaken reject). Counting
    those would charge twice over: once by consuming a slot of the free
    allowance so the next genuine catch gets billed early
    (billing._insert_catch), and again by overstating "catches" and
    "money saved" on the owner dashboard and savings report.
    """
    row = query_one(
        conn,
        SQL["billing.count_billable_catches"],
        (account_id,),
    )
    return row["n"] if row else 0


def grant_credit(conn, account_id: int, cents: int, reason: str, granted_by: str) -> int:
    return execute(
        conn,
        SQL["billing.grant_credit"],
        (account_id, cents, reason, granted_by),
    )


def credits_for_account(conn, account_id: int) -> list[sqlite3.Row]:
    return query(
        conn,
        SQL["billing.credits_for_account"],
        (account_id,),
    )


def total_credit_cents(
    conn, account_id: int, start: str | None = None, end: str | None = None
) -> int:
    """Credits granted to an account, optionally restricted to a window.

    The window matters once anything invoices per period: without it a
    caller asking for one month's balance would subtract every credit
    ever granted from that single month (and from every other month too).
    """
    row = query_one(
        conn,
        SQL["billing.credits_total_for_account"],
        (account_id, start, start, end, end),
    )
    return row["total"] if row else 0


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------


def record_audit(
    conn,
    actor: str,
    action: str,
    target_table: str | None = None,
    target_id: str | None = None,
    before: Any | None = None,
    after: Any | None = None,
    ip: str | None = None,
) -> int:
    """Write the audit row. Callers that perform a privileged mutation MUST
    call this -- and let it commit -- BEFORE performing the mutation, per
    the "audit before action, not best-effort" invariant. This function
    commits synchronously (autocommit connection) so the row is durable
    even if the caller's subsequent action fails.
    """
    return execute(
        conn,
        SQL["audit.record_audit"],
        (
            actor,
            action,
            target_table,
            target_id,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            ip,
        ),
    )


def list_audit_log(conn, limit: int = 200) -> list[sqlite3.Row]:
    return query(conn, SQL["audit.list_audit_log"], (limit,))


def list_audit_log_for_manifest(conn, manifest_id: int) -> list[sqlite3.Row]:
    """Every audit_log entry touching one manifest: the commit itself
    (target_table='manifests', target_id=the manifest's own id) plus
    every line add/edit/delete against it (target_table='manifest_lines',
    target_id=the LINE's id instead -- manifest_id only lives inside the
    before/after JSON blob for those, via json_extract, since the line id
    is what actually identifies the row that changed).
    """
    return query(
        conn,
        SQL["audit.list_audit_log_for_manifest"],
        (str(manifest_id), manifest_id, manifest_id),
    )


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------


def _window_start(window_seconds: int) -> str:
    from datetime import datetime, timedelta

    started = datetime.now(UTC) - timedelta(seconds=window_seconds)
    return started.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def count_rate_limit_events(conn, bucket: str, key: str, window_seconds: int) -> int:
    """How many events are recorded for this bucket+key inside the window.

    Counting rows rather than reading a mutable counter is deliberate:
    under the documented multi-worker gunicorn deployment two processes
    can record a hit at the same instant, and a counter column would need
    a read-modify-write that could lose one of them. Independent INSERTs
    can't lose each other.
    """
    row = query_one(
        conn,
        SQL["rate_limits.count_rate_limit_events"],
        (bucket, key, _window_start(window_seconds)),
    )
    return row["n"] if row else 0


def record_rate_limit_event(conn, bucket: str, key: str) -> int:
    return execute(conn, SQL["rate_limits.record_rate_limit_event"], (bucket, key))


def clear_rate_limit_events(conn, bucket: str, key: str) -> None:
    """Drop this bucket+key's history -- used on a successful login so a
    few mistyped passwords before a correct one don't count against the
    user later."""
    execute(conn, SQL["rate_limits.clear_rate_limit_events"], (bucket, key))


def purge_expired_rate_limit_events(conn, older_than_seconds: int) -> int:
    """Housekeeping so the table doesn't grow without bound. Safe to call
    opportunistically -- anything older than the longest active window is
    already irrelevant to every limiter."""
    cur = conn.execute(
        SQL["rate_limits.purge_expired_rate_limit_events"], (_window_start(older_than_seconds),)
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# impersonation tokens
# ---------------------------------------------------------------------------


def create_impersonation_token(conn, account_id: int, user_id: int | None, expires_at: str) -> str:
    token = secrets.token_urlsafe(32)
    execute(
        conn,
        SQL["impersonation.create_impersonation_token"],
        (token, account_id, user_id, expires_at),
    )
    return token


def redeem_impersonation_token(conn, token: str, ip: str) -> sqlite3.Row | None:
    row = query_one(
        conn,
        SQL["impersonation.get_unredeemed_impersonation_token"],
        (token,),
    )
    if not row:
        return None
    execute(
        conn,
        SQL["impersonation.mark_impersonation_token_redeemed"],
        (ip, token),
    )
    return row


# ---------------------------------------------------------------------------
# generic table browsing (operator GUI "Tables" tab)
# ---------------------------------------------------------------------------

KNOWN_TABLES = [
    "accounts",
    "users",
    "workers",
    "manifests",
    "manifest_lines",
    "line_keys",
    "aliases",
    "shifts",
    "shift_manifests",
    "sessions",
    "scans",
    "exceptions",
    "billing_events",
    "account_credits",
    "audit_log",
    "impersonation_tokens",
    "web_sessions",
    "rate_limit_events",
]


def table_columns(conn, table: str) -> list[str]:
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table: {table}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def table_page(
    conn,
    table: str,
    limit: int = 100,
    offset: int = 0,
    order_by: str | None = None,
    order_dir: str = "DESC",
    filters: dict | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Paginate a table entirely in SQL -- never loads the full table."""
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table: {table}")
    columns = table_columns(conn, table)
    where_clauses = []
    params: list = []
    if filters:
        for col, val in filters.items():
            if col not in columns or val in (None, ""):
                continue
            where_clauses.append(f"{col} LIKE ?")
            params.append(f"%{val}%")
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    count_row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where_sql}", params).fetchone()
    total = count_row["n"]

    # rowid is always appended as a tiebreaker (every KNOWN_TABLES table is
    # an ordinary rowid table, none are WITHOUT ROWID) -- without it, a
    # column that isn't unique, or no order_by at all, leaves row order
    # across separate LIMIT/OFFSET calls unspecified by SQLite. That was
    # latent but harmless while each call stood alone; it became a real
    # bug once callers (e.g. admin_gui.py's CSV export) started paging
    # through multiple calls expecting a stable total ordering.
    order_parts = []
    if order_by in columns:
        direction = "DESC" if order_dir.upper() == "DESC" else "ASC"
        order_parts.append(f"{order_by} {direction}")
    # DESC so an unsorted view still reads newest-first (matches every
    # other "recent activity" listing in this codebase, e.g. Live feed's
    # "ORDER BY ts_server DESC") -- not just a tiebreaker default, a
    # deliberate choice about what an operator wants to see first when
    # they haven't picked a sort column themselves.
    order_parts.append("rowid DESC")
    order_sql = "ORDER BY " + ", ".join(order_parts)
    rows = conn.execute(
        f"SELECT * FROM {table} {where_sql} {order_sql} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return rows, total
