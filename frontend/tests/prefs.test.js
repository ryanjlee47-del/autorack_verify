import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanScan } from "../w/js/prefs.js";

test("cleanScan drops AIM symbology prefixes, keeping GS1 data parseable", () => {
  assert.equal(cleanScan("]E0012345678905"), "012345678905");
  assert.equal(cleanScan("]C10109501101530003"), "\u001d0109501101530003");
  assert.equal(cleanScan("012345678905\r\n"), "012345678905");
  assert.equal(cleanScan("ABC-123"), "ABC-123");
  assert.equal(cleanScan("]"), "]");
});
