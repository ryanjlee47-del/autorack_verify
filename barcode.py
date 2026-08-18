"""Barcode normalization and tiered matching engine.

This module is the single source of truth for how a scanned barcode payload
turns into a decision about which manifest line it refers to. Its JavaScript
twin, static/js/barcode.js, must produce byte-identical normalized strings
and index keys for the same input -- see the "FNC1 parity trap" note below
and tests/test_hash_parity.py, which enforces it.

Pipeline (normalize()):
  1. Keep the raw payload untouched, always.
  2. Parse GS1 element-string structure (AI parsing) on a lightly edge-trimmed
     copy of the raw payload, BEFORE the aggressive control-character strip.
     GS1 needs its embedded \\x1d (Group Separator) bytes intact to know
     where variable-length fields end -- if we stripped them first there
     would be nothing left to split on.
  3. Compute the fully stripped + uppercased "normalized" string, used for
     the tier-1 key and as the input to plain (non-GS1) numeric/alphanumeric
     handling.
  4. If a GTIN was found (from GS1 AI 01/02, or because the normalized
     string is itself an 8/12/13/14-digit numeric code), canonicalize it
     through UPC-E -> UPC-A -> EAN-13 -> GTIN-14.
  5. Emit one index key per tier that applies to this payload.

FNC1 parity trap
-----------------
Python's str.strip() (and str.isspace()) treat \\x1c-\\x1f (FS/GS/RS/US) as
whitespace; JavaScript's String.prototype.trim() does not. If server and
phone each normalize with their language's built-in strip/trim, the same
physical label produces two different normalized strings -- and therefore
two different hashes/keys -- depending on which side computed it. That is
a phantom reject, and phantom rejects are billable events you did not earn.

The fix is to never call str.strip() or .trim() on scanner payloads here.
Instead we define an explicit character set (CONTROL_CHARS below) and strip
exactly those code points, from anywhere in the string, using the same list
on both sides. static/js/barcode.js defines the identical set by code point
and tests/test_hash_parity.py asserts the two implementations agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum

# ---------------------------------------------------------------------------
# Explicit control/separator character set. See module docstring.
# NUL, FS/GS/RS/US (0x1c-0x1f), and standard ASCII whitespace.
# ---------------------------------------------------------------------------
CONTROL_CHARS = "\x00" + "\x1c\x1d\x1e\x1f" + " \t\n\r\x0b\x0c"
_CONTROL_TABLE = {ord(c): None for c in CONTROL_CHARS}

# Characters trimmed from the ends only, before GS1 AI parsing, so that
# embedded \x1d (GS) separators used by variable-length AI fields survive
# intact for the parser. Only NUL and plain whitespace are edge-trimmed here;
# \x1c-\x1f are deliberately left alone.
_EDGE_TRIM_CHARS = "\x00 \t\n\r\x0b\x0c"

GS_SEP = "\x1d"


def strip_control_chars(s: str) -> str:
    """Remove NUL, FS/GS/RS/US, and whitespace from anywhere in the string.

    Deliberately not str.strip() -- see module docstring.
    """
    return s.translate(_CONTROL_TABLE)


def _edge_trim_preserve_gs(s: str) -> str:
    return s.strip(_EDGE_TRIM_CHARS)


def to_upper_ascii(s: str) -> str:
    """Uppercase, then drop any non-ASCII character."""
    upper = s.upper()
    return "".join(ch for ch in upper if ord(ch) < 128)


# ---------------------------------------------------------------------------
# GS1 application identifiers we understand.
# ---------------------------------------------------------------------------
# AI -> fixed data length (digits/chars after the AI code itself).
FIXED_AI_LENGTHS: dict[str, int] = {
    "00": 18,
    "01": 14,
    "02": 14,
    "11": 6,
    "12": 6,
    "13": 6,
    "15": 6,
    "17": 6,
    "20": 2,
    "410": 13,
    "411": 13,
    "412": 13,
    "413": 13,
    "414": 13,
    "415": 13,
    "422": 3,
}
# AIs whose value runs until the next GS separator, '(' delimiter, or EOS.
VARIABLE_AIS: set[str] = {"10", "21", "30"}

_THREE_DIGIT_AI_PREFIXES = ("41", "42")

AI_FIELD_NAMES = {
    "00": "sscc",
    "01": "gtin",
    "02": "gtin",
    "10": "lot",
    "21": "serial",
    "30": "qty",
}


@dataclass
class GS1Fields:
    gtin: str | None = None
    lot: str | None = None
    serial: str | None = None
    qty: str | None = None
    sscc: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _looks_like_gs1(s: str) -> bool:
    if not s:
        return False
    if s[0] == "(":
        return True
    if s[0] == GS_SEP:
        return True
    # Bare AI prefix: starts with a known 2-digit AI code from our table.
    return s[:2] in FIXED_AI_LENGTHS or s[:2] in VARIABLE_AIS


def parse_gs1(s: str) -> GS1Fields | None:
    """Parse a GS1 element string into fields. Returns None if `s` does not
    look like a GS1 payload at all.

    Operates on an edge-trimmed (not control-stripped) copy of the raw
    payload so embedded GS separators are preserved.
    """
    working = _edge_trim_preserve_gs(s)
    if not working or not _looks_like_gs1(working):
        return None

    bracketed = working[0] == "("
    if working and working[0] == GS_SEP:
        working = working[1:]

    fields = GS1Fields()
    pos = 0
    n = len(working)
    found_any = False

    while pos < n:
        ch = working[pos]
        if ch == GS_SEP:
            pos += 1
            continue
        if ch == "(":
            end = working.find(")", pos)
            if end == -1:
                break
            ai = working[pos + 1 : end]
            pos = end + 1
        else:
            if working[pos : pos + 2] in _THREE_DIGIT_AI_PREFIXES:
                ai = working[pos : pos + 3]
                pos += 3
            else:
                ai = working[pos : pos + 2]
                pos += 2
            if not ai.isdigit():
                break

        if ai in FIXED_AI_LENGTHS:
            length = FIXED_AI_LENGTHS[ai]
            value = working[pos : pos + length]
            if len(value) < length:
                break
            pos += length
        elif ai in VARIABLE_AIS:
            if bracketed:
                next_paren = working.find("(", pos)
                end = next_paren if next_paren != -1 else n
            else:
                next_gs = working.find(GS_SEP, pos)
                end = next_gs if next_gs != -1 else n
            value = working[pos:end]
            pos = end
        else:
            # Unknown AI: can't know its length, stop parsing rather than
            # guess and silently corrupt downstream fields.
            break

        found_any = True
        field_name = AI_FIELD_NAMES.get(ai)
        if field_name == "gtin":
            fields.gtin = value
        elif field_name == "lot":
            fields.lot = value
        elif field_name == "serial":
            fields.serial = value
        elif field_name == "qty":
            fields.qty = value
        elif field_name == "sscc":
            fields.sscc = value
        else:
            fields.extra[ai] = value

    return fields if found_any else None


# ---------------------------------------------------------------------------
# GTIN canonicalization: UPC-E -> UPC-A -> EAN-13 -> GTIN-14
# ---------------------------------------------------------------------------


def upce_body_to_upca_body(upce_body: str) -> str:
    """Expand a 7-digit UPC-E body (N S1 S2 S3 S4 S5 S6, check digit
    excluded) to an 11-digit UPC-A body (also check-digit-free). The
    branch table only ever rearranges these 7 digits -- the check digit
    is never an input to it -- so this is usable even when a scanner or
    manifest line has the check digit stripped off entirely.
    """
    if len(upce_body) != 7 or not upce_body.isdigit():
        raise ValueError("UPC-E body must be exactly 7 digits (check digit excluded)")
    n, s1, s2, s3, s4, s5, s6 = tuple(upce_body)
    if s6 in "012":
        return n + s1 + s2 + s6 + "0000" + s3 + s4 + s5
    elif s6 == "3":
        return n + s1 + s2 + s3 + "00000" + s4 + s5
    elif s6 == "4":
        return n + s1 + s2 + s3 + s4 + "00000" + s5
    else:  # 5-9
        return n + s1 + s2 + s3 + s4 + s5 + "0000" + s6


def upce_to_upca(upce: str) -> str:
    """Expand an 8-digit UPC-E code (N S1 S2 S3 S4 S5 S6 C) to 12-digit UPC-A."""
    if len(upce) != 8 or not upce.isdigit():
        raise ValueError("UPC-E input must be exactly 8 digits")
    return upce_body_to_upca_body(upce[:7]) + upce[7]


def compute_check_digit(body: str) -> str:
    """GS1 mod-10 check digit: weight 3 for the rightmost digit, alternating."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        weight = 3 if i % 2 == 0 else 1
        total += int(ch) * weight
    return str((10 - (total % 10)) % 10)


@dataclass
class GtinInfo:
    gtin14: str
    ean13: str | None
    upca: str | None
    upce: str | None
    check_valid: bool
    body_no_check: str


def gtin_canonicalize(digits: str) -> GtinInfo | None:
    """Canonicalize an all-digit code of length 8/12/13/14 to GTIN-14.

    Returns None for any other length -- canonicalization only fires for
    these lengths, never a truncation or rejection of other lengths
    elsewhere in the pipeline.
    """
    if not digits.isdigit():
        return None
    n = len(digits)
    upce = upca = ean13 = gtin14 = None
    if n == 8:
        upce = digits
        upca = upce_to_upca(digits)
        ean13 = "0" + upca
        gtin14 = "00" + upca
    elif n == 12:
        upca = digits
        ean13 = "0" + upca
        gtin14 = "00" + upca
    elif n == 13:
        ean13 = digits
        gtin14 = "0" + ean13
        upca = ean13[1:] if ean13[0] == "0" else None
    elif n == 14:
        gtin14 = digits
        ean13 = gtin14[1:] if gtin14[0] == "0" else None
        upca = ean13[1:] if ean13 and ean13[0] == "0" else None
    else:
        return None

    body = gtin14[:-1]
    check = gtin14[-1]
    computed = compute_check_digit(body)
    return GtinInfo(
        gtin14=gtin14,
        ean13=ean13,
        upca=upca,
        upce=upce,
        check_valid=(computed == check),
        body_no_check=body,
    )


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


class Tier(IntEnum):
    RAW = 0
    NORMALIZED = 1
    GTIN14 = 2
    DIGITS_STRIPPED = 3
    BODY_NO_CHECK = 4
    ALIAS = 5
    SUFFIX = 6


TIER_CONFIDENCE = {
    Tier.RAW: "certain",
    Tier.NORMALIZED: "certain",
    Tier.GTIN14: "certain",
    Tier.DIGITS_STRIPPED: "high",
    Tier.BODY_NO_CHECK: "high",
    Tier.ALIAS: "certain",
    Tier.SUFFIX: "low",
}

# Tiers that require owner confirmation before they can count toward
# billing, even when they resolve uniquely.
CONFIRMATION_REQUIRED_TIERS = {Tier.SUFFIX}

DEFAULT_SUFFIX_LEN = 8
MIN_SUFFIX_LEN = 6


@dataclass
class Normalized:
    raw: str
    normalized: str
    gs1: GS1Fields | None
    gtin_info: GtinInfo | None
    keys: dict[Tier, str]

    def key_for(self, tier: Tier) -> str | None:
        return self.keys.get(tier)


def normalize(raw: str, suffix_len: int = DEFAULT_SUFFIX_LEN) -> Normalized:
    """Run the full normalization pipeline on a raw scanner payload.

    `suffix_len` controls the tier-6 (loose suffix) key length; callers that
    have not enabled tier 6 for the account can simply ignore that key.
    """
    if suffix_len < MIN_SUFFIX_LEN:
        suffix_len = MIN_SUFFIX_LEN

    gs1 = parse_gs1(raw)
    normalized = to_upper_ascii(strip_control_chars(raw))

    gtin_source = None
    if gs1 and gs1.gtin:
        gtin_source = strip_control_chars(gs1.gtin)
    elif normalized.isdigit() and len(normalized) in (8, 12, 13, 14):
        gtin_source = normalized

    gtin_info = gtin_canonicalize(gtin_source) if gtin_source else None

    keys: dict[Tier, str] = {}
    keys[Tier.RAW] = raw
    keys[Tier.NORMALIZED] = normalized

    if gtin_info:
        keys[Tier.GTIN14] = gtin_info.gtin14
        keys[Tier.BODY_NO_CHECK] = gtin_info.body_no_check
    elif normalized.isdigit() and len(normalized) in (7, 11):
        # Unambiguous "check digit omitted" body: 7 digits can only be a
        # UPC-E body missing its check digit, 11 only a UPC-A body missing
        # its check digit (12/13-digit inputs are ambiguous with a
        # *complete* UPC-A/EAN-13 and are deliberately not guessed at).
        # This lets tier 4 match regardless of which side -- the scanner
        # or the manifest export -- is the one missing the check digit.
        # The key must land on the SAME canonical (GTIN-14-level, "00"
        # prefixed) body that a full-length code's body_no_check would
        # produce, or the two sides would compare unequal lengths.
        upca_body = upce_body_to_upca_body(normalized) if len(normalized) == 7 else normalized
        keys[Tier.BODY_NO_CHECK] = "00" + upca_body

    digits_only = "".join(ch for ch in normalized if ch.isdigit())
    if normalized.isdigit() and digits_only:
        stripped_zeros = digits_only.lstrip("0") or "0"
        keys[Tier.DIGITS_STRIPPED] = stripped_zeros

    if digits_only and len(digits_only) >= suffix_len:
        keys[Tier.SUFFIX] = digits_only[-suffix_len:]

    return Normalized(raw=raw, normalized=normalized, gs1=gs1, gtin_info=gtin_info, keys=keys)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# Strict lookup order. Index 0 is tried first.
#
# ALIAS sits with the other "certain"-confidence tiers (see
# TIER_CONFIDENCE below), not after the "high"-confidence loose-numeric
# tiers -- an owner-taught alias is a confirmed correction and must not
# be silently outranked by an automatic guess. Getting this wrong is a
# real, demonstrated bug, not a theoretical one: teach an alias for scan
# X -> line A, then add an unrelated new manifest line whose
# DIGITS_STRIPPED key happens to collide with X's -- with ALIAS ordered
# after DIGITS_STRIPPED, the next scan of X silently resolves to the new
# line instead of the taught one. See
# tests/test_barcode.py::test_alias_is_not_overridden_by_a_later_colliding_loose_tier.
#
# static/js/barcode.js's TIER_ORDER must be kept byte-for-byte identical
# to this one -- matching runs on both sides (the phone offline, the
# server re-verifying REJECT claims in app.py's _bill_confirmed_reject),
# and a divergence here is the same class of phantom-mismatch risk as
# the FNC1 parity trap in normalize(). See
# tests/test_hash_parity.py::test_tier_order_matches_javascript.
TIER_ORDER: tuple[Tier, ...] = (
    Tier.RAW,
    Tier.NORMALIZED,
    Tier.GTIN14,
    Tier.ALIAS,
    Tier.DIGITS_STRIPPED,
    Tier.BODY_NO_CHECK,
    Tier.SUFFIX,
)


class Resolution(IntEnum):
    RESOLVED = 0
    UNRESOLVED = 1


@dataclass
class MatchResult:
    resolution: Resolution
    tier: Tier | None
    manifest_line_id: object | None
    candidates_by_tier: dict[Tier, list[object]]
    needs_confirmation: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.resolution == Resolution.RESOLVED


class MatchIndex:
    """In-memory tiered lookup index built from manifest_lines/line_keys/aliases.

    Structure: tier -> key -> set(manifest_line_id).
    `disabled_keys[tier]` holds keys that ingest-time collision analysis
    flagged as too ambiguous for loose matching -- those specific keys are
    treated as having no index entry at that tier (falls through to the
    next tier), even if only one line happens to hold them right now.
    """

    def __init__(self, loose_match_enabled: bool = False, suffix_len: int = DEFAULT_SUFFIX_LEN):
        self._index: dict[Tier, dict[str, set[object]]] = {t: {} for t in TIER_ORDER}
        self.disabled_keys: dict[Tier, set[str]] = {t: set() for t in TIER_ORDER}
        self.loose_match_enabled = loose_match_enabled
        self.suffix_len = max(suffix_len, MIN_SUFFIX_LEN)

    def add_line(self, manifest_line_id: object, raw_barcode: str) -> Normalized:
        norm = normalize(raw_barcode, suffix_len=self.suffix_len)
        for tier, key in norm.keys.items():
            if tier == Tier.SUFFIX and not self.loose_match_enabled:
                continue
            self._index[tier].setdefault(key, set()).add(manifest_line_id)
        return norm

    def add_alias(self, normalized_key: str, manifest_line_id: object) -> None:
        self._index[Tier.ALIAS].setdefault(normalized_key, set()).add(manifest_line_id)

    def add_key(self, manifest_line_id: object, tier: Tier, key: str) -> None:
        """Insert a precomputed (tier, key) pair directly -- used when
        rehydrating an index from persisted line_keys rows rather than
        re-normalizing raw barcodes."""
        if tier == Tier.SUFFIX and not self.loose_match_enabled:
            return
        self._index[tier].setdefault(key, set()).add(manifest_line_id)

    def disable_key(self, tier: Tier, key: str) -> None:
        self.disabled_keys[tier].add(key)

    def candidates(self, tier: Tier, key: str | None) -> list[object]:
        if key is None:
            return []
        if key in self.disabled_keys.get(tier, ()):
            return []
        return sorted(self._index[tier].get(key, ()), key=repr)

    def match(self, raw_payload: str) -> MatchResult:
        norm = normalize(raw_payload, suffix_len=self.suffix_len)
        candidates_by_tier: dict[Tier, list[object]] = {}

        for tier in TIER_ORDER:
            if tier == Tier.SUFFIX and not self.loose_match_enabled:
                continue
            # Aliases are keyed by the plain normalized string (see the
            # `aliases` table: normalized_key), not a key normalize()
            # itself emits -- it has no notion of account-learned aliases.
            key = norm.normalized if tier == Tier.ALIAS else norm.key_for(tier)
            hits = self.candidates(tier, key)
            candidates_by_tier[tier] = hits
            if len(hits) == 1:
                return MatchResult(
                    resolution=Resolution.RESOLVED,
                    tier=tier,
                    manifest_line_id=hits[0],
                    candidates_by_tier=candidates_by_tier,
                    needs_confirmation=tier in CONFIRMATION_REQUIRED_TIERS,
                )
            # 0 or >1 hits: ambiguity guard -- fall through to next tier.

        return MatchResult(
            resolution=Resolution.UNRESOLVED,
            tier=None,
            manifest_line_id=None,
            candidates_by_tier=candidates_by_tier,
        )


# ---------------------------------------------------------------------------
# Collision analysis (manifest ingest time)
# ---------------------------------------------------------------------------

LOOSE_TIERS = (Tier.DIGITS_STRIPPED, Tier.BODY_NO_CHECK, Tier.SUFFIX)


@dataclass
class CollisionReport:
    # tier -> key -> list of manifest_line_ids sharing that key
    collisions: dict[Tier, dict[str, list[object]]] = field(default_factory=dict)

    def total_lines_affected(self) -> int:
        seen: set[object] = set()
        for by_key in self.collisions.values():
            for ids in by_key.values():
                seen.update(ids)
        return len(seen)

    def warnings(self) -> list[str]:
        out = []
        for tier in LOOSE_TIERS:
            by_key = self.collisions.get(tier, {})
            if not by_key:
                continue
            affected = sum(len(ids) for ids in by_key.values())
            out.append(
                f"{affected} lines share a tier-{int(tier)} ({tier.name.lower()}) key "
                f"with at least one other line. Loose matching is disabled for these."
            )
        return out


def analyze_collisions(
    lines: Sequence[tuple[object, str]], suffix_len: int = DEFAULT_SUFFIX_LEN
) -> CollisionReport:
    """lines: list of (manifest_line_id, raw_barcode).

    Returns which specific keys, at the tiers used for loose matching,
    are shared by more than one line -- those keys should be disabled on
    the MatchIndex via disable_key(), not the tiers globally.
    """
    report = CollisionReport()
    per_tier: dict[Tier, dict[str, list[object]]] = {t: {} for t in LOOSE_TIERS}

    for line_id, raw in lines:
        norm = normalize(raw, suffix_len=suffix_len)
        for tier in LOOSE_TIERS:
            key = norm.key_for(tier)
            if key is None:
                continue
            per_tier[tier].setdefault(key, []).append(line_id)

    for tier, by_key in per_tier.items():
        colliding = {k: ids for k, ids in by_key.items() if len(ids) > 1}
        if colliding:
            report.collisions[tier] = colliding

    return report


def apply_collision_report(index: MatchIndex, report: CollisionReport) -> None:
    for tier, by_key in report.collisions.items():
        for key in by_key:
            index.disable_key(tier, key)
