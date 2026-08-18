"""Tests for the named-SQL loader.

The loader's whole job is to fail loudly at import time rather than
quietly at query time, so most of what is worth testing here is which
malformed inputs raise.
"""

from __future__ import annotations

import pytest

import sqlstore


def _write(tmp_path, name: str, body: str):
    (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_loads_named_statements_namespaced_by_filename(tmp_path):
    _write(
        tmp_path,
        "widgets.sql",
        "-- name: get\nSELECT 1;\n\n-- name: put\nINSERT INTO t VALUES (1);\n",
    )
    loaded = sqlstore.load(tmp_path)
    assert loaded == {
        "widgets.get": "SELECT 1;",
        "widgets.put": "INSERT INTO t VALUES (1);",
    }


def test_same_name_in_two_files_does_not_collide(tmp_path):
    """The filename prefix is what makes `list_for_account` reusable."""
    _write(tmp_path, "a.sql", "-- name: shared\nSELECT 'a';\n")
    _write(tmp_path, "b.sql", "-- name: shared\nSELECT 'b';\n")
    loaded = sqlstore.load(tmp_path)
    assert loaded["a.shared"] == "SELECT 'a';"
    assert loaded["b.shared"] == "SELECT 'b';"


def test_comments_under_a_marker_are_kept_with_the_statement(tmp_path):
    """A query's explanation should travel with it into a stack trace."""
    _write(tmp_path, "a.sql", "-- name: q\n-- why this exists\nSELECT 1;\n")
    assert "-- why this exists" in sqlstore.load(tmp_path)["a.q"]


def test_duplicate_name_in_one_file_is_an_error(tmp_path):
    """Silently keeping the last one would mean an edit to the first
    statement has no effect, with nothing to indicate why."""
    _write(tmp_path, "a.sql", "-- name: q\nSELECT 1;\n-- name: q\nSELECT 2;\n")
    with pytest.raises(sqlstore.SQLParseError, match="duplicate"):
        sqlstore.load(tmp_path)


def test_marker_with_no_statement_is_an_error(tmp_path):
    _write(tmp_path, "a.sql", "-- name: q\n\n-- name: r\nSELECT 1;\n")
    with pytest.raises(sqlstore.SQLParseError, match="no statement"):
        sqlstore.load(tmp_path)


def test_sql_before_the_first_marker_is_an_error(tmp_path):
    """Unaddressable SQL is a mistake, not a header comment."""
    _write(tmp_path, "a.sql", "SELECT 'orphan';\n-- name: q\nSELECT 1;\n")
    with pytest.raises(sqlstore.SQLParseError, match="before the first"):
        sqlstore.load(tmp_path)


def test_header_comment_before_the_first_marker_is_fine(tmp_path):
    _write(tmp_path, "a.sql", "-- what this file is for\n\n-- name: q\nSELECT 1;\n")
    assert sqlstore.load(tmp_path) == {"a.q": "SELECT 1;"}


def test_missing_directory_is_an_error(tmp_path):
    with pytest.raises(sqlstore.SQLParseError, match="not found"):
        sqlstore.load(tmp_path / "nope")


def test_empty_directory_is_an_error(tmp_path):
    """An empty sql/ means a packaging mistake -- the app would otherwise
    start and then KeyError on its first database call."""
    with pytest.raises(sqlstore.SQLParseError, match="no named statements"):
        sqlstore.load(tmp_path)


def test_name_marker_must_be_at_the_start_of_a_line(tmp_path):
    """Otherwise a '-- name:' mentioned inside a statement's own comment
    would split it in two."""
    _write(tmp_path, "a.sql", "-- name: q\nSELECT 1;  -- name: not_a_marker\n")
    loaded = sqlstore.load(tmp_path)
    assert list(loaded) == ["a.q"]


def test_every_shipped_statement_is_valid_sqlite(tmp_path):
    """Compile each real statement against the real schema.

    A typo in sql/*.sql -- a misspelled column, a table renamed by a
    migration -- would otherwise only surface when the one route that uses
    that statement is exercised. EXPLAIN forces SQLite to prepare the
    statement (resolving every table and column name) without running it.

    Only OperationalError is a failure. ProgrammingError means SQLite
    compiled the statement and then objected that no parameters were
    bound, which is precisely the outcome this test wants: it got far
    enough to count the placeholders.
    """
    import sqlite3

    import db

    conn = db.init_db(tmp_path / "syntax.db")
    try:
        for key, text in sorted(sqlstore.SQL.items()):
            try:
                conn.execute("EXPLAIN " + text)
            except sqlite3.ProgrammingError:
                pass  # compiled; only the bindings were missing
            except sqlite3.OperationalError as exc:  # pragma: no cover - failure path
                pytest.fail(f"{key} is not valid against the current schema: {exc}")
    finally:
        conn.close()


def _sql_keys_used_in_source() -> dict[str, set[str]]:
    """Every SQL["..."] subscript in the project, by filename.

    Parsed from the AST rather than grepped: both db.py and sqlstore.py
    show example lookups inside their docstrings -- sqlstore's is a
    deliberate typo demonstrating the failure mode -- and a regex cannot
    tell those from real code.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    found: dict[str, set[str]] = {}
    for py in sorted(root.glob("*.py")) + sorted((root / "tools").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        keys = {
            node.slice.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "SQL"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        }
        if keys:
            found[py.name] = keys
    return found


def test_every_sql_reference_in_the_codebase_resolves():
    """Every SQL["file.name"] in the project names a real statement.

    Parsing sql/*.sql at import validates the files, but NOT the keys used
    to look statements up: `SQL["accounts.get_acount"]` sits inside a
    function body and raises KeyError only when that line runs. An
    unreferenced typo can therefore survive both import and a test run
    that never exercises the one route using it -- which happened during
    the migration to this loader, when app.py briefly referenced five
    statements that had not been written yet and everything still
    imported cleanly.
    """
    missing = [
        f"{name}: {key}"
        for name, keys in _sql_keys_used_in_source().items()
        for key in sorted(keys)
        if key not in sqlstore.SQL
    ]
    assert not missing, "SQL keys with no matching statement:\n  " + "\n  ".join(missing)


def test_no_shipped_statement_is_unreferenced():
    """Nothing in sql/ is dead weight.

    An orphaned statement is usually the residue of a rename: the caller
    moved to a new name and the old block stayed behind, where it will be
    read as current by the next person.
    """
    referenced: set[str] = set()
    for keys in _sql_keys_used_in_source().values():
        referenced |= keys
    assert not (set(sqlstore.SQL) - referenced), sorted(set(sqlstore.SQL) - referenced)
