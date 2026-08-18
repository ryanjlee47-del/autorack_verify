"""Render an entity-relationship diagram of the Autorack Verify schema to PNG.

Run it with no arguments to refresh the checked-in diagram:

    python tools/generate_erd.py            # -> docs/erd.png

Why this script exists at all
-----------------------------
A hand-drawn ERD is wrong the moment someone adds a migration, and nobody
notices until it misleads a reader. This generates the picture from the
schema itself, so a stale diagram is a one-command fix rather than a
redraw.

Why it introspects a database instead of parsing migrations/*.sql
-----------------------------------------------------------------
Migrations move forward only, and they do not merely accumulate: 0007 adds
email-verification columns and 0008 drops them again. Reading the .sql
files in order and unioning their CREATE/ALTER statements therefore
produces a schema that never existed. Applying them to a throwaway
database and reading PRAGMA table_info / foreign_key_list gives the schema
as it actually ends up -- the same thing db.init_db() hands the running
app. The throwaway database lives in a temp dir and is deleted on exit, so
this never touches data/app.db.

Why Pillow and not graphviz
---------------------------
graphviz is a system binary, not a Python package, and this project's
deployment story (a small on-prem box, a fixed dependency list) does not
have room for one more thing to install before the docs build. Pillow is
pure-pip and dev-only -- it is in requirements-dev.txt, not
requirements.txt, because nothing the server serves at runtime imports it.

Layout
------
Tables are placed in columns by foreign-key depth: a table sits one column
to the right of the furthest-right table it references. That puts
`accounts` on the left, the things it owns next, and the append-only
scan/billing tail on the right, which matches the direction data actually
flows through the system. It is not a general-purpose graph layout and it
would not survive a cyclic schema -- see _assign_columns.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# tools/ is not a package and this script is run directly, so the project
# root has to go on sys.path before `import db` can work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

# ---------------------------------------------------------------------------
# Palette. Taken from static/css/tokens.css so the diagram looks like it
# belongs to the same product as the web surfaces, rather than like a
# generic tool's default output.
# ---------------------------------------------------------------------------
CONCRETE = "#e4e2dc"  # page background -- warehouse floor grey
INK = "#14171a"  # near-black body text
STEEL = "#3d4552"  # borders, secondary text
PAPER = "#f7f6f3"  # table body fill
SAFETY_YELLOW = "#f4b400"
DOCK_ORANGE = "#d9541f"
VERIFIED_GREEN = "#1e7d3c"
REJECT_RED = "#c62828"
MUTED = "#8a8f98"

# Header colour per functional group. Grouping is editorial, not derived:
# the schema has no notion of "this table is about billing", but a reader
# does, and colour is the cheapest way to carry that.
GROUP_COLORS: dict[str, str] = {
    "tenancy": STEEL,  # who the customer is, who can log in
    "catalog": VERIFIED_GREEN,  # what is supposed to ship
    "floor": SAFETY_YELLOW,  # what happened on the floor during a shift
    "money": REJECT_RED,  # what gets charged, and the proof behind it
    "ops": MUTED,  # operational plumbing with no business meaning
}

TABLE_GROUPS: dict[str, str] = {
    "accounts": "tenancy",
    "users": "tenancy",
    "web_sessions": "tenancy",
    "impersonation_tokens": "tenancy",
    "manifests": "catalog",
    "manifest_lines": "catalog",
    "line_keys": "catalog",
    "aliases": "catalog",
    "workers": "floor",
    "shifts": "floor",
    "shift_manifests": "floor",
    "sessions": "floor",
    "scans": "floor",
    "exceptions": "money",
    "billing_events": "money",
    "account_credits": "money",
    "audit_log": "ops",
    "rate_limit_events": "ops",
    "schema_migrations": "ops",
}

# Tables whose rows can never be updated or deleted, enforced by BEFORE
# UPDATE/DELETE triggers in migrations/0001_initial.sql rather than by
# application discipline. This is the single most load-bearing property of
# the schema -- billing integrity depends on history not being editable --
# so the diagram marks it explicitly instead of leaving a reader to grep
# the migrations for triggers.
APPEND_ONLY_TABLES = frozenset({"scans", "billing_events", "audit_log"})

# Notes rendered under a table's column list. Reserved for facts a reader
# cannot deduce from column names and foreign keys, which is the only kind
# of annotation worth the vertical space.
TABLE_NOTES: dict[str, str] = {
    "scans": "uuid is client-generated; sync is idempotent on it",
    "billing_events": "BEFORE INSERT trigger: 'catch' requires\nresult='reject' or a resolved exception",
    "audit_log": "no FKs on purpose: it must outlive\nthe rows it describes",
    "line_keys": "one row per (line, match tier);\ncollision=1 excludes a key from bundles",
    "sessions": "token is the worker's opaque credential;\nthe integer id is never exposed",
    "workers": "no password: a worker types a name at\n/w/join. Attribution, not authentication",
    "rate_limit_events": "append-only counting rows, shared\nacross gunicorn workers",
}

# ---------------------------------------------------------------------------
# Geometry. Pixel constants, tuned by eye against the real table list.
# ---------------------------------------------------------------------------
MARGIN = 48
COL_GAP = 108  # horizontal space between columns, where FK edges route
ROW_GAP = 34
BOX_WIDTH = 268
HEADER_H = 30
ROW_H = 19
NOTE_LINE_H = 15
PADDING_X = 10
TITLE_H = 108
LEGEND_H = 92
SCALE = 2  # supersampling factor; see render()


@dataclass
class Column:
    """One column of one table, as reported by PRAGMA table_info."""

    name: str
    type_: str
    not_null: bool
    is_pk: bool
    is_fk: bool = False


@dataclass
class Table:
    """One table, plus the layout state the renderer assigns to it."""

    name: str
    columns: list[Column]
    # (local column, referenced table, referenced column)
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)
    col_index: int = 0  # which diagram column this table sits in
    x: int = 0
    y: int = 0

    @property
    def group(self) -> str:
        return TABLE_GROUPS.get(self.name, "ops")

    @property
    def note(self) -> str | None:
        return TABLE_NOTES.get(self.name)

    @property
    def height(self) -> int:
        h = HEADER_H + ROW_H * len(self.columns) + 8
        if self.note:
            h += NOTE_LINE_H * (self.note.count("\n") + 1) + 6
        return h


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------


def read_schema() -> dict[str, Table]:
    """Apply every migration to a throwaway database and read the result.

    Returns tables keyed by name, with `sqlite_%` internal tables dropped --
    they are SQLite's own bookkeeping and mean nothing to a reader of this
    diagram.
    """
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.init_db(Path(tmp) / "erd-introspection.db")
        try:
            return _read_schema_from(conn)
        finally:
            conn.close()


def _read_schema_from(conn: sqlite3.Connection) -> dict[str, Table]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]

    tables: dict[str, Table] = {}
    for name in names:
        # PRAGMA does not accept a bound parameter for the table name, so
        # this is an f-string by necessity. `name` came from sqlite_master
        # in this same database, not from user input, so there is nothing
        # here to inject.
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()
        fks = [(row[3], row[2], row[4]) for row in conn.execute(f"PRAGMA foreign_key_list({name})")]
        fk_columns = {local for local, _, _ in fks}

        columns = [
            Column(
                name=row[1],
                type_=row[2],
                not_null=bool(row[3]),
                is_pk=bool(row[5]),
                is_fk=row[1] in fk_columns,
            )
            for row in info
        ]
        tables[name] = Table(name=name, columns=columns, foreign_keys=fks)

    return tables


def _assign_columns(tables: dict[str, Table]) -> None:
    """Place each table one column right of everything it references.

    Iterative relaxation rather than a topological sort, because it is
    shorter and this schema is small. The iteration cap is what keeps a
    future circular foreign key from spinning forever -- if the cap is ever
    hit the layout degrades to "slightly wrong columns", which is a far
    better failure than a hang in a docs build.
    """
    for _ in range(len(tables) + 1):
        changed = False
        for table in tables.values():
            for _local, target, _remote in table.foreign_keys:
                if target == table.name:
                    continue  # self-reference: no depth implication
                wanted = tables[target].col_index + 1
                if wanted > table.col_index:
                    table.col_index = wanted
                    changed = True
        if not changed:
            return


def _assign_positions(tables: dict[str, Table]) -> tuple[int, int]:
    """Stack each diagram column vertically. Returns the canvas size."""
    by_column: dict[int, list[Table]] = {}
    for table in sorted(tables.values(), key=lambda t: (t.col_index, t.name)):
        by_column.setdefault(table.col_index, []).append(table)

    canvas_h = 0
    for col_index, column_tables in sorted(by_column.items()):
        x = MARGIN + col_index * (BOX_WIDTH + COL_GAP)
        y = MARGIN + TITLE_H
        for table in column_tables:
            table.x, table.y = x, y
            y += table.height + ROW_GAP
        canvas_h = max(canvas_h, y)

    canvas_w = MARGIN * 2 + (max(by_column) + 1) * (BOX_WIDTH + COL_GAP) - COL_GAP
    return canvas_w, canvas_h + LEGEND_H


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _load_fonts(scale: int) -> dict[str, ImageFont.ImageFont | ImageFont.FreeTypeFont]:
    """Load DejaVu at the sizes the diagram uses, falling back to Pillow's
    bitmap default if the system has no TrueType fonts.

    The fallback produces an ugly but still readable diagram. Raising here
    instead would mean a machine without DejaVu cannot regenerate the docs
    at all, which is a worse trade for a build-time tool.

    The return type is a union because those two paths give back different
    classes -- FreeTypeFont is scalable, ImageFont is a fixed-size bitmap.
    Every consumer here only ever passes the value straight to Pillow's
    `font=` argument, which accepts both.
    """
    candidates = {
        "sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "mono": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    }
    sizes = {
        "title": ("bold", 22),
        "subtitle": ("sans", 11),
        "table": ("bold", 12),
        "column": ("mono", 9),
        "note": ("sans", 8),
        "legend": ("sans", 10),
        "edge": ("sans", 8),
    }
    fonts: dict[str, ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}
    for role, (family, size) in sizes.items():
        try:
            fonts[role] = ImageFont.truetype(candidates[family], size * scale)
        except OSError:
            fonts[role] = ImageFont.load_default()
    return fonts


def _draw_table(
    draw: ImageDraw.ImageDraw,
    table: Table,
    fonts: dict[str, ImageFont.ImageFont | ImageFont.FreeTypeFont],
    s: int,
) -> None:
    """Draw one table box: coloured header, column rows, optional note."""
    x, y, w, h = table.x * s, table.y * s, BOX_WIDTH * s, table.height * s
    header_color = GROUP_COLORS[table.group]

    draw.rectangle([x, y, x + w, y + h], fill=PAPER, outline=STEEL, width=max(1, s))
    draw.rectangle([x, y, x + w, y + HEADER_H * s], fill=header_color)

    # Safety yellow is far too light for white text; every other group
    # colour is dark enough to need it.
    header_text = INK if table.group == "floor" else "#ffffff"
    label = table.name
    if table.name in APPEND_ONLY_TABLES:
        label += "   ⬤ append-only"
    draw.text((x + PADDING_X * s, y + 8 * s), label, font=fonts["table"], fill=header_text)

    row_y = y + (HEADER_H + 5) * s
    for column in table.columns:
        # Marker column: PK beats FK when a column is both, because "this
        # is the identity of the row" is the more important fact.
        if column.is_pk:
            marker, marker_color = "PK", DOCK_ORANGE
        elif column.is_fk:
            marker, marker_color = "FK", STEEL
        else:
            marker, marker_color = "  ", MUTED

        draw.text((x + PADDING_X * s, row_y), marker, font=fonts["column"], fill=marker_color)
        name = column.name if column.not_null or column.is_pk else f"{column.name} ○"
        draw.text((x + (PADDING_X + 22) * s, row_y), name, font=fonts["column"], fill=INK)
        draw.text(
            (x + (BOX_WIDTH - 58) * s, row_y),
            column.type_.lower(),
            font=fonts["column"],
            fill=MUTED,
        )
        row_y += ROW_H * s

    if table.note:
        draw.line(
            [x + PADDING_X * s, row_y + 2 * s, x + w - PADDING_X * s, row_y + 2 * s],
            fill="#d8d5cd",
            width=max(1, s),
        )
        draw.multiline_text(
            (x + PADDING_X * s, row_y + 6 * s),
            table.note,
            font=fonts["note"],
            fill=STEEL,
            spacing=4 * s,
        )


def _draw_edges(draw: ImageDraw.ImageDraw, tables: dict[str, Table], s: int) -> None:
    """Draw foreign keys as orthogonal right-to-left elbows.

    Every edge leaves the right side of the *referenced* table and enters
    the left side of the *referencing* one, so arrows point the way you
    read the diagram: `accounts` on the left, the rows that depend on it to
    the right. A crow's-foot notation would be more formally correct, but
    every relationship in this schema is plain many-to-one and drawing the
    same glyph nineteen times carries no information.
    """
    for table in sorted(tables.values(), key=lambda t: t.name):
        for local, target, _remote in table.foreign_keys:
            if target not in tables or target == table.name:
                continue
            src, dst = tables[target], table

            x0 = (src.x + BOX_WIDTH) * s
            y0 = (src.y + HEADER_H // 2) * s
            x1 = dst.x * s
            y1 = (dst.y + HEADER_H // 2) * s

            # Backward edges (the referenced table sits to the right of the
            # referencing one) would otherwise be drawn as a line straight
            # through both boxes. Route them around the outside instead.
            if x1 < x0:
                mid_y = min(y0, y1) - 14 * s
                draw.line(
                    [
                        (x0, y0),
                        (x0 + 18 * s, y0),
                        (x0 + 18 * s, mid_y),
                        (x1 - 18 * s, mid_y),
                        (x1 - 18 * s, y1),
                        (x1, y1),
                    ],
                    fill="#a9a49a",
                    width=max(1, s),
                )
            else:
                mid_x = (x0 + x1) // 2
                draw.line(
                    [(x0, y0), (mid_x, y0), (mid_x, y1), (x1, y1)],
                    fill="#a9a49a",
                    width=max(1, s),
                )

            # Arrowhead at the referencing table's edge.
            a = 4 * s
            draw.polygon(
                [(x1, y1), (x1 - a * 2, y1 - a), (x1 - a * 2, y1 + a)],
                fill=STEEL,
            )
            _ = local  # column name is on the box already; no need to label


def _draw_chrome(
    draw: ImageDraw.ImageDraw,
    fonts: dict[str, ImageFont.ImageFont | ImageFont.FreeTypeFont],
    size: tuple[int, int],
    table_count: int,
    s: int,
) -> None:
    """Title block and legend."""
    draw.text((MARGIN * s, MARGIN * s), "Autorack Verify", font=fonts["title"], fill=INK)
    draw.text(
        (MARGIN * s, (MARGIN + 32) * s),
        f"Entity-relationship diagram — {table_count} tables, generated from the "
        "applied schema by tools/generate_erd.py",
        font=fonts["subtitle"],
        fill=STEEL,
    )
    draw.text(
        (MARGIN * s, (MARGIN + 50) * s),
        "Arrows point from the referenced table to the table holding the foreign key.  "
        "○ marks a nullable column.",
        font=fonts["subtitle"],
        fill=STEEL,
    )

    legend_y = size[1] - (LEGEND_H - 18) * s
    draw.line(
        [MARGIN * s, legend_y - 14 * s, size[0] - MARGIN * s, legend_y - 14 * s],
        fill="#c9c5bc",
        width=max(1, s),
    )

    x = MARGIN * s
    labels = {
        "tenancy": "tenancy — customers and logins",
        "catalog": "catalog — what should ship",
        "floor": "floor — shifts and scanning",
        "money": "money — charges and evidence",
        "ops": "ops — plumbing",
    }
    for group, label in labels.items():
        draw.rectangle([x, legend_y, x + 13 * s, legend_y + 13 * s], fill=GROUP_COLORS[group])
        draw.text((x + 19 * s, legend_y + 1 * s), label, font=fonts["legend"], fill=INK)
        x += 224 * s

    draw.text(
        (MARGIN * s, legend_y + 26 * s),
        "⬤ append-only: BEFORE UPDATE/DELETE triggers reject any mutation. "
        "Corrections are new rows (exceptions, or a 'reversal' billing_event), never edits — "
        "this is what makes every charge auditable back to the bytes the scanner produced.",
        font=fonts["legend"],
        fill=STEEL,
    )
    draw.text(
        (MARGIN * s, legend_y + 44 * s),
        "audit_log, rate_limit_events and schema_migrations carry no foreign keys by design: "
        "each must stay readable after the rows it refers to are gone.",
        font=fonts["legend"],
        fill=STEEL,
    )


def render(tables: dict[str, Table], out_path: Path) -> tuple[int, int]:
    """Lay out and draw the whole diagram, returning the final pixel size.

    Drawn at SCALE times the nominal size and downsampled with LANCZOS at
    the end. Pillow has no antialiasing for lines or text, so rendering at
    1x gives visibly jagged diagonals and gritty small type; supersampling
    is the cheapest fix and costs a fraction of a second at this canvas
    size.
    """
    _assign_columns(tables)
    width, height = _assign_positions(tables)

    image = Image.new("RGB", (width * SCALE, height * SCALE), CONCRETE)
    draw = ImageDraw.Draw(image)
    fonts = _load_fonts(SCALE)

    # Edges first so they pass behind the boxes rather than across them.
    _draw_edges(draw, tables, SCALE)
    for table in tables.values():
        _draw_table(draw, table, fonts, SCALE)
    _draw_chrome(draw, fonts, (width * SCALE, height * SCALE), len(tables), SCALE)

    image = image.resize((width, height), Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, "PNG", optimize=True)
    return width, height


def schema_json(tables: dict[str, Table]) -> str:
    """Serialise the introspected schema deterministically.

    Sorted keys and sorted table order so the output depends only on the
    schema, never on dict insertion order or filesystem traversal order --
    otherwise the CI comparison this feeds would produce spurious diffs.
    """
    payload = {
        name: {
            "columns": [
                {
                    "name": c.name,
                    "type": c.type_,
                    "not_null": c.not_null,
                    "pk": c.is_pk,
                    "fk": c.is_fk,
                }
                for c in table.columns
            ],
            "foreign_keys": sorted(
                f"{local} -> {target}.{remote}" for local, target, remote in table.foreign_keys
            ),
        }
        for name, table in sorted(tables.items())
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "erd.png",
        help="output PNG path (default: docs/erd.png)",
    )
    parser.add_argument(
        "--dump-schema",
        type=Path,
        metavar="PATH",
        help=(
            "write the introspected schema as JSON instead of rendering. "
            "CI compares this against docs/schema.json to detect a migration "
            "that landed without the diagram being regenerated."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    tables = read_schema()

    # Why CI diffs this JSON rather than the PNG: PNG bytes are not
    # reproducible across machines. Rendering depends on which DejaVu
    # build is installed (and _load_fonts silently falls back to a bitmap
    # font when none is), plus the Pillow version's own encoder. A byte
    # comparison would fail on a CI runner for reasons that have nothing
    # to do with the schema. This dump contains exactly what the diagram
    # is derived from, and is stable anywhere Python and SQLite run.
    if args.dump_schema:
        args.dump_schema.parent.mkdir(parents=True, exist_ok=True)
        args.dump_schema.write_text(schema_json(tables) + "\n", encoding="utf-8")
        print(f"{args.dump_schema}  {len(tables)} tables")
        return 0

    width, height = render(tables, args.out)
    print(f"{args.out}  {width}x{height}  {len(tables)} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
