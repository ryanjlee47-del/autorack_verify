#!/usr/bin/env python3
"""Wipe every account and all operational data, leaving an empty schema.

For clearing test data out of a deployment -- e.g. after trying the app
out on PythonAnywhere -- without hand-deleting rows in the SQL console or
losing the schema.

    python reset_data.py --db data/app.db          # dry run: shows counts
    python reset_data.py --db data/app.db --yes    # actually wipe

THIS IS NOT REVERSIBLE. It deletes all accounts, users, workers,
manifests, shifts, sessions, scans, exceptions, billing events, credits,
the audit log, and every saved appeal photo. A timestamped backup is
taken first (unless --no-backup) via backup.py, into backup_data/.

What it does NOT touch:
  - the schema itself (migrations stay applied, so the app boots fine)
  - data/secret_key      -- deleting it would log out nothing, but would
                            invalidate CSRF tokens mid-session for no gain
  - data/admin_api_token -- deleting it would break the operator GUI's
                            saved remote connection

After running this the app has zero accounts. Sign up again at /signup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import backup
import db
from sqlstore import SQL

# Order matters: children before parents, so foreign keys stay satisfied
# at every step (the connection runs with PRAGMA foreign_keys=ON).
WIPE_ORDER = [
    "line_keys",
    "aliases",
    "manifest_lines",
    "shift_manifests",
    "billing_events",
    "account_credits",
    "exceptions",
    "scans",
    "sessions",
    "shifts",
    "manifests",
    "workers",
    "impersonation_tokens",
    "web_sessions",
    "rate_limit_events",
    "audit_log",
    "users",
    "accounts",
]


def counts(conn) -> dict[str, int]:
    out = {}
    for table in WIPE_ORDER:
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out


def append_only_triggers(conn) -> list[tuple[str, str]]:
    """`scans`, `billing_events` and `audit_log` carry BEFORE DELETE
    triggers that RAISE(ABORT) -- they are append-only by design, which is
    load-bearing for billing integrity and the audit trail.

    A wipe is the one legitimate exception, so the triggers are dropped
    and then recreated verbatim from the definitions captured here. They
    are read back out of sqlite_master rather than hardcoded so this keeps
    working if a later migration changes them.
    """
    return [(r["name"], r["sql"]) for r in conn.execute(SQL["meta.append_only_triggers"])]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db", type=str, default=None, help="Path to the database (default: data/app.db)"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform the wipe. Without this, only reports what would be deleted.",
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="Skip the safety backup taken before wiping."
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else db.DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"No database at {db_path} -- nothing to reset.")
        return 0

    conn = db.init_db(db_path)
    before = counts(conn)
    total = sum(before.values())

    print(f"Database: {db_path}")
    if total == 0:
        print("Already empty -- nothing to do.")
        return 0
    print("\nRows that will be DELETED:")
    for table, n in before.items():
        if n:
            print(f"  {table:24} {n}")
    print(f"  {'TOTAL':24} {total}")

    photos_dir = db_path.parent / "appeal_photos"
    photo_files = [p for p in photos_dir.glob("*") if p.is_file()] if photos_dir.exists() else []
    if photo_files:
        print(f"\nAppeal photos that will be DELETED: {len(photo_files)} file(s) in {photos_dir}")

    if not args.yes:
        print("\nDry run -- nothing was changed. Re-run with --yes to actually wipe.")
        return 0

    if not args.no_backup:
        result = backup.backup_everything(db_path)
        print(f"\nSafety backup written: {result['db']}")
        if result["photos"]:
            print(f"Appeal photos backed up: {result['photos']}")

    triggers = append_only_triggers(conn)
    with db.transaction(conn):
        for name, _sql in triggers:
            conn.execute(f"DROP TRIGGER {name}")
        for table in WIPE_ORDER:
            conn.execute(f"DELETE FROM {table}")
        for _name, sql in triggers:
            conn.execute(sql)
    # No table in this schema uses AUTOINCREMENT, so there is no
    # sqlite_sequence to clear -- plain INTEGER PRIMARY KEY reuses ids
    # from 1 once the table is empty, which is what we want anyway.

    restored = {name for name, _ in append_only_triggers(conn)}
    missing = {name for name, _ in triggers} - restored
    if missing:
        print(f"ERROR: append-only triggers not restored: {sorted(missing)}", file=sys.stderr)
        return 1

    for p in photo_files:
        p.unlink()
    conn.execute("VACUUM")

    after = counts(conn)
    remaining = sum(after.values())
    print(f"\nDone. {total} row(s) deleted, {remaining} remaining.")
    if remaining:
        print("WARNING: some rows survived -- inspect manually.", file=sys.stderr)
        return 1
    print("The app now has zero accounts. Sign up again at /signup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
