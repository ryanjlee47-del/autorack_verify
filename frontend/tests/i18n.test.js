import assert from "node:assert/strict";
import { test } from "node:test";

import { LANGUAGES, STRINGS, T, setLang } from "../w/js/i18n.js";

test("every string exists in every language", () => {
  const en = Object.keys(STRINGS.en).sort();
  assert.deepEqual(Object.keys(STRINGS).sort(), LANGUAGES.map(([code]) => code).sort());
  for (const [lang, table] of Object.entries(STRINGS)) {
    assert.deepEqual(Object.keys(table).sort(), en, `${lang} is missing or has extra keys`);
    for (const [k, v] of Object.entries(table)) assert.ok(v.trim(), `${lang}.${k} is empty`);
  }
});

test("placeholders match between languages", () => {
  const vars = (s) => (s.match(/\{\w+\}/g) || []).sort().join(",");
  for (const lang of Object.keys(STRINGS)) {
    for (const k of Object.keys(STRINGS.en)) assert.equal(vars(STRINGS[lang][k]), vars(STRINGS.en[k]), `${lang}.${k}`);
  }
});

test("Chinese and Vietnamese are real translations, not English copies", () => {
  for (const lang of ["zh", "vi"]) {
    const words = (s) => s.replace(/\{\w+\}/g, "");
    const same = Object.keys(STRINGS.en).filter((k) => STRINGS[lang][k] === STRINGS.en[k] && /[a-z]{3}/.test(words(STRINGS.en[k])));
    assert.deepEqual(same.filter((k) => k !== "appName"), [], `${lang} left in English`);
  }
  setLang("zh");
  assert.equal(T("queued", { n: 2 }), "2 条等待同步");
  setLang("vi");
  assert.equal(T("resultMatch"), "Đúng hàng");
});

test("T substitutes every occurrence and falls back", () => {
  setLang("es");
  assert.equal(T("queued", { n: 3 }), "3 por sincronizar");
  assert.equal(T("no_such_key"), "no_such_key");
  setLang("xx");
  assert.equal(T("offline"), "Offline");
});
