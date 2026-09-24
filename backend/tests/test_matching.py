"""The matching engine, in isolation."""

from __future__ import annotations

import pytest

from autorack.matching import (
    MatchIndex,
    Tier,
    analyze_collisions,
    apply_collision_report,
    compute_check_digit,
    equivalent_line_groups,
    gtin_canonicalize,
    normalize,
    normalized_key,
    parse_gs1,
    upce_to_upca,
)


def index(*lines: tuple[str, str], loose: bool = False, collisions: bool = True) -> MatchIndex:
    idx = MatchIndex(loose_match_enabled=loose)
    for lid, bc in lines:
        idx.add_line(lid, bc)
    if collisions:
        apply_collision_report(idx, analyze_collisions(list(lines)))
    return idx


def test_exact_and_normalized():
    idx = index(("a", "012345678905"), ("b", "ABC-123"))
    assert idx.match("012345678905").tier == Tier.RAW
    r = idx.match("  012345678905\r\n")
    assert r.line_id == "a" and r.tier == Tier.NORMALIZED
    assert idx.match("abc-123").line_id == "b"


def test_upce_scan_matches_upca_line():
    # 02532038 is the UPC-E form of UPC-A 025300000208 (same carton).
    assert upce_to_upca("02532038") == "025300000208"
    idx = index(("a", "025300000208"))
    r = idx.match("02532038")
    assert r.line_id == "a" and r.tier == Tier.GTIN14


def test_ean13_and_gtin14_equivalence():
    idx = index(("a", "0012345678905"))
    assert idx.match("012345678905").line_id == "a"
    assert idx.match("00012345678905").line_id == "a"


def test_gs1_element_string_matches_plain_gtin():
    idx = index(("a", "012345678905"))
    assert idx.match("(01)00012345678905(10)LOT42").line_id == "a"
    assert idx.match("\x1d0100012345678905" + "10LOT42\x1d21SER1").line_id == "a"
    fields = parse_gs1("0100012345678905" + "10LOT42\x1d21SER9")
    assert fields and fields.gtin == "00012345678905" and fields.lot == "LOT42" and fields.serial == "SER9"


def test_missing_check_digit_matches_either_direction():
    idx = index(("a", "01234567890"))  # 11-digit body, check digit omitted
    r = idx.match("012345678905")
    assert r.line_id == "a" and r.tier in (Tier.DIGITS_STRIPPED, Tier.BODY_NO_CHECK)
    idx2 = index(("a", "012345678905"))
    assert idx2.match("01234567890").line_id == "a"


def test_unknown_barcode_is_confident_mismatch():
    idx = index(("a", "012345678905"), ("b", "036000291452"))
    r = idx.match("9780306406157")
    assert not r.is_resolved and not r.ambiguous


def test_two_lines_same_product_in_different_formats_is_ambiguous_not_a_guess():
    idx = index(("a", "025300000208"), ("b", "02532038"))
    r = idx.match("025300000208")
    # Tier 0 finds exactly line a (raw string), so that one is fine...
    assert r.line_id == "a"
    # ...but a GS1 payload is only visible at the GTIN-level tiers, where it
    # hits both lines: never guess.
    r = idx.match("(01)00025300000208")
    assert not r.is_resolved and r.ambiguous
    assert equivalent_line_groups([("a", "025300000208"), ("b", "02532038")]) == [["a", "b"]]


def test_colliding_loose_key_is_disabled_and_marks_ambiguous():
    # Different GTINs whose zero-stripped digits differ but suffixes collide.
    lines = [("a", "000011112222333344"), ("b", "990011112222333344")]
    idx = index(*lines, loose=True)
    r = idx.match("5511112222333344")
    assert not r.is_resolved
    assert r.ambiguous  # suffix key is shared -> disabled -> ambiguous, not a mismatch


def test_suffix_tier_needs_confirmation():
    idx = index(("a", "4006381333931"), loose=True)
    r = idx.match("99994006381333931")
    assert r.is_resolved and r.tier == Tier.SUFFIX and r.needs_confirmation


def test_suffix_tier_is_off_by_default():
    idx = index(("a", "4006381333931"))
    assert not idx.match("99994006381333931").is_resolved


def test_alias_outranks_loose_tiers():
    idx = MatchIndex()
    idx.add_line("a", "VENDOR-XYZ")
    idx.add_line("b", "00000777")
    idx.add_alias(normalized_key("777"), "a")
    r = idx.match("777")
    assert r.line_id == "a" and r.tier == Tier.ALIAS


def test_check_digit_and_canonicalization():
    assert compute_check_digit("01234567890") == "5"
    info = gtin_canonicalize("012345678905")
    assert info and info.gtin14 == "00012345678905" and info.check_valid
    assert gtin_canonicalize("12345") is None


@pytest.mark.parametrize("payload", ["(01)" + "²" * 14, "٠" * 12, "", "\x00\x1d\x1e", "constructor"])
def test_hostile_payloads_do_not_crash(payload: str):
    normalize(payload)
    index(("a", "012345678905")).match(payload)


def test_normalized_key_strips_separators_and_case():
    assert normalized_key(" ab\x1dc\t") == "ABC"


def test_index_rows_roundtrip():
    idx = index(("a", "012345678905"))
    rows = idx.rows()
    assert ("a", int(Tier.GTIN14), "00012345678905") in rows
    rebuilt = MatchIndex()
    for lid, tier, key in rows:
        rebuilt.add_key(lid, Tier(tier), key)
    assert rebuilt.match("012345678905").line_id == "a"
