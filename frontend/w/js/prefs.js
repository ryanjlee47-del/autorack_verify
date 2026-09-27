// Per-phone settings: how this phone scans, and how loud it is about it.
// Kept in localStorage: they belong to the device, not to whoever signs in.

const KEY = "ar.prefs";

// What each kind of scanner needs. A hardware scanner types the barcode as
// keystrokes; `gapMs` is the longest pause between two keystrokes of one scan
// (Bluetooth ring scanners are slower than a built-in scan engine).
export const SCANNERS = {
  camera: { wedge: true, gapMs: 150 },
  wedge: { wedge: true, gapMs: 150 },
  ring: { wedge: true, gapMs: 300 },
  rugged: { wedge: true, gapMs: 80 },
};

export const DEFAULTS = { scanner: "camera", sound: true, loud: false, vibrate: true, strongVibrate: false };

export function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || "null");
    return { ...DEFAULTS, ...(saved && typeof saved === "object" ? saved : {}) };
  } catch {
    return { ...DEFAULTS };
  }
}

export function savePrefs(prefs) {
  const clean = { ...DEFAULTS };
  for (const k of Object.keys(DEFAULTS)) if (k in prefs) clean[k] = prefs[k];
  if (!SCANNERS[clean.scanner]) clean.scanner = DEFAULTS.scanner;
  try {
    localStorage.setItem(KEY, JSON.stringify(clean));
  } catch {
    /* private mode: this page load only */
  }
  return clean;
}

export function usesHardwareScanner(prefs) {
  return prefs.scanner !== "camera";
}

export function keyGapMs(prefs) {
  return (SCANNERS[prefs.scanner] || SCANNERS.camera).gapMs;
}

// Scanners can be set to prefix an AIM symbology identifier ("]C1" for a
// GS1-128, "]E0" for EAN-13...). It says what kind of barcode it was, not
// what it says: drop it. A GS1 one becomes the FNC1 separator the GS1 parser
// understands.
const AIM = /^\][A-Za-z][0-9A-Za-z]/;
const AIM_GS1 = new Set(["]C1", "]e0", "]d2", "]Q3", "]J1"]);

export function cleanScan(raw) {
  const text = String(raw == null ? "" : raw).replace(/[\r\n]+$/, "");
  const m = AIM.exec(text);
  if (!m) return text;
  const rest = text.slice(3);
  return AIM_GS1.has(m[0]) && rest ? `\u001d${rest}` : rest;
}
