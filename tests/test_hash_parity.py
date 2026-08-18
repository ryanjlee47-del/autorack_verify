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
