"""The phone and the server must agree, byte for byte.

The worker PWA decides match/mismatch offline with frontend/shared/barcode.js;
the server re-decides with autorack/matching.py. If they ever disagree the
worker is shown one answer and the record says another. This runs both over
the same adversarial corpus and compares normalized keys and match decisions.

Skips when `node` is unavailable; CI installs Node and asserts this did not skip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autorack.matching import TIER_ORDER, MatchIndex, analyze_collisions, apply_collision_report, normalize

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

LINES = [
    ("l-upca", "025300000208"),
    ("l-ean", "4006381333931"),
    ("l-gtin14", "10012345678902"),
    ("l-nocheck", "01234567890"),
    ("l-alpha", "VND-88213"),
    ("l-long-a", "000011112222333344"),
    ("l-long-b", "990011112222333344"),
    ("l-ws", "  036000291452 "),
    ("l-gs1", "(01)00012345678905(10)LOT1"),
]
ALIASES = [("INTERNAL-5521", "l-alpha"), ("777", "l-ean")]

CORPUS = [
    "025300000208",
    "02532038",
    "0025300000208",
    "00025300000208",
    "(01)00025300000208",
    "4006381333931",
    "04006381333931",
    "99994006381333931",
    "400638133393",
    "10012345678902",
    "1001234567890",
    "012345678905",
    "01234567890",
    "12345678905",
    "vnd-88213",
    " VND-88213\r\n",
    "VND\x1d-88213",
    "internal-5521",
    "777",
    "0777",
    "5511112222333344",
    "11112222333344",
    "036000291452",
    "\x00036000291452\x00",
    "\x1d0100012345678905" + "10LOT1\x1d21SER",
    "(01)00012345678905",
    "0100012345678905",
    "(01)" + "²" * 14,
    "٠" * 12,
    "",
    " ",
    "\x1c\x1d\x1e\x1f",
    "constructor",
    "__proto__",
    "toString",
    "hasOwnProperty",
    "café",
    "ß" * 3,
    "12345",
    "1234567",
    "12345678901234567890",
    "(00)123456789012345678",
    "(17)260101(10)ABC",
    "410" + "1" * 13,
    "(21)",
    "01" + "9" * 20,
]

JS = """
import * as B from %s;
let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const input = JSON.parse(raw);
const out = { tierOrder: B.TIER_ORDER, confirm: B.CONFIRMATION_REQUIRED_TIERS, normalize: [], match: {} };
for (const p of input.payloads) {
  const n = B.normalize(p, input.suffixLen);
  out.normalize.push({ normalized: n.normalized, keys: n.keys });
}
for (const [name, cfg] of Object.entries(input.indexes)) {
  const idx = B.buildIndex(cfg.rows);
  out.match[name] = input.payloads.map((p) => {
    const opts = { looseMatchEnabled: cfg.loose, suffixLen: input.suffixLen, disabledKeys: cfg.disabled };
    const m = B.matchAgainstIndex(idx, p, opts);
    return { resolved: m.resolved, tier: m.tier, line: m.lineId, confirm: m.needsConfirmation, ambiguous: m.ambiguous };
  });
}
process.stdout.write(JSON.stringify(out));
"""


def build_index(loose: bool, suffix_len: int) -> MatchIndex:
    idx = MatchIndex(loose_match_enabled=loose, suffix_len=suffix_len)
    for lid, bc in LINES:
        idx.add_line(lid, bc)
    for key, lid in ALIASES:
        idx.add_alias(key, lid)
    apply_collision_report(idx, analyze_collisions(LINES, suffix_len=suffix_len))
    return idx


def run_js(payload: dict) -> dict:
    script = JS % json.dumps((FRONTEND / "shared" / "barcode.js").as_uri())
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],  # type: ignore[list-item]
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("suffix_len", [6, 8, 11])
def test_python_and_javascript_engines_agree(suffix_len: int):
    indexes = {}
    for name, loose in (("strict", False), ("loose", True)):
        idx = build_index(loose, suffix_len)
        indexes[name] = {
            "loose": loose,
            "rows": [{"line_id": lid, "tier": tier, "key": key} for lid, tier, key in idx.rows()],
            "disabled": idx.disabled_rows(),
            "_py": idx,
        }
    js = run_js(
        {
            "payloads": CORPUS,
            "suffixLen": suffix_len,
            "indexes": {k: {kk: vv for kk, vv in v.items() if kk != "_py"} for k, v in indexes.items()},
        }
    )

    assert js["tierOrder"] == [int(t) for t in TIER_ORDER]
    assert js["confirm"] == [6]

    for payload, got in zip(CORPUS, js["normalize"], strict=True):
        n = normalize(payload, suffix_len=suffix_len)
        want = {str(int(t)): k for t, k in n.keys.items()}
        assert got["normalized"] == n.normalized, repr(payload)
        assert got["keys"] == want, repr(payload)

    for name, cfg in indexes.items():
        idx: MatchIndex = cfg["_py"]  # type: ignore[assignment]
        for payload, got in zip(CORPUS, js["match"][name], strict=True):
            m = idx.match(payload)
            want = {
                "resolved": m.is_resolved,
                "tier": int(m.tier) if m.tier is not None else None,
                "line": m.line_id,
                "confirm": m.needs_confirmation,
                "ambiguous": m.ambiguous,
            }
            assert got == want, (name, repr(payload))


def test_offline_payload_index_rebuilds_identically_in_js(client):
    """The exact index the API ships to phones must drive the JS engine to the
    same decisions the server makes."""
    from conftest import make_order, signup, worker_on_phone

    owner = signup(client)
    client.patch("/api/warehouse", json={"loose_match_enabled": True}, headers=owner.h)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [(bc.strip(), 1) for _, bc in LINES])["id"]
    payload = client.get(f"/api/worker/orders/{oid}", headers=phone.h).json()
    by_barcode = {li["expected_barcode"]: li["id"] for li in payload["lines"]}

    js = run_js(
        {
            "payloads": CORPUS,
            "suffixLen": payload["match"]["suffix_len"],
            "indexes": {
                "api": {
                    "loose": payload["match"]["loose_match_enabled"],
                    "rows": payload["match"]["index"],
                    "disabled": payload["match"]["disabled_keys"],
                }
            },
        }
    )
    idx = MatchIndex(loose_match_enabled=True, suffix_len=payload["match"]["suffix_len"])
    lines = [(by_barcode[bc.strip()], bc.strip()) for _, bc in LINES]
    for lid, bc in lines:
        idx.add_line(lid, bc)
    apply_collision_report(idx, analyze_collisions(lines, suffix_len=idx.suffix_len))
    for p, got in zip(CORPUS, js["match"]["api"], strict=True):
        m = idx.match(p)
        assert (got["resolved"], got["line"], got["ambiguous"]) == (m.is_resolved, m.line_id, m.ambiguous), repr(p)
