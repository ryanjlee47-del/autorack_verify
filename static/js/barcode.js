// Barcode normalization engine -- JavaScript mirror of barcode.py.
//
// This file MUST stay byte-for-byte behaviorally identical to barcode.py's
// normalization pipeline. It runs on the phone, offline, and computes the
// same index keys the server computed when it built the shift bundle. If
// the two sides ever disagree, a scan that should match silently doesn't --
// a phantom reject, which is a billable event you did not earn.
//
// FNC1 parity trap: do not use String.prototype.trim() here, and the
// Python side must never use str.strip(). Python's str.isspace() (and
// therefore str.strip()) treats \x1c-\x1f (FS/GS/RS/US) as whitespace;
// JS's .trim() does not touch those code points at all. Relying on the
// two languages' built-ins would strip different characters on each side.
// Instead both sides define the exact same character set below and strip
// it explicitly, from anywhere in the string -- see tests/test_hash_parity.py.
//
// No build step: plain script, attaches `Barcode` to `window` in the
// browser and exports the same object via `module.exports` under Node
// (used only by the parity test harness).
(function (root) {
  "use strict";

  // NUL, FS/GS/RS/US (0x1c-0x1f), and standard ASCII whitespace.
  var CONTROL_CHARS = [
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

  function stripControlChars(s) {
    var out = "";
    for (var i = 0; i < s.length; i++) {
      var code = s.charCodeAt(i);
      if (!CONTROL_SET[code]) {
        out += s[i];
      }
    }
    return out;
  }

  function edgeTrimPreserveGs(s) {
    var start = 0;
    var end = s.length;
    while (start < end && EDGE_TRIM_SET[s.charCodeAt(start)]) start++;
    while (end > start && EDGE_TRIM_SET[s.charCodeAt(end - 1)]) end--;
    return s.slice(start, end);
  }

  function toUpperAscii(s) {
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

  function parseGs1(raw) {
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
  function upceBodyToUpcaBody(upceBody) {
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

  function upceToUpca(upce) {
    if (upce.length !== 8 || !isDigits(upce)) {
      throw new Error("UPC-E input must be exactly 8 digits");
    }
    return upceBodyToUpcaBody(upce.slice(0, 7)) + upce[7];
  }

  function computeCheckDigit(body) {
    var total = 0;
    var i = 0;
    for (var pos = body.length - 1; pos >= 0; pos--, i++) {
      var weight = i % 2 === 0 ? 3 : 1;
      total += parseInt(body[pos], 10) * weight;
    }
    return String((10 - (total % 10)) % 10);
  }

  function gtinCanonicalize(digits) {
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
  // Tiers -- must match barcode.py's Tier IntEnum values exactly.
  // ---------------------------------------------------------------------
  var Tier = {
    RAW: 0,
    NORMALIZED: 1,
    GTIN14: 2,
    DIGITS_STRIPPED: 3,
    BODY_NO_CHECK: 4,
    ALIAS: 5,
    SUFFIX: 6,
  };

  // ALIAS must stay grouped with the other certain-confidence tiers
  // (RAW/NORMALIZED/GTIN14), before the high-confidence loose-numeric
  // tiers -- an owner-taught alias must not be silently outranked by an
  // automatic guess. This array must stay byte-for-byte identical to
  // barcode.py's TIER_ORDER -- see the comment there and
  // tests/test_hash_parity.py::test_tier_order_matches_javascript.
  var TIER_ORDER = [
    Tier.RAW,
    Tier.NORMALIZED,
    Tier.GTIN14,
    Tier.ALIAS,
    Tier.DIGITS_STRIPPED,
    Tier.BODY_NO_CHECK,
    Tier.SUFFIX,
  ];

  var DEFAULT_SUFFIX_LEN = 8;
  var MIN_SUFFIX_LEN = 6;

  // Mirrors barcode.py's CONFIRMATION_REQUIRED_TIERS. Transcribed as a
  // condition (`tier === Tier.SUFFIX`) it was invisible to the parity test
  // that covers TIER_ORDER; as a named constant it is covered.
  var CONFIRMATION_REQUIRED_TIERS = [Tier.SUFFIX];

  function normalize(raw, suffixLen) {
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
      // Unambiguous "check digit omitted" body -- see barcode.py's
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
  // Matching against an in-memory bundle index.
  //
  // The index shape mirrors what the server ships in the bundle:
  //   { tier: { key: [manifestLineId, ...] } }
  // Aliases (tier 5) can be merged in client-side as they are learned,
  // same shape.
  // ---------------------------------------------------------------------
  function buildIndex(lineKeyRows) {
    // lineKeyRows: [{manifestLineId, tier, key}, ...]
    //
    // Every map here is Object.create(null), never {}. With a plain object
    // literal a manifest line whose barcode text is "constructor" made
    // `!byKey[row.key]` falsy (it found Function.prototype.constructor), so the
    // array was never created and .indexOf threw -- which propagated out of
    // applyBundle, left matchIndex null, and silently discarded every scan for
    // the rest of the shift. The mirror defect is in matchAgainstIndex.
    // Python's dict has no such inherited keys; this is what keeps the two
    // engines equivalent. tests/_parity_harness.js covers both directions.
    var index = Object.create(null);
    TIER_ORDER.forEach(function (t) {
      index[t] = Object.create(null);
    });
    lineKeyRows.forEach(function (row) {
      var byKey = index[row.tier];
      if (byKey === undefined) return;
      if (!has(byKey, row.key)) byKey[row.key] = [];
      if (byKey[row.key].indexOf(row.manifestLineId) === -1) {
        byKey[row.key].push(row.manifestLineId);
      }
    });
    return index;
  }

  function matchAgainstIndex(index, rawPayload, options) {
    options = options || {};
    var looseMatchEnabled = !!options.looseMatchEnabled;
    // Explicit null/undefined test, not `||`: suffixLen 0 must clamp to
    // MIN_SUFFIX_LEN the way barcode.py's max(suffix_len, MIN_SUFFIX_LEN)
    // does, not fall back to DEFAULT_SUFFIX_LEN. The two produced different
    // tier-6 keys, which is a phantom reject.
    var suffixLen = options.suffixLen === undefined || options.suffixLen === null
      ? DEFAULT_SUFFIX_LEN
      : options.suffixLen;
    if (suffixLen < MIN_SUFFIX_LEN) suffixLen = MIN_SUFFIX_LEN;
    var disabledKeys = options.disabledKeys || {};

    var norm = normalize(rawPayload, suffixLen);
    var candidatesByTier = {};

    for (var i = 0; i < TIER_ORDER.length; i++) {
      var tier = TIER_ORDER[i];
      if (tier === Tier.SUFFIX && !looseMatchEnabled) continue;
      var key = tier === Tier.ALIAS ? norm.normalized : norm.keys[tier];
      var hits = [];
      if (key !== undefined && key !== null) {
        // has(): disabledKeys arrives deserialized from the bundle JSON, so
        // it is a plain object literal and a bare lookup would find
        // Object.prototype members for prototype-named tiers.
        var disabledForTier = has(disabledKeys, tier) ? disabledKeys[tier] : null;
        var isDisabled = Array.isArray(disabledForTier) && disabledForTier.indexOf(key) !== -1;
        if (!isDisabled) {
          var byKey = index[tier];
          if (byKey && has(byKey, key)) hits = byKey[key];
          // An index built by buildIndex always yields an array here. A bundle
          // deserialized from anywhere else might not, and `hits.length === 1`
          // on a non-array is how a wrong item passes as OK.
          if (!Array.isArray(hits)) hits = [];
        }
      }
      candidatesByTier[tier] = hits;
      if (hits.length === 1 && hits[0] !== undefined && hits[0] !== null) {
        return {
          resolved: true,
          tier: tier,
          manifestLineId: hits[0],
          candidatesByTier: candidatesByTier,
          needsConfirmation: CONFIRMATION_REQUIRED_TIERS.indexOf(tier) !== -1,
        };
      }
    }

    return {
      resolved: false,
      tier: null,
      manifestLineId: null,
      candidatesByTier: candidatesByTier,
    };
  }

  var Barcode = {
    CONTROL_CHARS: CONTROL_CHARS,
    stripControlChars: stripControlChars,
    edgeTrimPreserveGs: edgeTrimPreserveGs,
    toUpperAscii: toUpperAscii,
    parseGs1: parseGs1,
    upceToUpca: upceToUpca,
    upceBodyToUpcaBody: upceBodyToUpcaBody,
    computeCheckDigit: computeCheckDigit,
    gtinCanonicalize: gtinCanonicalize,
    Tier: Tier,
    TIER_ORDER: TIER_ORDER,
    DEFAULT_SUFFIX_LEN: DEFAULT_SUFFIX_LEN,
    MIN_SUFFIX_LEN: MIN_SUFFIX_LEN,
    CONFIRMATION_REQUIRED_TIERS: CONFIRMATION_REQUIRED_TIERS,
    normalize: normalize,
    buildIndex: buildIndex,
    matchAgainstIndex: matchAgainstIndex,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Barcode;
  }
  if (root) {
    root.Barcode = Barcode;
  }
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : null);
