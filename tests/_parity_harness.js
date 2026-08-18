// Test-only harness: reads a JSON array of raw strings from stdin, runs
// each through the JS normalization pipeline, writes a JSON array of
// results to stdout. Used exclusively by tests/test_hash_parity.py to
// compare against barcode.py's output -- not part of the app.
const path = require("path");
const Barcode = require(path.join(__dirname, "..", "static", "js", "barcode.js"));

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const rawList = JSON.parse(input);
  const results = rawList.map((raw) => {
    const stripped = Barcode.stripControlChars(raw);
    const norm = Barcode.normalize(raw);
    const keys = {};
    Object.keys(norm.keys).forEach((tier) => {
      keys[tier] = norm.keys[tier];
    });
    return {
      stripped: stripped,
      normalized: norm.normalized,
      keys: keys,
    };
  });
  process.stdout.write(JSON.stringify(results));
});
