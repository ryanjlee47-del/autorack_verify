import assert from "node:assert/strict";
import { test } from "node:test";

import { STRINGS, T, setLang } from "../w/js/i18n.js";

test("every string exists in English and Spanish", () => {
  const en = Object.keys(STRINGS.en).sort();
  const es = Object.keys(STRINGS.es).sort();
  assert.deepEqual(es, en);
  for (const [lang, table] of Object.entries(STRINGS)) {
    for (const [k, v] of Object.entries(table)) assert.ok(v.trim(), `${lang}.${k} is empty`);
  }
});

test("placeholders match between languages", () => {
  const vars = (s) => (s.match(/\{\w+\}/g) || []).sort().join(",");
  for (const k of Object.keys(STRINGS.en)) assert.equal(vars(STRINGS.es[k]), vars(STRINGS.en[k]), k);
});

test("T substitutes every occurrence and falls back", () => {
  setLang("es");
  assert.equal(T("queued", { n: 3 }), "3 por sincronizar");
  assert.equal(T("no_such_key"), "no_such_key");
  setLang("xx");
  assert.equal(T("offline"), "Offline");
});
