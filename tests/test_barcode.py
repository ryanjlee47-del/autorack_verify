import barcode
from barcode import (
    MatchIndex,
    Resolution,
    Tier,
    analyze_collisions,
    apply_collision_report,
    gtin_canonicalize,
    normalize,
    parse_gs1,
    strip_control_chars,
    to_upper_ascii,
)

# ---------------------------------------------------------------------------
# Control-character stripping
# ---------------------------------------------------------------------------


def test_strip_control_chars_removes_nul_gs_and_whitespace_anywhere():
    raw = "AB\x00CD\x1cEF\x1dGH\x1eIJ\x1fKL MN\tOP\nQR"
    assert strip_control_chars(raw) == "ABCDEFGHIJKLMNOPQR"


def test_strip_control_chars_leaves_ordinary_punctuation():
    raw = "SKU-123_ALT.9"
    assert strip_control_chars(raw) == raw


def test_does_not_use_python_strip_semantics_at_edges_only():
    # A naive `.strip()` only removes from the ends; our function removes
    # matches anywhere, including in the middle of the string.
    raw = "12\x1d34"
    assert strip_control_chars(raw) == "1234"
    assert raw.strip() == raw  # nothing to trim at the *edges* here


# ---------------------------------------------------------------------------
# GS1 parsing
# ---------------------------------------------------------------------------


def test_parse_gs1_bracketed_notation():
    fields = parse_gs1("(01)00012345678905(10)LOT42(21)SER9")
    assert fields.gtin == "00012345678905"
    assert fields.lot == "LOT42"
    assert fields.serial == "SER9"


def test_parse_gs1_unbracketed_with_gs_separators():
    payload = "0100012345678905\x1d10LOT42\x1d21SER9\x1d"
    fields = parse_gs1(payload)
    assert fields.gtin == "00012345678905"
    assert fields.lot == "LOT42"
    assert fields.serial == "SER9"


def test_parse_gs1_fixed_length_ai_no_separator_needed():
    # AI 01 is fixed-length (14 digits); AI 17 (expiry, fixed 6) follows
    # immediately with no separator required. AI 30 (qty) is variable
    # length and, with no trailing GS, runs to end-of-string.
    payload = "0100012345678905" + "17251231" + "3000000005"
    fields = parse_gs1(payload)
    assert fields.gtin == "00012345678905"
    assert fields.extra["17"] == "251231"
    assert fields.qty == "00000005"


def test_parse_gs1_sscc():
    fields = parse_gs1("(00)106141411234567897")
    assert fields.sscc == "106141411234567897"


def test_parse_gs1_returns_none_for_plain_sku():
    assert parse_gs1("ALT-SKU-9000") is None


def test_serialized_carton_matches_unserialized_twin_on_gtin():
    plain = normalize("00012345678905")
    serialized = normalize("(01)00012345678905(21)SERIAL0001")
    assert plain.key_for(Tier.GTIN14) == serialized.key_for(Tier.GTIN14)


# ---------------------------------------------------------------------------
# GTIN canonicalization / check digit
# ---------------------------------------------------------------------------


def test_gtin_canonicalize_lengths_8_12_13_14_all_collapse_to_same_gtin14():
    upce = gtin_canonicalize("02532038")
    upca = gtin_canonicalize("025300000208")
    ean13 = gtin_canonicalize("0025300000208")
    gtin14 = gtin_canonicalize("00025300000208")
    assert upce.gtin14 == upca.gtin14 == ean13.gtin14 == gtin14.gtin14


def test_gtin_canonicalize_rejects_other_lengths_returns_none():
    assert gtin_canonicalize("123") is None
    assert gtin_canonicalize("123456789") is None  # 9 digits


def test_gtin_canonicalize_check_valid_flag():
    ok = gtin_canonicalize("025300000208")
    assert ok.check_valid is True
    bad = gtin_canonicalize("025300000209")
    assert bad.check_valid is False


def test_scan_missing_check_digit_matches_manifest_full_code():
    # Manifest has the full 12-digit UPC-A; the worker's scanner (or a
    # flaky decode) reports only the 11-digit body, no check digit.
    idx = MatchIndex()
    idx.add_line("line-1", "025300000208")
    result = idx.match("02530000020")  # 11 digits, no check digit
    assert result.is_resolved
    assert result.tier == Tier.BODY_NO_CHECK
    assert result.manifest_line_id == "line-1"


def test_manifest_missing_check_digit_matches_scan_full_code():
    # The reverse: the manifest export stripped the check digit (11-digit
    # body stored as raw_barcode), but the worker's phone reads the
    # physical barcode in full (12 digits, check digit included).
    idx = MatchIndex()
    idx.add_line("line-1", "02530000020")  # 11-digit body only
    result = idx.match("025300000208")  # full UPC-A as printed
    assert result.is_resolved
    assert result.tier == Tier.BODY_NO_CHECK


def test_upce_body_missing_check_digit_gets_body_no_check_key():
    # 7 digits is unambiguous: only a UPC-E body could be that short. Its
    # key must land on the same canonical ("00"-prefixed GTIN-14-level)
    # body a full UPC-E/UPC-A/EAN-13/GTIN-14 scan of the same product
    # would produce, so it matches regardless of which side has the full
    # code with its check digit.
    norm = normalize("0253203")  # 02532038 minus its check digit
    full = normalize("02532038")  # same product, check digit included
    assert norm.key_for(Tier.BODY_NO_CHECK) == full.key_for(Tier.BODY_NO_CHECK)
    assert norm.key_for(Tier.GTIN14) is None  # never guessed/completed


def test_ambiguous_12_and_13_digit_lengths_are_not_guessed_as_bodies():
    # A 12-digit code is always treated as a complete UPC-A, never as an
    # EAN-13 missing its check digit -- that would be guessing.
    norm12 = normalize("025300000208")
    info = gtin_canonicalize("025300000208")
    assert norm12.key_for(Tier.BODY_NO_CHECK) == info.body_no_check
    assert norm12.key_for(Tier.BODY_NO_CHECK) != "025300000208"


def test_body_no_check_strips_last_digit_only():
    info = gtin_canonicalize("025300000208")
    assert info.gtin14 == "00025300000208"
    assert info.body_no_check == "0002530000020"
    assert info.body_no_check + info.gtin14[-1] == info.gtin14


# ---------------------------------------------------------------------------
# normalize() / index keys
# ---------------------------------------------------------------------------


def test_normalize_alphanumeric_sku_gets_raw_and_normalized_keys_only():
    norm = normalize("alt-sku-9000")
    assert norm.key_for(Tier.RAW) == "alt-sku-9000"
    assert norm.key_for(Tier.NORMALIZED) == "ALT-SKU-9000"
    assert norm.key_for(Tier.GTIN14) is None
    assert norm.key_for(Tier.DIGITS_STRIPPED) is None


def test_normalize_numeric_gets_digits_stripped_leading_zeros():
    norm = normalize("00012345")
    assert norm.key_for(Tier.DIGITS_STRIPPED) == "12345"


def test_normalize_all_zero_digits_stripped_keeps_single_zero():
    norm = normalize("0000")
    assert norm.key_for(Tier.DIGITS_STRIPPED) == "0"


def test_normalize_suffix_key_requires_min_length_digits():
    norm = normalize("123", suffix_len=8)
    assert norm.key_for(Tier.SUFFIX) is None  # shorter than suffix_len


def test_normalize_suffix_len_floor_is_enforced():
    norm = normalize("1234567890", suffix_len=2)
    # MIN_SUFFIX_LEN=6 floors the effective length even if caller asks less
    assert norm.key_for(Tier.SUFFIX) == "567890"


def test_normalize_does_not_truncate_long_alphanumeric_codes():
    long_code = "A" * 5 + "1" * 20
    norm = normalize(long_code)
    assert norm.key_for(Tier.RAW) == long_code
    assert norm.key_for(Tier.NORMALIZED) == long_code


# ---------------------------------------------------------------------------
# Tiered matching + ambiguity guard
# ---------------------------------------------------------------------------


def test_match_resolves_at_tier1_normalized_when_unique():
    idx = MatchIndex()
    idx.add_line("line-1", "ALT-SKU-1")
    idx.add_line("line-2", "ALT-SKU-2")
    result = idx.match("alt-sku-1")
    assert result.is_resolved
    assert result.tier == Tier.NORMALIZED
    assert result.manifest_line_id == "line-1"


def test_match_falls_through_ambiguous_tier_to_next_tier():
    idx = MatchIndex()
    # Two lines share the same normalized string (case difference collapses)
    # but have distinct GTIN-14 values via distinct raw barcodes.
    idx.add_line("line-1", "00012345678905")
    idx.add_line("line-2", "12345678905")  # different digits, no collision here
    # Force an artificial ambiguity at tier 1 by aliasing both lines to the
    # same literal normalized string via two lines with identical raw text.
    idx2 = MatchIndex()
    idx2.add_line("dup-a", "SAME-CODE")
    idx2.add_line("dup-b", "SAME-CODE")
    result = idx2.match("SAME-CODE")
    assert result.resolution == Resolution.UNRESOLVED
    assert len(result.candidates_by_tier[Tier.RAW]) == 2
    assert len(result.candidates_by_tier[Tier.NORMALIZED]) == 2


def test_ambiguity_never_produces_a_reject_only_unresolved():
    idx = MatchIndex()
    idx.add_line("a", "DUPLICATE-CODE")
    idx.add_line("b", "DUPLICATE-CODE")
    result = idx.match("DUPLICATE-CODE")
    assert result.resolution == Resolution.UNRESOLVED
    assert result.manifest_line_id is None


def test_match_unresolved_when_nothing_found_at_any_tier():
    idx = MatchIndex()
    idx.add_line("a", "KNOWN-SKU")
    result = idx.match("TOTALLY-UNKNOWN-SKU")
    assert result.resolution == Resolution.UNRESOLVED


def test_off_length_numeric_gets_no_gtin14_key_but_can_still_resolve_via_digits_tier():
    idx = MatchIndex()
    idx.add_line("line-1", "00012345678905")  # GTIN-14 form on manifest
    # 11 digits is not an eligible GTIN length (8/12/13/14), so it must
    # never get a GTIN14 key -- we never guess/truncate on off-length
    # codes. It can still resolve via the looser digits-stripped tier,
    # which is exactly what that tier exists for (leading-zero variance).
    result = idx.match("12345678905")
    assert result.is_resolved
    assert result.tier == Tier.DIGITS_STRIPPED
    norm = normalize("12345678905")
    assert norm.key_for(Tier.GTIN14) is None


def test_match_upce_scan_resolves_against_upca_manifest_line():
    idx = MatchIndex()
    idx.add_line("line-1", "025300000208")  # manifest exported as UPC-A
    result = idx.match("02532038")  # worker's scanner reads UPC-E
    assert result.is_resolved
    assert result.tier == Tier.GTIN14
    assert result.manifest_line_id == "line-1"


def test_tier6_suffix_disabled_by_default():
    idx = MatchIndex(loose_match_enabled=False)
    idx.add_line("line-1", "9988776655443322")
    result = idx.match("76655443322")  # shares an 8-digit numeric suffix, not a full match
    assert result.resolution == Resolution.UNRESOLVED
    assert Tier.SUFFIX not in result.candidates_by_tier


def test_tier6_suffix_opt_in_and_requires_confirmation():
    idx = MatchIndex(loose_match_enabled=True, suffix_len=8)
    idx.add_line("line-1", "9988776655443322")
    result = idx.match("00076655443322")  # shares the last 8 digits
    assert result.is_resolved
    assert result.tier == Tier.SUFFIX
    assert result.needs_confirmation is True


def test_alias_tier_resolves_learned_mapping():
    idx = MatchIndex()
    idx.add_line("line-1", "OFFICIAL-SKU-1")
    idx.add_alias(to_upper_ascii(strip_control_chars("WEIRD-VENDOR-CODE")), "line-1")
    result = idx.match("weird-vendor-code")
    assert result.is_resolved
    assert result.tier == Tier.ALIAS
    assert result.needs_confirmation is False  # aliases are owner-confirmed already


def test_strict_tier_order_prefers_earlier_tier():
    idx = MatchIndex()
    idx.add_line("line-1", "12345678905")  # will hit at NORMALIZED (raw scan matches exactly)
    result = idx.match("12345678905")
    assert result.tier == Tier.RAW


def test_alias_is_not_overridden_by_a_later_colliding_loose_tier():
    """ALIAS is a 'certain'-confidence tier (TIER_CONFIDENCE), same as
    RAW/NORMALIZED/GTIN14 -- it must not sit behind DIGITS_STRIPPED/
    BODY_NO_CHECK ('high' confidence) in TIER_ORDER, or an owner's taught
    correction can be silently overridden by a later, unrelated
    manifest line whose loose-numeric key happens to collide.

    This is not hypothetical: an owner teaches "this scan means line A"
    via /exceptions' "remember this mapping" checkbox specifically
    because the scan didn't resolve automatically. If a line added
    *after* that lesson coincidentally shares a DIGITS_STRIPPED key with
    the taught scan, the next identical scan must still hit the alias,
    not the new line -- the taught mapping is a confirmed fact, the
    coincidental digit match is just a guess.
    """
    idx = MatchIndex(loose_match_enabled=True, suffix_len=8)
    taught_raw = "000012345"
    norm = normalize(taught_raw)
    idx.add_alias(norm.normalized, "line-A")

    # Sanity: the alias resolves on its own, before any collision exists.
    assert idx.match(taught_raw).tier == Tier.ALIAS
    assert idx.match(taught_raw).manifest_line_id == "line-A"

    # A different raw barcode, added later, whose leading-zero-stripped
    # digits happen to equal the taught scan's ("12345").
    idx.add_line("line-B", "00012345")
    assert normalize("00012345").key_for(Tier.DIGITS_STRIPPED) == norm.key_for(Tier.DIGITS_STRIPPED)

    result = idx.match(taught_raw)
    assert result.tier == Tier.ALIAS
    assert result.manifest_line_id == "line-A"


def test_alias_precedes_digits_stripped_and_body_no_check_in_tier_order():
    """Direct check on the ordering itself, not just one scenario it
    protects: ALIAS must sit with the other 'certain' tiers, not after
    the 'high'-confidence loose-numeric ones."""
    assert barcode.TIER_ORDER.index(Tier.ALIAS) < barcode.TIER_ORDER.index(Tier.DIGITS_STRIPPED)
    assert barcode.TIER_ORDER.index(Tier.ALIAS) < barcode.TIER_ORDER.index(Tier.BODY_NO_CHECK)


# ---------------------------------------------------------------------------
# Collision analysis
# ---------------------------------------------------------------------------


def test_collision_analysis_flags_shared_short_codes():
    lines = [("a", "12345"), ("b", "12345"), ("c", "99999")]
    report = analyze_collisions(lines)
    assert Tier.DIGITS_STRIPPED in report.collisions
    assert "12345" in report.collisions[Tier.DIGITS_STRIPPED]
    assert set(report.collisions[Tier.DIGITS_STRIPPED]["12345"]) == {"a", "b"}
    assert report.total_lines_affected() == 2


def test_collision_analysis_disables_only_the_colliding_key_not_whole_manifest():
    lines = [("a", "12345"), ("b", "12345"), ("c", "67890")]
    idx = MatchIndex()
    for line_id, raw in lines:
        idx.add_line(line_id, raw)
    report = analyze_collisions(lines)
    apply_collision_report(idx, report)

    # The colliding short code is now unresolved even though "12345" only
    # maps to tier candidates that are ambiguous anyway (belt and suspenders
    # -- this also holds once aliases/renumbering could otherwise make one
    # of them briefly unique).
    result_collision = idx.match("12345")
    assert result_collision.resolution == Resolution.UNRESOLVED

    # A non-colliding short code on the same manifest still resolves fine.
    result_clean = idx.match("67890")
    assert result_clean.is_resolved
    assert result_clean.manifest_line_id == "c"


def test_collision_report_warnings_message_format():
    lines = [(i, str(10000 + (i % 3))) for i in range(10)]
    report = analyze_collisions(lines)
    warnings = report.warnings()
    assert any("Loose matching is disabled" in w for w in warnings)
