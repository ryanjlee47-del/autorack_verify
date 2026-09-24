import assert from "node:assert/strict";
import { test } from "node:test";

import { Tier, buildIndex, matchAgainstIndex, normalize, normalizedKey } from "../shared/barcode.js";

test("GS1 payloads canonicalize to GTIN-14", () => {
  assert.equal(normalize("(01)00012345678905(10)LOT").keys[Tier.GTIN14], "00012345678905");
  assert.equal(normalize("\x1d0100012345678905").keys[Tier.GTIN14], "00012345678905");
});

test("prototype-named payloads never match", () => {
  const idx = buildIndex([{ line_id: "a", tier: 1, key: "X" }]);
  for (const p of ["constructor", "__proto__", "toString", "hasOwnProperty"]) {
    assert.equal(matchAgainstIndex(idx, p).resolved, false);
  }
});

test("disabled keys are ambiguous, not mismatches", () => {
  const idx = buildIndex([{ line_id: "a", tier: 6, key: "22333344" }, { line_id: "b", tier: 6, key: "22333344" }]);
  const r = matchAgainstIndex(idx, "5511112222333344", { looseMatchEnabled: true, disabledKeys: { 6: ["22333344"] } });
  assert.equal(r.resolved, false);
  assert.equal(r.ambiguous, true);
});

test("normalizedKey strips separators without trim()", () => {
  assert.equal(normalizedKey("\x1d ab\x1c"), "AB");
});
