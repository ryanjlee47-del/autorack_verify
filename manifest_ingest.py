"""Manifest ingest: parsing uploaded files/paste into rows, column
auto-detection, a preview-and-confirm step, and committing a manifest
(building the line_keys match index and running collision analysis).

Accepted inputs: CSV, TSV, XLSX-exported-as-CSV (which is just CSV with
possibly different quoting/encoding quirks -- handled by csv.Sniffer),
and raw newline-delimited paste (one barcode per line, no columns).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import OrderedDict
from dataclasses import dataclass, field

import barcode
import db
from sqlstore import SQL

# Column name variants we recognize during auto-detection, lowercased.
COLUMN_ALIASES = {
    "sku": {
        "sku",
        "item",
        "item number",
        "item#",
        "product",
        "product code",
        "part number",
        "part#",
    },
    "description": {"description", "desc", "item description", "name", "product description"},
    "qty_expected": {"qty", "quantity", "qty expected", "expected qty", "units"},
    "raw_barcode": {
        "barcode",
        "upc",
        "ean",
        "gtin",
        "scan",
        "code",
        "sku barcode",
        "barcode value",
    },
}


@dataclass
class ParsedManifest:
    rows: list[dict]
    detected_columns: dict[str, str | None]  # our field name -> source header (or None)
    warnings: list[str] = field(default_factory=list)


def _sniff_dialect(text: str) -> type[csv.Dialect]:
    """Guess the delimiter, falling back to comma.

    Returns the dialect *class*, not an instance: csv.Sniffer.sniff()
    builds and returns a subclass of csv.Dialect rather than an object,
    and csv.reader accepts either. Both branches return a class so the
    two paths agree.
    """
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:

        class _Fallback(csv.excel):
            delimiter = ","

        return _Fallback


def _detect_columns(header: list[str]) -> dict[str, str | None]:
    lowered = {h: h.strip().lower() for h in header}
    detected: dict[str, str | None] = {
        "sku": None,
        "description": None,
        "qty_expected": None,
        "raw_barcode": None,
    }
    for field_name, aliases in COLUMN_ALIASES.items():
        for original, low in lowered.items():
            if low in aliases:
                detected[field_name] = original
                break
    return detected


def parse_header(text: str) -> list[str]:
    """Header row only, using the same dialect-sniffing as parse_delimited
    -- used by the owner app to render the column-mapping dropdowns with
    the correct delimiter (comma/tab/etc.) instead of guessing again."""
    text = text.lstrip("﻿")
    dialect = _sniff_dialect(text)
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        return next(reader)
    except StopIteration:
        return []


def _cell(row: list[str], idx: dict[str, int | None], name: str) -> str | None:
    """Read one named column out of a raw row, or None if it is absent.

    Module-level rather than a closure defined inside the per-row loop.
    The closure form captured `row` by reference and so was correct only
    because every call happened in the same iteration that defined it --
    a property of the call sites, not of the function. Passing `row` in
    makes that explicit and removes the trap for anyone who later stores
    the callable or defers a call.

    A short row is treated as a missing value rather than an error:
    spreadsheet exports routinely drop trailing empty cells, so a row
    ending early is normal input, not corruption.
    """
    i = idx.get(name)
    if i is None or i >= len(row):
        return None
    return row[i].strip()


def parse_delimited(text: str, filename: str | None = None) -> ParsedManifest:
    """Parse CSV/TSV (or XLSX-exported-as-CSV) text with column
    auto-detection. Returns a preview -- caller must call commit_manifest()
    after the owner confirms the column mapping."""
    text = text.lstrip("﻿")  # strip BOM some Excel exports add
    dialect = _sniff_dialect(text)
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return ParsedManifest(rows=[], detected_columns={}, warnings=["File is empty."])

    header = rows[0]
    detected = _detect_columns(header)
    warnings = []
    if detected["raw_barcode"] is None:
        warnings.append(
            "Could not auto-detect a barcode column. Pick one manually before committing."
        )

    idx = {name: (header.index(col) if col else None) for name, col in detected.items()}
    parsed_rows = []
    for line_no, row in enumerate(rows[1:], start=1):
        if not any(cell.strip() for cell in row):
            continue  # skip blank lines

        raw_barcode = _cell(row, idx, "raw_barcode")
        if not raw_barcode:
            continue
        qty_raw = _cell(row, idx, "qty_expected")
        try:
            qty = int(qty_raw) if qty_raw else 1
        except ValueError:
            qty = 1
        parsed_rows.append(
            {
                "line_no": line_no,
                "sku": _cell(row, idx, "sku"),
                "description": _cell(row, idx, "description"),
                "qty_expected": qty,
                "raw_barcode": raw_barcode,
            }
        )

    return ParsedManifest(rows=parsed_rows, detected_columns=detected, warnings=warnings)


def parse_paste(text: str) -> ParsedManifest:
    """Raw newline-delimited paste: one barcode per line, no columns."""
    lines = [ln.strip() for ln in text.splitlines()]
    rows = [
        {"line_no": i, "sku": None, "description": None, "qty_expected": 1, "raw_barcode": ln}
        for i, ln in enumerate(lines, start=1)
        if ln
    ]
    warnings = [] if rows else ["No barcodes found in pasted text."]
    return ParsedManifest(
        rows=rows,
        detected_columns={
            "sku": None,
            "description": None,
            "qty_expected": None,
            "raw_barcode": None,
        },
        warnings=warnings,
    )


def reparse_with_mapping(text: str, mapping: dict[str, str | None]) -> ParsedManifest:
    """Re-parse delimited text with an explicit, owner-confirmed column
    mapping (our field name -> source header), overriding auto-detection."""
    dialect = _sniff_dialect(text)
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), dialect)
    rows = list(reader)
    if not rows:
        return ParsedManifest(rows=[], detected_columns=mapping, warnings=["File is empty."])
    header = rows[0]
    idx = {
        name: (header.index(col) if col and col in header else None)
        for name, col in mapping.items()
    }
    parsed_rows = []
    for line_no, row in enumerate(rows[1:], start=1):
        if not any(cell.strip() for cell in row):
            continue

        raw_barcode = _cell(row, idx, "raw_barcode")
        if not raw_barcode:
            continue
        qty_raw = _cell(row, idx, "qty_expected")
        try:
            qty = int(qty_raw) if qty_raw else 1
        except ValueError:
            qty = 1
        parsed_rows.append(
            {
                "line_no": line_no,
                "sku": _cell(row, idx, "sku"),
                "description": _cell(row, idx, "description"),
                "qty_expected": qty,
                "raw_barcode": raw_barcode,
            }
        )
    return ParsedManifest(rows=parsed_rows, detected_columns=mapping, warnings=[])


# ---------------------------------------------------------------------------
# Commit: persist lines, build line_keys, run collision analysis.
# ---------------------------------------------------------------------------


def commit_manifest(
    conn,
    account_id: int,
    ref: str,
    source_filename: str | None,
    rows: list[dict],
    loose_match_enabled: bool,
    loose_suffix_len: int,
) -> tuple[int, barcode.CollisionReport]:
    """Insert manifest + lines, compute every tier's index keys, run
    collision analysis, and mark the specific colliding keys so the match
    index (built later, at shift-prepare time) disables loose matching for
    exactly those keys -- not the whole manifest.

    All of it lands in one transaction: a manifest marked 'committed' but
    missing some of its line_keys would silently fail to match items that
    are genuinely on it, which reads to a worker as a false REJECT.
    """
    with db.transaction(conn):
        manifest_id = db.create_manifest(conn, account_id, ref, source_filename)
        line_ids = db.insert_manifest_lines(conn, manifest_id, rows)

        lines_for_collision = [
            (line_id, row["raw_barcode"]) for line_id, row in zip(line_ids, rows, strict=False)
        ]
        report = barcode.analyze_collisions(lines_for_collision, suffix_len=loose_suffix_len)
        collision_keys: dict[barcode.Tier, set[str]] = {
            tier: set(by_key.keys()) for tier, by_key in report.collisions.items()
        }

        key_rows = []
        for line_id, row in zip(line_ids, rows, strict=False):
            norm = barcode.normalize(row["raw_barcode"], suffix_len=loose_suffix_len)
            for tier, key in norm.keys.items():
                if tier == barcode.Tier.SUFFIX and not loose_match_enabled:
                    continue
                is_collision = key in collision_keys.get(tier, ())
                key_rows.append((line_id, int(tier), key, int(is_collision)))
        db.insert_line_keys(conn, key_rows)

        db.set_manifest_status(conn, manifest_id, "committed", line_count=len(line_ids))
    return manifest_id, report


def build_match_index(conn, account, manifest_ids: list[int]) -> barcode.MatchIndex:
    """Build an in-memory MatchIndex from committed manifest lines for the
    given account/manifests, honoring the account's loose-match settings,
    per-key collision disabling, and learned aliases (tier 5).
    """
    idx = barcode.MatchIndex(
        loose_match_enabled=bool(account["loose_match_enabled"]),
        suffix_len=account["loose_suffix_len"],
    )
    key_rows = db.get_line_keys_for_manifests(conn, manifest_ids)
    for row in key_rows:
        tier = barcode.Tier(row["tier"])
        if tier == barcode.Tier.SUFFIX and not idx.loose_match_enabled:
            continue
        if row["collision"]:
            idx.disable_key(tier, row["key"])
            continue
        idx.add_key(row["manifest_line_id"], tier, row["key"])

    # Aliases are keyed by (account, normalized_key) -> sku. Resolve sku to
    # whichever manifest_line_id(s) in *this* bundle actually carry it --
    # aliases outlive any one manifest, but only apply where the sku
    # still appears.
    sku_to_line_ids = db.get_manifest_line_ids_by_sku(conn, manifest_ids)
    for alias in db.list_aliases(conn, account["id"]):
        for line_id in sku_to_line_ids.get(alias["sku"], ()):
            idx.add_alias(alias["normalized_key"], line_id)

    return idx


def _db_identity(conn) -> str:
    """A stable identifier for which database file a connection talks to.

    The cache below is process-level and outlives any single connection
    or request, so it needs to key on more than (shift_id, bundle_version)
    -- otherwise two different SQLite files with overlapping autoincrement
    ids (guaranteed in tests, which open a fresh tmp_path database per
    test; not impossible in production if the app is ever pointed at more
    than one db) could serve one shift's match index for another's. The
    real app only ever opens one db file per process, so this is a no-op
    there.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


# Matching a single barcode against a built MatchIndex is cheap (a handful
# of tiered hash lookups); building the index from line_keys is the
# expensive part, especially at 15k+ manifest lines. Verifying a
# client-reported REJECT before billing it (see app.py's w_sync) does
# this per scan, potentially many times per sync batch and many batches
# per shift, so the built index is cached and only rebuilt when the
# shift's bundle_version actually changes.
#
# The comment here used to claim memory was "bounded by one index per
# currently-active shift, not by history." Replacing a shift's entry on a
# version bump does bound the cache per shift -- but nothing ever removed a
# SHIFT, so every shift that had ever synced kept a full MatchIndex resident
# in every gunicorn worker until restart. At 15k lines per manifest that is
# not a small leak, and "currently-active" was doing work no code performed.
#
# An OrderedDict with a hard cap makes the claim true: least-recently-used
# eviction, so the working set is the shifts actually syncing right now.
# Evicting a live shift's index costs one rebuild, which is exactly the cost
# of a cache miss and is what the pre-cache code paid on every scan.
MATCH_INDEX_CACHE_MAX_ENTRIES = 32

_match_index_cache: OrderedDict[tuple[str, int], tuple[int, barcode.MatchIndex]] = OrderedDict()


def clear_match_index_cache() -> None:
    """Drop every cached index.

    Required after anything that replaces the database file underneath the
    process (see admin_api.run_restore): the cache is keyed on
    (database identity, shift id, bundle_version), and a restored file can
    reuse all three while holding entirely different manifest lines.
    """
    _match_index_cache.clear()


def cached_match_index(conn, account, shift, manifest_ids: list[int]) -> barcode.MatchIndex:
    """Like build_match_index, but reuses the last build for this shift
    unless its bundle_version has moved on."""
    key = (_db_identity(conn), shift["id"])
    cached = _match_index_cache.get(key)
    if cached is not None and cached[0] == shift["bundle_version"]:
        _match_index_cache.move_to_end(key)  # LRU: this shift is still active
        return cached[1]
    idx = build_match_index(conn, account, manifest_ids)
    _match_index_cache[key] = (shift["bundle_version"], idx)
    _match_index_cache.move_to_end(key)
    while len(_match_index_cache) > MATCH_INDEX_CACHE_MAX_ENTRIES:
        _match_index_cache.popitem(last=False)
    return idx


# ---------------------------------------------------------------------------
# Shift bundle: what actually ships to the phone. A QR code cannot carry
# 15,000 barcodes (~2-4KB capacity) -- it carries a join token. The bundle
# itself is fetched once over the door-area wifi and persisted client-side
# in IndexedDB, then rehydrated into an in-memory Map. From then on the
# phone needs no network for the rest of the shift.
# ---------------------------------------------------------------------------


def bundle_payload(conn, account, shift, manifest_ids: list[int]) -> dict:
    lines = []
    for manifest_id in manifest_ids:
        for row in db.get_manifest_lines(conn, manifest_id):
            lines.append(
                {
                    "id": row["id"],
                    "manifestId": row["manifest_id"],
                    "sku": row["sku"],
                    "description": row["description"],
                    "qtyExpected": row["qty_expected"],
                }
            )

    loose_enabled = bool(account["loose_match_enabled"])
    key_rows = db.get_line_keys_for_manifests(conn, manifest_ids)
    keys = []
    # Omitting a colliding key from the bundle is NOT equivalent to the
    # server's disable_key(), and the difference is a live divergence rather
    # than a stylistic one. Collision analysis runs per manifest at commit
    # time; matching runs per shift across every manifest in it. A key
    # flagged in manifest A but clean in manifest B is dead on the server
    # (disable_key suppresses the whole tier/key pair) and, if we merely
    # omitted A's row, still live on the phone via B's row -- so the phone
    # reports a confident `ok` the server would have called unresolved, and
    # the server never re-verifies `ok`, so nobody ever sees the
    # disagreement. Shipping the disabled set explicitly and having
    # matchAgainstIndex honor it makes the two sides use one mechanism.
    disabled_keys: dict[int, list[str]] = {}
    for row in key_rows:
        tier = row["tier"]
        if tier == int(barcode.Tier.SUFFIX) and not loose_enabled:
            continue
        if row["collision"]:
            bucket = disabled_keys.setdefault(tier, [])
            if row["key"] not in bucket:
                bucket.append(row["key"])
            continue
        keys.append({"manifestLineId": row["manifest_line_id"], "tier": tier, "key": row["key"]})

    sku_to_line_ids = db.get_manifest_line_ids_by_sku(conn, manifest_ids)
    for alias in db.list_aliases(conn, account["id"]):
        for line_id in sku_to_line_ids.get(alias["sku"], ()):
            keys.append(
                {
                    "manifestLineId": line_id,
                    "tier": int(barcode.Tier.ALIAS),
                    "key": alias["normalized_key"],
                }
            )

    payload = {
        "shift": {
            "id": shift["id"],
            "label": shift["label"],
            "date": shift["date"],
            "bundleVersion": shift["bundle_version"],
        },
        "lines": lines,
        "keys": keys,
        # tier -> [key, ...]; mirrors MatchIndex.disabled_keys. See above.
        "disabledKeys": {str(tier): sorted(ks) for tier, ks in disabled_keys.items()},
        "settings": {
            "looseMatchEnabled": loose_enabled,
            "looseSuffixLen": account["loose_suffix_len"],
            "workerSelfResolve": bool(account["worker_self_resolve"]),
        },
    }
    return payload


def regenerate_keys(
    conn, manifest_id: int, loose_match_enabled: bool, loose_suffix_len: int
) -> barcode.CollisionReport:
    """Recompute line_keys for a manifest from scratch -- used by the
    operator GUI's Manifests & Shifts tab after an account's matching
    profile (loose-match toggle/suffix length) changes, since keys are
    otherwise only computed once, at commit time."""
    lines = db.get_manifest_lines(conn, manifest_id)
    db.execute(
        conn,
        SQL["manifests.delete_line_keys_for_manifest"],
        (manifest_id,),
    )

    lines_for_collision = [(row["id"], row["raw_barcode"]) for row in lines]
    report = barcode.analyze_collisions(lines_for_collision, suffix_len=loose_suffix_len)
    collision_keys = {tier: set(by_key.keys()) for tier, by_key in report.collisions.items()}

    key_rows = []
    for row in lines:
        norm = barcode.normalize(row["raw_barcode"], suffix_len=loose_suffix_len)
        for tier, key in norm.keys.items():
            if tier == barcode.Tier.SUFFIX and not loose_match_enabled:
                continue
            is_collision = key in collision_keys.get(tier, ())
            key_rows.append((row["id"], int(tier), key, int(is_collision)))
    db.insert_line_keys(conn, key_rows)
    db.bump_manifest_bundle_version(conn, manifest_id)
    # Any shift that has already handed this manifest's bundle out to
    # phones needs its bundle_version bumped too -- that's what makes
    # connected phones pull a delta and disconnected phones show "Bundle
    # stale" on next reconnect (see app.py's /w/heartbeat and /w/sync).
    for shift in db.get_shifts_for_manifest(conn, manifest_id):
        db.bump_shift_bundle_version(conn, shift["id"])
    return report


def content_hash(payload: dict) -> str:
    """Hash of a whole bundle payload, envelope included.

    Shipped to the phone as `contentHash` so it can detect a truncated or
    corrupted download of the exact response it just received.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# The parts of a bundle that describe what the phone will MATCH against, as
# opposed to which shift it belongs to.
_CONTENT_KEYS = ("lines", "keys", "disabledKeys", "settings")


def bundle_content_hash(payload: dict) -> str:
    """Hash of a bundle's matching content, excluding shift identity.

    shifts.bundle_hash is recorded at prepare time, before the shift row
    exists -- so shift_prepare hashes a payload whose `shift.id` is None,
    while /w/bundle later serves one carrying the real id. Hashing the whole
    envelope therefore produced two values that could never be equal, which
    is why the recorded hash was not merely unread but uncomparable: an
    integrity check that could not have passed even if something had checked
    it.

    Hashing only the content makes the two ends comparable, and is also the
    more useful question: "does this shift still serve the bundle it was
    prepared with?" Shift label and date can change without the matching
    content changing, and vice versa -- the second is the one that matters.
    """

    def canonical(value):
        # Lists here are sets in disguise: `lines` becomes linesById on the
        # phone and `keys` becomes a tiered lookup table, so neither one's
        # order carries meaning. It does vary, though -- shift_prepare passes
        # the manifest ids in the order the owner's form submitted them,
        # while /w/bundle reads them back from shift_manifests -- so an
        # order-sensitive hash reported a mismatch on every completely normal
        # shift, which is worse than not checking at all: an integrity
        # warning that always fires is one nobody reads.
        if isinstance(value, list):
            return sorted(
                (canonical(item) for item in value),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        return value

    content = {key: canonical(payload.get(key)) for key in _CONTENT_KEYS}
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
