// The dashboard in English, Spanish, Chinese and Vietnamese.
//
// Keys are the English text itself: T("Orders"), T("{n} lines", { n }).
// A string with no translation shows in English, so nothing ever renders
// blank. tests/dashboard-i18n.test.js checks every T("...") in the dashboard
// has a translation in every language, with the same placeholders.
//
// The language is read once when the page loads; changing it reloads the
// page, so labels built at import time (tab names, column headers) follow.

import { setLocale } from "../../shared/dom.js";
import { ES } from "./locales/es.js";
import { VI } from "./locales/vi.js";
import { ZH } from "./locales/zh.js";

const KEY = "ar.dash_lang";
export const LANGUAGES = [["en", "English"], ["es", "Español"], ["zh", "中文"], ["vi", "Tiếng Việt"]];
const DICTS = { es: ES, zh: ZH, vi: VI };
const HTML_LANG = { zh: "zh-Hans" };
const INTL = { en: undefined, es: "es", zh: "zh-CN", vi: "vi-VN" };

function initial() {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved && LANGUAGES.some(([c]) => c === saved)) return saved;
  } catch {
    /* storage blocked */
  }
  const nav = (typeof navigator !== "undefined" && navigator.language || "en").toLowerCase().slice(0, 2);
  return DICTS[nav] ? nav : "en";
}

let lang = initial();
if (typeof document !== "undefined") document.documentElement.lang = HTML_LANG[lang] || lang;

export function getLang() {
  return lang;
}

export function setLang(code) {
  if (!LANGUAGES.some(([c]) => c === code)) return;
  try {
    localStorage.setItem(KEY, code);
  } catch {
    /* this page load only */
  }
  lang = code;
  location.reload();
}

export function T(text, vars) {
  const dict = DICTS[lang];
  let out = (dict && dict[text]) || text;
  if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m));
  return out;
}

// Dates, numbers and "5 min ago" in the same language.
if (lang !== "en") setLocale(INTL[lang], T);
