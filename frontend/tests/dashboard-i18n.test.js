// Every T("...") in the dashboard has a translation in every language, with
// only placeholders the English has.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";

import { ES } from "../app/js/locales/es.js";
import { VI } from "../app/js/locales/vi.js";
import { ZH } from "../app/js/locales/zh.js";

const DIR = new URL("../app/js/", import.meta.url);
// Words the shared formatters and dialogs ask for (shared/dom.js).
const SHARED = ["never", "just now", "{n} min ago", "{n} h ago", "{n} d ago", "Close", "Confirm", "Cancel", "Language"];

function sources() {
  const files = [];
  for (const sub of ["", "views/"]) {
    for (const f of readdirSync(new URL(sub, DIR))) {
      if (f.endsWith(".js") && f !== "i18n.js") files.push(readFileSync(new URL(sub + f, DIR), "utf8"));
    }
  }
  return files;
}

function keys() {
  const found = new Set(SHARED);
  const re = /\bT\("((?:[^"\\]|\\.)*)"/g;
  for (const src of sources()) {
    for (const m of src.matchAll(re)) found.add(JSON.parse(`"${m[1]}"`));
  }
  return found;
}

const placeholders = (s) => new Set([...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]));

for (const [name, dict] of [["es", ES], ["zh", ZH], ["vi", VI]]) {
  test(`dashboard ${name}: every string is translated`, () => {
    const missing = [...keys()].filter((k) => !(k in dict));
    assert.deepEqual(missing, [], `${missing.length} strings have no ${name} translation`);
  });

  test(`dashboard ${name}: placeholders match`, () => {
    for (const [en, tr] of Object.entries(dict)) {
      const want = placeholders(en);
      for (const p of placeholders(tr)) assert.ok(want.has(p), `${name}: "${tr}" has {${p}} that "${en}" doesn't`);
      assert.ok(tr.trim().length > 0, `${name}: empty translation for "${en}"`);
    }
  });

  test(`dashboard ${name}: no leftover keys`, () => {
    const all = keys();
    const stale = Object.keys(dict).filter((k) => !all.has(k));
    assert.deepEqual(stale, [], `${name} has translations nothing uses`);
  });
}
