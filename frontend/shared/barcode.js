// Barcode normalization and 7-tier matching -- the JavaScript twin of
// backend/autorack/matching.py.
//
// The phone runs this offline to tell a worker "right item / wrong item"
// instantly; the server re-runs matching.py on sync and is authoritative.
// Both must produce byte-identical keys for the same payload, or the same
// physical label resolves differently depending on which side computed it.
// backend/tests/test_js_parity.py runs both over an adversarial corpus.
//
// FNC1 parity trap: never use String.prototype.trim() here (and never
// str.strip() in Python). Python treats \x1c-\x1f as whitespace, JS does
// not. Both sides strip one explicit character set instead.

// NUL, FS/GS/RS/US (0x1c-0x1f), and standard ASCII whitespace.
export const CONTROL_CHARS = [
  0x00, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x09, 0x0a, 0x0d, 0x0b, 0x0c,
];
var CONTROL_SET = {};
CONTROL_CHARS.forEach(function (c) {
  CONTROL_SET[c] = true;
});

// Only NUL + plain whitespace are edge-trimmed before GS1 parsing, so
// embedded GS (0x1d) separators used by variable-length AI fields survive.
var EDGE_TRIM_SET = { 0x00: true, 0x20: true, 0x09: true, 0x0a: true, 0x0d: true, 0x0b: true, 0x0c: true };

var GS_SEP = "\x1d";

export function stripControlChars(s) {
  var out = "";
  for (var i = 0; i < s.length; i++) {
    var code = s.charCodeAt(i);
    if (!CONTROL_SET[code]) {
      out += s[i];
    }
  }
  return out;
}

export function edgeTrimPreserveGs(s) {
  var start = 0;
  var end = s.length;
  while (start < end && EDGE_TRIM_SET[s.charCodeAt(start)]) start++;
  while (end > start && EDGE_TRIM_SET[s.charCodeAt(end - 1)]) end--;
  return s.slice(start, end);
}

export function toUpperAscii(s) {
  var upper = s.toUpperCase();
  var out = "";
  for (var i = 0; i < upper.length; i++) {
    if (upper.charCodeAt(i) < 128) out += upper[i];
  }
  return out;
}

// ---------------------------------------------------------------------
// GS1 application identifiers
// ---------------------------------------------------------------------
var FIXED_AI_LENGTHS = {
  "00": 18,
  "01": 14,
  "02": 14,
  "11": 6,
  "12": 6,
  "13": 6,
  "15": 6,
  "17": 6,
  "20": 2,
  "410": 13,
  "411": 13,
  "412": 13,
  "413": 13,
  "414": 13,
  "415": 13,
  "422": 3,
};
var VARIABLE_AIS = { "10": true, "21": true, "30": true };
var THREE_DIGIT_AI_PREFIXES = { "41": true, "42": true };
var AI_FIELD_NAMES = {
  "00": "sscc",
  "01": "gtin",
  "02": "gtin",
  "10": "lot",
  "21": "serial",
  "30": "qty",
};

// Every lookup into an object used as a map goes through this. A bare
// `table[key]` finds Object.prototype members -- "constructor", "toString",
// "valueOf", "hasOwnProperty" -- for keys that are not in the table at all.
// A barcode payload is attacker-influenced text, so those keys are reachable
// from the dock. See buildIndex/matchAgainstIndex for what that cost.
function has(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function isDigits(s) {
  if (s.length === 0) return false;
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i);
    if (c < 48 || c > 57) return false;
  }
  return true;
}

function looksLikeGs1(s) {
  if (!s) return false;
  if (s[0] === "(") return true;
  if (s[0] === GS_SEP) return true;
  var two = s.slice(0, 2);
  return has(FIXED_AI_LENGTHS, two) || has(VARIABLE_AIS, two);
}

export function parseGs1(raw) {
  var working = edgeTrimPreserveGs(raw);
  if (!working || !looksLikeGs1(working)) return null;

  var bracketed = working[0] === "(";
  if (working[0] === GS_SEP) working = working.slice(1);

  var fields = { gtin: null, lot: null, serial: null, qty: null, sscc: null, extra: {} };
  var pos = 0;
  var n = working.length;
  var foundAny = false;

  while (pos < n) {
    var ch = working[pos];
    var ai;
    if (ch === GS_SEP) {
      pos += 1;
      continue;
    }
    if (ch === "(") {
      var end = working.indexOf(")", pos);
      if (end === -1) break;
      ai = working.slice(pos + 1, end);
      pos = end + 1;
    } else {
      var two = working.slice(pos, pos + 2);
      if (has(THREE_DIGIT_AI_PREFIXES, two)) {
        ai = working.slice(pos, pos + 3);
        pos += 3;
      } else {
        ai = two;
        pos += 2;
      }
      if (!isDigits(ai)) break;
    }

    var value;
    if (has(FIXED_AI_LENGTHS, ai)) {
      var length = FIXED_AI_LENGTHS[ai];
      value = working.slice(pos, pos + length);
      if (value.length < length) break;
      pos += length;
    } else if (has(VARIABLE_AIS, ai)) {
      var stop;
      if (bracketed) {
        var nextParen = working.indexOf("(", pos);
        stop = nextParen === -1 ? n : nextParen;
      } else {
        var nextGs = working.indexOf(GS_SEP, pos);
        stop = nextGs === -1 ? n : nextGs;
      }
      value = working.slice(pos, stop);
      pos = stop;
    } else {
      break;
    }

    foundAny = true;
    var fieldName = AI_FIELD_NAMES[ai];
    if (fieldName === "gtin") fields.gtin = value;
    else if (fieldName === "lot") fields.lot = value;
    else if (fieldName === "serial") fields.serial = value;
    else if (fieldName === "qty") fields.qty = value;
    else if (fieldName === "sscc") fields.sscc = value;
    else fields.extra[ai] = value;
  }

  return foundAny ? fields : null;
}

// ---------------------------------------------------------------------
// GTIN canonicalization
// ---------------------------------------------------------------------
export function upceBodyToUpcaBody(upceBody) {
  if (upceBody.length !== 7 || !isDigits(upceBody)) {
    throw new Error("UPC-E body must be exactly 7 digits (check digit excluded)");
  }
  var n = upceBody[0], s1 = upceBody[1], s2 = upceBody[2], s3 = upceBody[3],
    s4 = upceBody[4], s5 = upceBody[5], s6 = upceBody[6];
  if (s6 === "0" || s6 === "1" || s6 === "2") {
    return n + s1 + s2 + s6 + "0000" + s3 + s4 + s5;
  } else if (s6 === "3") {
    return n + s1 + s2 + s3 + "00000" + s4 + s5;
  } else if (s6 === "4") {
    return n + s1 + s2 + s3 + s4 + "00000" + s5;
  }
  return n + s1 + s2 + s3 + s4 + s5 + "0000" + s6;
}

export function upceToUpca(upce) {
  if (upce.length !== 8 || !isDigits(upce)) {
    throw new Error("UPC-E input must be exactly 8 digits");
  }
  return upceBodyToUpcaBody(upce.slice(0, 7)) + upce[7];
}

export function computeCheckDigit(body) {
  var total = 0;
  var i = 0;
  for (var pos = body.length - 1; pos >= 0; pos--, i++) {
    var weight = i % 2 === 0 ? 3 : 1;
    total += parseInt(body[pos], 10) * weight;
  }
  return String((10 - (total % 10)) % 10);
}

export function gtinCanonicalize(digits) {
  if (!isDigits(digits)) return null;
  var n = digits.length;
  var upce = null, upca = null, ean13 = null, gtin14 = null;
  if (n === 8) {
    upce = digits;
    upca = upceToUpca(digits);
    ean13 = "0" + upca;
    gtin14 = "00" + upca;
  } else if (n === 12) {
    upca = digits;
    ean13 = "0" + upca;
    gtin14 = "00" + upca;
  } else if (n === 13) {
    ean13 = digits;
    gtin14 = "0" + ean13;
    upca = ean13[0] === "0" ? ean13.slice(1) : null;
  } else if (n === 14) {
    gtin14 = digits;
    ean13 = gtin14[0] === "0" ? gtin14.slice(1) : null;
    upca = ean13 && ean13[0] === "0" ? ean13.slice(1) : null;
  } else {
    return null;
  }
  var body = gtin14.slice(0, -1);
  var check = gtin14.slice(-1);
  var computed = computeCheckDigit(body);
  return {
    gtin14: gtin14,
    ean13: ean13,
    upca: upca,
    upce: upce,
    checkValid: computed === check,
    bodyNoCheck: body,
  };
}

// ---------------------------------------------------------------------
// Tiers -- must match matching.py's Tier IntEnum values exactly.
// ---------------------------------------------------------------------
export const Tier = Object.freeze({
  RAW: 0,
  NORMALIZED: 1,
  GTIN14: 2,
  DIGITS_STRIPPED: 3,
  BODY_NO_CHECK: 4,
  ALIAS: 5,
  SUFFIX: 6,
});

// ALIAS must stay grouped with the other certain-confidence tiers
// (RAW/NORMALIZED/GTIN14), before the high-confidence loose-numeric
// tiers -- an owner-taught alias must not be silently outranked by an
// automatic guess. This array must stay byte-for-byte identical to
// matching.py's TIER_ORDER -- see the comment there and
// backend/tests/test_js_parity.py.
export const TIER_ORDER = [
  Tier.RAW,
  Tier.NORMALIZED,
  Tier.GTIN14,
  Tier.ALIAS,
  Tier.DIGITS_STRIPPED,
  Tier.BODY_NO_CHECK,
  Tier.SUFFIX,
];

export const DEFAULT_SUFFIX_LEN = 8;
export const MIN_SUFFIX_LEN = 6;

// Mirrors matching.py's CONFIRMATION_REQUIRED_TIERS. Transcribed as a
// condition (`tier === Tier.SUFFIX`) it was invisible to the parity test
// that covers TIER_ORDER; as a named constant it is covered.
export const CONFIRMATION_REQUIRED_TIERS = [Tier.SUFFIX];

export function normalize(raw, suffixLen) {
  if (suffixLen === undefined || suffixLen === null) suffixLen = DEFAULT_SUFFIX_LEN;
  if (suffixLen < MIN_SUFFIX_LEN) suffixLen = MIN_SUFFIX_LEN;

  var gs1 = parseGs1(raw);
  var normalized = toUpperAscii(stripControlChars(raw));

  var gtinSource = null;
  if (gs1 && gs1.gtin) {
    gtinSource = stripControlChars(gs1.gtin);
  } else if (isDigits(normalized) && [8, 12, 13, 14].indexOf(normalized.length) !== -1) {
    gtinSource = normalized;
  }

  var gtinInfo = gtinSource ? gtinCanonicalize(gtinSource) : null;

  var keys = {};
  keys[Tier.RAW] = raw;
  keys[Tier.NORMALIZED] = normalized;

  if (gtinInfo) {
    keys[Tier.GTIN14] = gtinInfo.gtin14;
    keys[Tier.BODY_NO_CHECK] = gtinInfo.bodyNoCheck;
  } else if (isDigits(normalized) && (normalized.length === 7 || normalized.length === 11)) {
    // Unambiguous "check digit omitted" body -- see matching.py's
    // normalize() for why only 7/11 (not 12/13) are safe to assume, and
    // why the key must be the "00"-prefixed canonical body.
    var upcaBody = normalized.length === 7 ? upceBodyToUpcaBody(normalized) : normalized;
    keys[Tier.BODY_NO_CHECK] = "00" + upcaBody;
  }

  var digitsOnly = "";
  for (var i = 0; i < normalized.length; i++) {
    var c = normalized[i];
    if (c >= "0" && c <= "9") digitsOnly += c;
  }
  if (isDigits(normalized) && digitsOnly) {
    var strippedZeros = digitsOnly.replace(/^0+/, "") || "0";
    keys[Tier.DIGITS_STRIPPED] = strippedZeros;
  }

  if (digitsOnly && digitsOnly.length >= suffixLen) {
    keys[Tier.SUFFIX] = digitsOnly.slice(-suffixLen);
  }

  return { raw: raw, normalized: normalized, gs1: gs1, gtinInfo: gtinInfo, keys: keys };
}

// ---------------------------------------------------------------------
// Matching against the index the server ships with each order:
//   rows: [{line_id, tier, key}, ...]   disabledKeys: {tier: [key, ...]}
// Every map is Object.create(null) and every lookup goes through has():
// a barcode reading "constructor" must not find Object.prototype members.
// ---------------------------------------------------------------------
export function buildIndex(rows) {
  const index = Object.create(null);
  for (const t of TIER_ORDER) index[t] = Object.create(null);
  for (const row of rows) {
    const byKey = index[row.tier];
    if (byKey === undefined) continue;
    if (!has(byKey, row.key)) byKey[row.key] = [];
    if (byKey[row.key].indexOf(row.line_id) === -1) byKey[row.key].push(row.line_id);
  }
  return index;
}

export function matchAgainstIndex(index, rawPayload, options = {}) {
  const looseMatchEnabled = !!options.looseMatchEnabled;
  // Explicit null test, not ||: suffixLen 0 must clamp like Python's max().
  let suffixLen = options.suffixLen === undefined || options.suffixLen === null ? DEFAULT_SUFFIX_LEN : options.suffixLen;
  if (suffixLen < MIN_SUFFIX_LEN) suffixLen = MIN_SUFFIX_LEN;
  const disabledKeys = options.disabledKeys || {};

  const norm = normalize(rawPayload, suffixLen);
  const candidatesByTier = {};
  let ambiguous = false;

  for (const tier of TIER_ORDER) {
    if (tier === Tier.SUFFIX && !looseMatchEnabled) continue;
    const key = tier === Tier.ALIAS ? norm.normalized : norm.keys[tier];
    if (key === undefined || key === null) {
      candidatesByTier[tier] = [];
      continue;
    }
    const byKey = index[tier];
    const disabledForTier = has(disabledKeys, tier) ? disabledKeys[tier] : null;
    if (Array.isArray(disabledForTier) && disabledForTier.indexOf(key) !== -1) {
      if (byKey && has(byKey, key) && Array.isArray(byKey[key]) && byKey[key].length) ambiguous = true;
      candidatesByTier[tier] = [];
      continue;
    }
    let hits = byKey && has(byKey, key) ? byKey[key] : [];
    if (!Array.isArray(hits)) hits = [];
    const sorted = hits.slice().sort();
    candidatesByTier[tier] = sorted;
    if (sorted.length === 1 && sorted[0] !== undefined && sorted[0] !== null) {
      return {
        resolved: true,
        tier,
        lineId: sorted[0],
        candidatesByTier,
        needsConfirmation: CONFIRMATION_REQUIRED_TIERS.indexOf(tier) !== -1,
        ambiguous,
      };
    }
    if (sorted.length > 1) ambiguous = true;
  }
  return { resolved: false, tier: null, lineId: null, candidatesByTier, needsConfirmation: false, ambiguous };
}

export function normalizedKey(raw) {
  return toUpperAscii(stripControlChars(raw));
}
