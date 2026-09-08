"""Parity and hardening for the index/lookup layer of the matching engine.

tests/test_hash_parity.py covers normalize(). This file covers everything
built on top of it -- buildIndex/matchAgainstIndex in JavaScript against
MatchIndex in Python -- because that is the layer where the two engines
diverged in practice while the normalization fuzz reported zero
divergences across 30,011 inputs.

The reason the layers fail differently: normalize() is pure string
manipulation and both languages express it almost identically. The index
is a dict-and-set on one side and object literals on the other, and
JavaScript object literals answer lookups for keys nobody inserted --
every member of Object.prototype. A manifest line whose barcode text is
"constructor" is not a hypothetical: barcode payloads are free text
printed on cartons by whoever shipped them.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import barcode

HARNESS = Path(__file__).parent / "_index_parity_harness.js"

# Every name a plain {} answers for without being told to. The four in the
# original report are functions of arity 1, which is what made
# `hits.length === 1` true and produced a green OK on the dock for an item
# that is on no manifest at all.
PROTOTYPE_KEYS = [
    "constructor",
    "hasOwnProperty",
    "isPrototypeOf",
    "propertyIsEnumerable",
    "toString",
    "toLocaleString",
    "valueOf",
    "__proto__",
    "__defineGetter__",
]


def _js(job: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required to run the JS side of the parity test")
    proc = subprocess.run(
        [node, str(HARNESS)],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _python_match(rows, payloads, loose=False, suffix_len=8, disabled=None):
    idx = barcode.MatchIndex(loose_match_enabled=loose, suffix_len=suffix_len)
    for row in rows:
        idx.add_key(row["manifestLineId"], barcode.Tier(row["tier"]), row["key"])
    for tier_str, keys in (disabled or {}).items():
        for key in keys:
            idx.disable_key(barcode.Tier(int(tier_str)), key)
    out = []
    for raw in payloads:
        m = idx.match(raw)
        out.append(
            {
                "resolved": m.is_resolved,
                "tier": int(m.tier) if m.is_resolved and m.tier is not None else None,
                "manifestLineId": m.manifest_line_id if m.is_resolved else None,
                "needsConfirmation": bool(m.needs_confirmation),
            }
        )
    return out


# ---------------------------------------------------------------------------
# A3: a prototype-named payload must not resolve
# ---------------------------------------------------------------------------


def test_prototype_named_payload_does_not_resolve_against_an_unrelated_index():
    """The false OK on the dock.

    An index holding exactly one unrelated line answered `resolved: true,
    manifestLineId: undefined` for these payloads. app.js then looked up
    linesById[undefined], found nothing, and -- because
    sessionScannedLineIds[undefined] is falsy the first time -- classified
    the scan "ok" and flashed green. An item on no manifest at all was
    waved through.
    """
    rows = [{"manifestLineId": 42, "tier": 0, "key": "ABC123"}]
    js = _js({"rows": rows, "payloads": PROTOTYPE_KEYS, "looseMatchEnabled": False, "suffixLen": 8})
    assert js["buildError"] is None
    for key, result in zip(PROTOTYPE_KEYS, js["results"], strict=True):
        assert result["resolved"] is False, f"{key!r} resolved against an index without it"
    assert _python_match(rows, PROTOTYPE_KEYS) == js["results"]


def test_a_real_key_still_resolves():
    """The guard must not be so broad that it breaks matching."""
    rows = [{"manifestLineId": 42, "tier": 0, "key": "ABC123"}]
    js = _js({"rows": rows, "payloads": ["ABC123"], "looseMatchEnabled": False, "suffixLen": 8})
    assert js["results"][0]["resolved"] is True
    assert js["results"][0]["manifestLineId"] == 42
    assert _python_match(rows, ["ABC123"]) == js["results"]


# ---------------------------------------------------------------------------
# A4: buildIndex must survive a prototype-named key
# ---------------------------------------------------------------------------


def test_build_index_accepts_prototype_named_keys():
    """The silent-phone failure.

    `if (!byKey[row.key]) byKey[row.key] = []` found
    Function.prototype.constructor for row.key === "constructor", so the
    array was never created and .indexOf threw. The throw left matchIndex
    null, and handleDecoded returns early when it is -- so every scan for
    the rest of the shift was discarded with no beep, no flash and nothing
    written to the outbox. The phone looked powered on and did nothing.
    """
    rows = [
        {"manifestLineId": i + 1, "tier": 0, "key": key} for i, key in enumerate(PROTOTYPE_KEYS)
    ]
    js = _js({"rows": rows, "payloads": PROTOTYPE_KEYS, "looseMatchEnabled": False, "suffixLen": 8})
    assert js["buildError"] is None, f"buildIndex threw: {js['buildError']}"
    # And with the keys genuinely present, they now resolve on both sides.
    for key, result in zip(PROTOTYPE_KEYS, js["results"], strict=True):
        assert result["resolved"] is True, f"{key!r} was inserted but does not match"
    assert _python_match(rows, PROTOTYPE_KEYS) == js["results"]


def test_parse_gs1_prototype_ai_agrees():
    """A5: the same defect class one branch away from the key-generating path."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required")
    barcode_js = Path(__file__).parent.parent / "static" / "js" / "barcode.js"
    proc = subprocess.run(
        [
            node,
            "-e",
            "process.stdout.write(JSON.stringify("
            "require(process.argv[1]).parseGs1(process.argv[2])))",
            str(barcode_js),
            "(constructor)ABC",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) is None
    assert barcode.parse_gs1("(constructor)ABC") is None


# ---------------------------------------------------------------------------
# A6: disabled keys must suppress on both sides
# ---------------------------------------------------------------------------


def test_disabled_key_suppresses_matching_on_both_sides():
    """The server suppresses a collided key globally per tier (disable_key);
    the bundle used to merely omit the flagged rows. Collision analysis runs
    per manifest and matching runs per shift, so a key flagged in manifest A
    but clean in manifest B was dead on the server and live on the phone --
    the phone resolved to B's line and reported `ok`, and the server never
    re-verifies `ok`, so the disagreement was never observed.

    manifest_ingest.bundle_payload now ships the disabled set and app.js
    passes it to matchAgainstIndex; this pins that the two agree.
    """
    # Line 7 is clean in its own manifest, but its key is flagged elsewhere.
    rows = [{"manifestLineId": 7, "tier": 1, "key": "SHARED-KEY"}]
    disabled = {"1": ["SHARED-KEY"]}

    js = _js(
        {
            "rows": rows,
            "payloads": ["SHARED-KEY"],
            "looseMatchEnabled": False,
            "suffixLen": 8,
            "disabledKeys": disabled,
        }
    )
    assert js["results"][0]["resolved"] is False, "phone resolved a key the server suppressed"
    assert _python_match(rows, ["SHARED-KEY"], disabled=disabled) == js["results"]

    # Without the suppression both sides resolve it -- proving the test is
    # measuring the disabled set and not something else.
    js_live = _js(
        {"rows": rows, "payloads": ["SHARED-KEY"], "looseMatchEnabled": False, "suffixLen": 8}
    )
    assert js_live["results"][0]["resolved"] is True


def test_bundle_and_server_agree_on_a_key_collided_in_one_manifest_only(tmp_path):
    """The end-to-end A6 case, which is the one the parity suite could not see.

    Two manifests run in the same shift. Manifest A contains two lines that
    collide on a loose key, so ingest flags that key. Manifest B contains one
    clean line carrying the same key.

    The server's MatchIndex suppresses the key globally per tier, so it
    resolves nothing. The bundle used to simply omit A's flagged rows, which
    left B's row live on the phone -- so the phone resolved to B's line and
    reported a confident `ok`. The server never re-verifies `ok`, so that
    disagreement produced no exception, no billing event, and no signal of
    any kind. The two sides must now reach the same verdict.
    """
    import db
    import manifest_ingest

    conn = db.init_db(tmp_path / "t.db")
    # Loose matching on, so tier 6 (suffix) participates and can collide.
    account_id = db.create_account(
        conn, "Acme", 900, 0, loose_match_enabled=True, loose_suffix_len=8
    )
    account = db.get_account(conn, account_id)

    def line(no, sku, code):
        return {
            "line_no": no,
            "sku": sku,
            "description": sku,
            "qty_expected": 1,
            "raw_barcode": code,
        }

    # Two codes sharing their last 8 digits: same tier-6 key, different items.
    manifest_a, report_a = manifest_ingest.commit_manifest(
        conn,
        account_id,
        "A",
        None,
        [line(1, "A-1", "111199887766"), line(2, "A-2", "222299887766")],
        True,
        8,
    )
    assert report_a.total_lines_affected() >= 2, "fixture must actually produce a collision"

    manifest_b, report_b = manifest_ingest.commit_manifest(
        conn, account_id, "B", None, [line(1, "B-1", "333399887766")], True, 8
    )
    assert report_b.total_lines_affected() == 0, "manifest B must be clean on its own"

    shift = {"id": None, "label": "S", "date": "2026-09-07", "bundle_version": 0}
    manifest_ids = [manifest_a, manifest_b]
    payload = manifest_ingest.bundle_payload(conn, account, shift, manifest_ids)

    # The colliding key is shipped as disabled rather than merely omitted.
    suffix_tier = str(int(barcode.Tier.SUFFIX))
    assert "99887766" in payload["disabledKeys"].get(suffix_tier, []), (
        "the bundle must tell the phone which keys the server suppressed"
    )

    # And the two engines now agree on the payload that hits that key.
    probe = "444499887766"
    server_idx = manifest_ingest.build_match_index(conn, account, manifest_ids)
    server_result = server_idx.match(probe)

    js = _js(
        {
            "rows": payload["keys"],
            "payloads": [probe],
            "looseMatchEnabled": payload["settings"]["looseMatchEnabled"],
            "suffixLen": payload["settings"]["looseSuffixLen"],
            "disabledKeys": payload["disabledKeys"],
        }
    )
    assert js["results"][0]["resolved"] == server_idx.match(probe).is_resolved
    assert server_result.is_resolved is False
    assert js["results"][0]["resolved"] is False, (
        "the phone resolved a key the server suppressed -- a confident OK the "
        "server would have called unresolved, and never re-checks"
    )


# ---------------------------------------------------------------------------
# A7 / A8: constants that must be mirrored, not transcribed
# ---------------------------------------------------------------------------


def test_confirmation_required_tiers_matches_javascript():
    """A7. Python exports CONFIRMATION_REQUIRED_TIERS; JavaScript used to
    write the condition out longhand as `tier === Tier.SUFFIX`. Identical in
    behaviour, invisible to a parity test -- in a file whose entire premise
    is that its constants are mirrored and test-enforced. TIER_ORDER had
    such a test and this did not, which is exactly where the divergence
    that produced a false OK was found.
    """
    js = _js({"rows": [], "payloads": [], "looseMatchEnabled": False, "suffixLen": 8})
    assert sorted(int(t) for t in barcode.CONFIRMATION_REQUIRED_TIERS) == sorted(
        js["confirmationRequiredTiers"]
    )


def test_suffix_bounds_match_javascript():
    js = _js({"rows": [], "payloads": [], "looseMatchEnabled": False, "suffixLen": 8})
    assert js["minSuffixLen"] == barcode.MIN_SUFFIX_LEN
    assert js["defaultSuffixLen"] == barcode.DEFAULT_SUFFIX_LEN


def test_control_chars_match_javascript():
    js = _js({"rows": [], "payloads": [], "looseMatchEnabled": False, "suffixLen": 8})
    assert [ord(c) for c in barcode.CONTROL_CHARS] == js["controlChars"]


def test_zero_suffix_len_clamps_identically():
    """A8. Python clamps with max(suffix_len, MIN_SUFFIX_LEN) -> 6.
    JavaScript used `options.suffixLen || DEFAULT_SUFFIX_LEN`, and 0 is
    falsy, so it fell back to 8 -- different tier-6 keys on the two sides
    for the same physical label, which is a silent phantom-reject generator
    that was reachable straight from the admin account form.
    """
    payload = "01234567890123"
    rows = [{"manifestLineId": 1, "tier": 6, "key": payload[-barcode.MIN_SUFFIX_LEN :]}]
    js = _js({"rows": rows, "payloads": [payload], "looseMatchEnabled": True, "suffixLen": 0})
    py = _python_match(rows, [payload], loose=True, suffix_len=0)
    assert js["results"] == py
    assert js["results"][0]["resolved"] is True, (
        "0 must clamp to MIN_SUFFIX_LEN, not fall back to 8"
    )
    assert js["results"][0]["needsConfirmation"] is True  # tier 6 always needs confirmation
