// Test-only harness for the index/lookup layer, the counterpart to
// _parity_harness.js (which covers normalize()). Reads a JSON job from
// stdin, runs it through the JS engine, writes a JSON result to stdout.
//
// This layer is where the two engines diverge most easily: Python uses a
// dict and a set, JavaScript uses object literals, and the differences --
// inherited Object.prototype members, a truthiness test standing in for a
// membership test -- do not show up anywhere in normalize()'s output. The
// 30,011-input differential fuzz over normalize() found zero divergences
// while a wrong item was passing as OK one layer up.
const path = require("path");
const Barcode = require(path.join(__dirname, "..", "static", "js", "barcode.js"));

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const job = JSON.parse(input);
  // job: {rows: [{manifestLineId, tier, key}], payloads: [str],
  //       looseMatchEnabled, suffixLen, disabledKeys}
  let index;
  let buildError = null;
  try {
    index = Barcode.buildIndex(job.rows);
  } catch (err) {
    buildError = String(err && err.message ? err.message : err);
  }

  const results = buildError
    ? null
    : job.payloads.map((raw) => {
        const m = Barcode.matchAgainstIndex(index, raw, {
          looseMatchEnabled: !!job.looseMatchEnabled,
          suffixLen: job.suffixLen,
          disabledKeys: job.disabledKeys || {},
        });
        return {
          resolved: !!m.resolved,
          tier: m.resolved ? m.tier : null,
          manifestLineId: m.resolved ? m.manifestLineId : null,
          needsConfirmation: !!m.needsConfirmation,
        };
      });

  process.stdout.write(
    JSON.stringify({
      buildError: buildError,
      results: results,
      confirmationRequiredTiers: Barcode.CONFIRMATION_REQUIRED_TIERS,
      minSuffixLen: Barcode.MIN_SUFFIX_LEN,
      defaultSuffixLen: Barcode.DEFAULT_SUFFIX_LEN,
      controlChars: Barcode.CONTROL_CHARS,
    })
  );
});
