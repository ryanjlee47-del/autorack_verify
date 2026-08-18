"""Load named SQL statements from sql/*.sql so no query text lives in Python.

Usage:

    from sqlstore import SQL

    def get_account(conn, account_id):
        return query_one(conn, SQL["accounts.get_account"], (account_id,))

Statements are keyed "<file stem>.<name>", so `sql/accounts.sql`'s
`-- name: get_account` becomes `accounts.get_account`. The file prefix
means two entities can each have a `list_for_account` without colliding,
and a key tells you which file to open.

Why the SQL is out of Python at all
-----------------------------------
Three things get easier once query text lives in .sql files:

  - A query can be pasted straight into a SQLite shell or the operator
    GUI's SQL console to see what it does, with no string-concatenation to
    undo first.
  - Editors syntax-highlight it, and `git diff` on a schema change shows
    the queries that changed alongside the migration that changed them.
  - Reviewing "what does this application actually ask the database" is
    reading one directory instead of grepping 1,400 lines of Python for
    quote characters.

Why statements are validated at import, not at first use
--------------------------------------------------------
`SQL["accounts.get_acount"]` (note the typo) must fail when the process
starts, not the first time a warehouse owner opens the page that calls it.
This module therefore parses every file eagerly and `SQL` is a plain dict
lookup that raises KeyError immediately. The cost is a few milliseconds at
import; the alternative is a typo that survives testing and surfaces at
3am on a loading dock.

Why there is no templating
--------------------------
The loader returns statement text verbatim and offers no way to
interpolate into it. Every parameter must go through sqlite3's own `?`
binding. A `{table}`-style placeholder would be convenient for the handful
of statements this codebase builds dynamically -- and would also be a SQL
injection primitive one careless call site away. Those few dynamic
statements stay in Python instead, where the allowlist that makes them
safe sits in the same function that assembles the text: see db.table_page,
which rejects any table not in db.KNOWN_TABLES and any filter or sort
column not returned by db.table_columns() for that table.
"""

from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"

# A name marker owns every line until the next marker. Anchored to the
# start of a line so an occurrence inside a string literal or a trailing
# comment cannot start a new statement.
_NAME_RE = re.compile(r"^--\s*name:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")


class SQLParseError(Exception):
    """Raised when a .sql file cannot be read as named statements.

    A dedicated type rather than ValueError so a caller that wants to
    report "your SQL directory is malformed" can catch exactly that, and
    so the traceback names the problem at a glance.
    """


def _parse_file(path: Path) -> dict[str, str]:
    """Split one .sql file into {name: statement text}.

    Comment lines between a marker and the statement it names are kept in
    the returned text. They are the query's documentation, and dropping
    them here would mean the explanation is only visible in the file --
    but anyone debugging is more likely to be looking at a stack trace
    that prints the statement.
    """
    statements: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        text = "\n".join(buffer).strip()
        if not text:
            raise SQLParseError(f"{path.name}: '-- name: {current}' has no statement under it")
        if current in statements:
            raise SQLParseError(f"{path.name}: duplicate statement name '{current}'")
        statements[current] = text

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = _NAME_RE.match(line)
        if match:
            flush()
            current = match.group(1)
            buffer = []
            continue
        if current is None:
            # Text before the first marker is the file's header comment.
            # Anything else there is a statement nobody can address by
            # name, which is a mistake worth naming rather than ignoring.
            if line.strip() and not line.lstrip().startswith("--"):
                raise SQLParseError(f"{path.name}:{lineno}: SQL before the first '-- name:' marker")
            continue
        buffer.append(line)

    flush()
    return statements


def load(sql_dir: Path = SQL_DIR) -> dict[str, str]:
    """Parse every .sql file in `sql_dir` into one flat namespaced dict."""
    if not sql_dir.is_dir():
        raise SQLParseError(f"SQL directory not found: {sql_dir}")

    loaded: dict[str, str] = {}
    for path in sorted(sql_dir.glob("*.sql")):
        for name, text in _parse_file(path).items():
            loaded[f"{path.stem}.{name}"] = text

    if not loaded:
        raise SQLParseError(f"no named statements found in {sql_dir}")
    return loaded


# Parsed once at import. Module-level state is appropriate here: the files
# are read-only application assets that cannot change while the process
# runs, and re-reading them per query would put filesystem I/O on the path
# of every database call a phone makes.
SQL: dict[str, str] = load()
