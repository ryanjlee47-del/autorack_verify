"""Hash parity between barcode.py and static/js/barcode.js.

The FNC1 parity trap: Python's str.strip() and JavaScript's .trim() treat
control characters differently (see barcode.py's module docstring). If the
phone and the server ever normalize the same raw payload differently, the
same physical label hashes to two different keys depending on which side
computed it -- a phantom reject, and phantom rejects are billable events
that were never earned.

This test extracts the actual normalization functions from both source
files (barcode.py directly; static/js/barcode.js via a small Node.js
harness script, tests/_parity_harness.js) and asserts they agree, byte for
byte, across a battery of adversarial inputs.
"""

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import barcode

HARNESS = Path(__file__).parent / "_parity_harness.js"

# 20+ adversarial cases: control chars in the middle (not just the edges,
# where Python's str.strip() and JS's .trim() diverge most), NUL bytes,
# mixed case, GS1 element strings with real GS separators, alphanumeric
# SKUs, UPC-E/UPC-A/EAN-13/GTIN-14 numeric codes, and empty-ish edge cases.
ADVERSARIAL_CASES = [
    "012345678905",
    "\x1c012345678905\x1c",
    "\x1d012345678905\x1d",
    "  012345678905  ",
    "\x00012345678905\x00",
    "012345\x1f678905",
    "012345\x00678905",
    "abc123XYZ",
    "abc123xyz",
    "  abc123xyz\t\n",
    "\x1e abc \x1e 123 \x1f xyz \x1e",
    "(01)00012345678905(10)LOT42",
    "\x1d01000123456789051017LOT42\x1d",
    "0100012345678905\x1d10LOT42\x1d21SERIAL9\x1d",
    "0100012345678905",
    "04252614",
    "02532038",
    "042100005264",
    "0425261400001",
    "00012345678905",
    "3S-99-ALPHA",
    "",
    "   ",
    "\x00\x00\x00",
    "\x1c\x1d\x1e\x1f",
    "MiXeD-Case_123",
    "999999999999999999",
    "(17)251231(10)B1(21)S9",
    "02530000020",  # 11-digit UPC-A body, check digit omitted
    "0253203",  # 7-digit UPC-E body, check digit omitted
    "025300000208",  # complete UPC-A, must NOT be treated as an 11/7-digit body
]


def _python_results():
    out = []
    for raw in ADVERSARIAL_CASES:
        stripped = barcode.strip_control_chars(raw)
        norm = barcode.normalize(raw)
        keys = {str(int(tier)): key for tier, key in norm.keys.items()}
        out.append({"stripped": stripped, "normalized": norm.normalized, "keys": keys})
    return out


def _js_results():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required to run the JS side of the parity test")
    proc = subprocess.run(
        [node, str(HARNESS)],
        input=json.dumps(ADVERSARIAL_CASES),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_at_least_20_adversarial_cases():
    assert len(ADVERSARIAL_CASES) >= 20


def test_stripped_and_normalized_match():
    py_results = _python_results()
    js_results = _js_results()
    assert len(py_results) == len(js_results) == len(ADVERSARIAL_CASES)
    for raw, py, js in zip(ADVERSARIAL_CASES, py_results, js_results, strict=False):
        assert py["stripped"] == js["stripped"], (
            f"strip mismatch for {raw!r}: py={py['stripped']!r} js={js['stripped']!r}"
        )
        assert py["normalized"] == js["normalized"], (
            f"normalize mismatch for {raw!r}: py={py['normalized']!r} js={js['normalized']!r}"
        )


def test_index_keys_match():
    py_results = _python_results()
    js_results = _js_results()
    for raw, py, js in zip(ADVERSARIAL_CASES, py_results, js_results, strict=False):
        assert py["keys"] == js["keys"], (
            f"key mismatch for {raw!r}: py={py['keys']} js={js['keys']}"
        )


def _js_tier_order():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required to run the JS side of the parity test")
    barcode_js = Path(__file__).parent.parent / "static" / "js" / "barcode.js"
    # Path passed as a real subprocess argument (process.argv[1]), not
    # interpolated into the -e string -- avoids any quoting fragility
    # from spaces or special characters in the path.
    proc = subprocess.run(
        [
            node,
            "-e",
            "process.stdout.write(JSON.stringify(require(process.argv[1]).TIER_ORDER))",
            str(barcode_js),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_tier_order_matches_javascript():
    """Matching runs on both sides -- offline on the phone, and re-verified
    server-side for REJECT claims (app.py's _bill_confirmed_reject) -- so
    the two TIER_ORDER lists must be not just equal as sets but identical
    in sequence: whichever tier resolves first wins, and a divergence
    here is the same class of phantom-mismatch risk as the FNC1 parity
    trap in normalize() (this file's other tests), just one level up the
    pipeline. Exactly this kind of silent drift is what let ALIAS end up
    behind DIGITS_STRIPPED/BODY_NO_CHECK in both files independently --
    see barcode.py's TIER_ORDER comment and
    test_barcode.py::test_alias_is_not_overridden_by_a_later_colliding_loose_tier.
    """
    py_order = [int(t) for t in barcode.TIER_ORDER]
    js_order = _js_tier_order()
    assert py_order == js_order


# ---------------------------------------------------------------------------
# Unicode digits (finding A1/A2)
#
# str.isdigit() returns True for characters int() cannot parse (superscripts)
# AND for non-ASCII decimal digits int() *can* parse (Arabic-Indic and
# friends). barcode.js's isDigits is ASCII-only by construction, so every
# .isdigit() in barcode.py was a divergence -- and the superscript case was
# worse than a divergence: gtin_canonicalize gated on .isdigit() and then
# handed each character to int(), raising ValueError out of normalize() on a
# path with no handler anywhere in it. That is reachable from an
# unauthenticated worker session via /w/sync's rawPayload, and because the
# outbox only drops what the server names, one such payload wedged that
# phone's sync queue permanently.
#
# These runs are 14 characters long on purpose: that is what reaches AI 01's
# fixed-length branch, which is where the crash lived. The existing
# adversarial corpus contains Unicode, but no run long enough to get there.
# ---------------------------------------------------------------------------

UNICODE_DIGIT_CASES = [
    "(01)" + "²" * 14,  # SUPERSCRIPT TWO -- isdigit() true, int() raises
    "(01)" + "³" * 14,  # SUPERSCRIPT THREE
    "(01)" + "¹" * 14,  # SUPERSCRIPT ONE
    "(01)" + "".join(chr(0x0660 + (i % 10)) for i in range(14)),  # ARABIC-INDIC
    "(01)" + "".join(chr(0x06F0 + (i % 10)) for i in range(14)),  # EXTENDED ARABIC-INDIC
    "(01)" + "".join(chr(0x0966 + (i % 10)) for i in range(14)),  # DEVANAGARI
    "²" * 8,  # bare 8 "digits": the UPC-E canonicalization branch
    "²" * 12,
    # chr(0x0660) is ARABIC-INDIC DIGIT ZERO. Written as chr() rather than
    # the literal glyph: it is visually indistinguishable from characters it
    # is not, which is the whole reason this class of bug survived review.
    chr(0x0660) * 12,  # bare 12: the UPC-A branch
    chr(0x0660) * 13,
    chr(0x0660) * 7,  # the 7/11 "check digit omitted" branch
    chr(0x0660) * 11,
    "1234" + chr(0x0660) + "5678",  # mixed ASCII and non-ASCII digits
    chr(0x0660) + "1234567890",
]


def test_normalize_does_not_raise_on_unicode_digits():
    """A1: the crash itself, independent of parity.

    normalize() is called inside /w/sync's per-scan db.transaction with no
    handler in the call chain, so an exception here is a 500 for the whole
    batch, not a rejected scan.
    """
    for raw in UNICODE_DIGIT_CASES:
        barcode.normalize(raw)  # must not raise


def test_unicode_digit_keys_match_javascript():
    """A2: the non-crashing half -- the server emitting keys the phone can
    never compute, which is a phantom reject, which is a billable event
    that was not earned."""
    py = []
    for raw in UNICODE_DIGIT_CASES:
        norm = barcode.normalize(raw)
        py.append(
            {
                "stripped": barcode.strip_control_chars(raw),
                "normalized": norm.normalized,
                "keys": {str(int(tier)): key for tier, key in norm.keys.items()},
            }
        )

    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required to run the JS side of the parity test")
    proc = subprocess.run(
        [node, str(HARNESS)],
        input=json.dumps(UNICODE_DIGIT_CASES),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    js = json.loads(proc.stdout)

    for raw, p, j in zip(UNICODE_DIGIT_CASES, py, js, strict=True):
        assert p["normalized"] == j["normalized"], f"normalize mismatch for {raw!r}"
        assert p["keys"] == j["keys"], f"key mismatch for {raw!r}: py={p['keys']} js={j['keys']}"


def test_no_isdigit_remains_in_the_engine():
    """The fix is only durable if the next edit cannot reintroduce it.

    str.isdigit() has no correct use in this module: every digit test here
    must agree with barcode.js's ASCII-only isDigits(). barcode._is_digits
    is the replacement.
    """
    source = (Path(__file__).parent.parent / "barcode.py").read_text()
    # Parsed, not grepped: the docstring of _is_digits explains at length why
    # .isdigit() is banned, and a text search cannot tell that prose from a
    # call. The AST can -- it only sees real attribute accesses.
    tree = ast.parse(source)
    offenders = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "isdigit"
    ]
    assert not offenders, "barcode.py must use _is_digits(), not .isdigit(): " + ", ".join(
        offenders
    )
