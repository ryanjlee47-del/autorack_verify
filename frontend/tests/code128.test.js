import assert from "node:assert/strict";
import { test } from "node:test";

import { PATTERNS, encode, modules } from "../shared/code128.js";

// Decode module widths back to text, the way a scanner would.
function decode(widths) {
  const syms = [];
  for (let i = 0; i < widths.length - 7; i += 6) syms.push(PATTERNS.indexOf(widths.slice(i, i + 6).join("")));
  const stop = widths.slice(widths.length - 7).join("");
  assert.equal(stop, PATTERNS[106]);
  const [start, ...rest] = syms;
  const check = rest.pop();
  let sum = start;
  rest.forEach((c, i) => { sum += c * (i + 1); });
  assert.equal(sum % 103, check, "checksum");
  if (start === 105) return rest.map((c) => String(c).padStart(2, "0")).join("");
  assert.equal(start, 104);
  return rest.map((c) => String.fromCharCode(c + 32)).join("");
}

test("every pattern is 11 modules (stop is 13)", () => {
  PATTERNS.forEach((p, i) => assert.equal(p.split("").map(Number).reduce((a, b) => a + b, 0), i === 106 ? 13 : 11, String(i)));
  assert.equal(new Set(PATTERNS).size, 107);
});

test("round trips text and digits", () => {
  for (const v of ["012345678905", "WID-BLU-12", "AR0000123456", "sku 7/b", "1"]) {
    assert.equal(decode(modules(v)), v);
  }
  assert.equal(encode("0123")[0], 105); // even digits: code set C
  assert.equal(encode("123")[0], 104); // odd: code set B
  assert.throws(() => encode("é"));
  assert.throws(() => encode(""));
});
