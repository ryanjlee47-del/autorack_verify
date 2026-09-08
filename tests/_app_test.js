// Node-only tests for static/js/worker/app.js's scan-classification path.
//
// app.js is a bare IIFE with no exports, so rather than add a test hook to
// production code this stubs enough of the DOM to load the real file and
// drive it the way a wedge scanner does -- a keydown on the hidden wedge
// input. Everything under test is therefore the actual shipped logic, the
// same principle as tests/_outbox_test.js and tests/_parity_harness.js.
//
// What it covers, and why those two things:
//
//   A3b -- a match that resolves to a manifest line the bundle does not
//          contain must never present as a green OK. The prototype-chain
//          bug produced exactly that: resolved=true with
//          manifestLineId=undefined, linesById[undefined] missing, and the
//          scan classified "ok" because sessionScannedLineIds[undefined]
//          was falsy. An item on no manifest was waved through the dock.
//
//   A4  -- a bundle that fails to build must be fatal and visible. It used
//          to leave matchIndex null, and handleDecoded returned early on
//          every scan after that: no beep, no flash, nothing written to the
//          outbox, for the rest of the shift.
const assert = require("assert");
const path = require("path");

const WORKER = path.join(__dirname, "..", "static", "js", "worker");

// --- minimal DOM ----------------------------------------------------------

function makeElement(id) {
  const el = {
    id,
    _listeners: {},
    style: { display: "none" },
    classList: { toggle() {}, add() {}, remove() {} },
    className: "",
    textContent: "",
    value: "",
    disabled: false,
    tagName: "DIV",
    children: [],
    files: null,
    placeholder: "",
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener() {},
    dispatchEvent(type, event) {
      (this._listeners[type] || []).forEach((fn) => fn(event || {}));
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    focus() {
      global.document.activeElement = this;
    },
    querySelector() {
      return makeElement(null);
    },
    click() {
      this.dispatchEvent("click");
    },
  };
  Object.defineProperty(el, "innerHTML", {
    get() {
      return this._innerHTML || "";
    },
    set(v) {
      // Any innerHTML write is a finding in its own right (B5) -- record it
      // so a test can assert none happened.
      global.__innerHTMLWrites.push({ id: this.id, value: v });
      this._innerHTML = v;
    },
  });
  return el;
}

function buildDom() {
  const els = {};
  global.__innerHTMLWrites = [];
  const ids = [
    "bundle-info", "result-overlay", "result-label", "result-detail", "result-sku",
    "dismiss-button", "appeal-open-button", "wedge-input", "torch-toggle",
    "camera-toggle", "camera-on-icon", "camera-off-icon", "camera-off-panel",
    "camera-off-title", "camera-off-hint", "camera-countdown", "logout-button",
    "camera-video", "decode-canvas", "manual-entry-toggle", "manual-entry-modal",
    "manual-entry-title", "manual-entry-input", "manual-entry-cancel",
    "manual-entry-submit", "appeal-modal", "appeal-modal-title",
    "appeal-instructions", "appeal-photo-input", "appeal-photo-preview",
    "appeal-take-photo-button", "appeal-note-input", "appeal-cancel-button",
    "appeal-submit-button", "summary-overlay", "summary-title", "summary-stats",
    "summary-done-button",
  ];
  ids.forEach((id) => (els[id] = makeElement(id)));

  const body = makeElement("body");
  body.getAttribute = (name) =>
    ({ "data-session-id": "sess-token-1", "data-worker-self-resolve": "false", "data-lang": "en" }[name]);

  global.document = {
    body,
    activeElement: body,
    getElementById: (id) => els[id] || null,
    createElement: (tag) => {
      const el = makeElement(null);
      el.tagName = tag.toUpperCase();
      return el;
    },
    addEventListener() {},
    removeEventListener() {},
  };
  return els;
}

// --- environment ----------------------------------------------------------

function loadApp(bundle, opts) {
  opts = opts || {};
  const els = buildDom();
  const stored = [];
  global.__stored = stored;
  global.__feedback = [];

  global.navigator = { onLine: false, vibrate() {} }; // offline: use the cached bundle only
  global.performance = { now: () => 0 };
  global.setTimeout = (fn, ms) => 0; // no timers fire during a test
  global.setInterval = () => 0;
  global.clearTimeout = () => {};
  global.clearInterval = () => {};
  global.fetch = () => Promise.reject(new Error("offline"));
  global.alert = () => {};
  global.confirm = () => true;
  global.crypto = require("crypto").webcrypto || require("crypto");

  const idb = {
    metaGet: (key) => Promise.resolve(key === "bundle" ? bundle : 0),
    metaSet: () => Promise.resolve(),
    outboxAdd: (scan) => {
      if (opts.failOutboxWrite) return Promise.reject(new Error("quota exceeded"));
      stored.push(scan);
      return Promise.resolve();
    },
    outboxAll: () => Promise.resolve(stored.slice()),
    outboxRemoveMany: () => Promise.resolve(),
    outboxCount: () => Promise.resolve(stored.length),
    appealAdd: () => Promise.resolve(),
    appealAll: () => Promise.resolve([]),
    appealRemove: () => Promise.resolve(),
  };

  global.window = {
    AutorackIDB: idb,
    Barcode: require(path.join(__dirname, "..", "static", "js", "barcode.js")),
    AutorackI18n: require(path.join(WORKER, "i18n.js")),
    AutorackFeedback: {
      unlockAudio() {},
      play(kind) {
        global.__feedback.push(kind);
      },
    },
    AutorackScanner: function () {
      return { start: () => Promise.resolve(), stop() {}, hasTorch: () => false };
    },
    addEventListener() {},
    location: { href: "", reload() {} },
  };
  global.window.AutorackOutbox = require(path.join(WORKER, "outbox.js"));
  global.window.AutorackAppealQueue = require(path.join(WORKER, "appeals.js"));
  global.window.window = global.window;

  delete require.cache[require.resolve(path.join(WORKER, "app.js"))];
  require(path.join(WORKER, "app.js"));
  return els;
}

function wedgeScan(els, payload) {
  els["wedge-input"].value = payload;
  els["wedge-input"].dispatchEvent("keydown", { key: "Enter", preventDefault() {} });
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

// --- tests ----------------------------------------------------------------

const BUNDLE = {
  shift: { id: 1, label: "S", date: "2026-09-07", bundleVersion: 3 },
  lines: [{ id: 10, manifestId: 1, sku: "REAL-SKU", description: "Real item", qtyExpected: 1 }],
  keys: [{ manifestLineId: 10, tier: 0, key: "REALCODE" }],
  disabledKeys: {},
  settings: { looseMatchEnabled: false, looseSuffixLen: 8, workerSelfResolve: false },
};

async function testResolvedMatchWithMissingLineIsNotOk() {
  // A bundle whose index points at line 99 while `lines` only describes 10.
  // That is precisely the shape the prototype-chain bug manufactured, and
  // the shape a corrupted or partially-applied bundle produces.
  const bundle = JSON.parse(JSON.stringify(BUNDLE));
  bundle.keys.push({ manifestLineId: 99, tier: 0, key: "GHOSTCODE" });

  const els = loadApp(bundle);
  await flush();
  wedgeScan(els, "GHOSTCODE");
  await flush();
  await flush();

  assert.strictEqual(global.__stored.length, 1, "the scan must still be recorded");
  const scan = global.__stored[0];
  assert.strictEqual(
    scan.result,
    "unresolved",
    "a match pointing at a line the bundle lacks must not be classified ok, got: " + scan.result
  );
  assert.ok(
    global.__feedback.indexOf("ok") === -1,
    "the worker must never see a green OK for a line that is not on the manifest"
  );
  assert.strictEqual(global.__feedback[0], "reject");
  console.log("ok  A3b: a resolved match with no manifest line is not an OK");
}

async function testRealMatchStillWorks() {
  const els = loadApp(JSON.parse(JSON.stringify(BUNDLE)));
  await flush();
  wedgeScan(els, "REALCODE");
  await flush();
  await flush();

  assert.strictEqual(global.__stored.length, 1);
  assert.strictEqual(global.__stored[0].result, "ok");
  assert.strictEqual(global.__stored[0].manifestLineId, 10);
  assert.strictEqual(global.__feedback[0], "ok");
  console.log("ok  control: a genuine match still reports OK");
}

async function testDuplicateRespectsQtyExpected() {
  const bundle = JSON.parse(JSON.stringify(BUNDLE));
  bundle.lines[0].qtyExpected = 2;
  const els = loadApp(bundle);
  await flush();
  for (let i = 0; i < 3; i++) {
    wedgeScan(els, "REALCODE");
    await flush();
    await flush();
  }
  const results = global.__stored.map((s) => s.result);
  assert.deepStrictEqual(
    results,
    ["ok", "ok", "duplicate"],
    "qtyExpected 2 means two units are OK and the third is the duplicate, got: " + results
  );
  console.log("ok  E2: qtyExpected units are not reported as duplicates");
}

async function testFailedBundleIsFatalAndVisible() {
  // `keys` is not an array -- buildIndex cannot produce an index from it.
  const bundle = JSON.parse(JSON.stringify(BUNDLE));
  bundle.keys = "not-an-array";

  const els = loadApp(bundle);
  await flush();
  wedgeScan(els, "REALCODE");
  await flush();
  await flush();

  assert.strictEqual(global.__stored.length, 0, "nothing can be recorded without an index");
  assert.ok(
    global.__feedback.length > 0,
    "a phone with no index must not fail silently -- it used to look powered on and do nothing"
  );
  assert.strictEqual(global.__feedback[0], "reject");
  assert.ok(
    els["result-label"].textContent.length > 0,
    "the failure must be shown on screen"
  );
  console.log("ok  A4: a bundle that will not build is fatal and visible");
}

async function testOutboxWriteFailureIsNotAGreenFlash() {
  const els = loadApp(JSON.parse(JSON.stringify(BUNDLE)), { failOutboxWrite: true });
  await flush();
  wedgeScan(els, "REALCODE");
  await flush();
  await flush();

  assert.strictEqual(global.__stored.length, 0);
  assert.ok(
    global.__feedback.indexOf("ok") === -1,
    "a scan that never reached storage must not flash green -- outbox.js's docstring " +
      "promises the write happens before anything else, and nothing awaited it"
  );
  console.log("ok  D1: a failed outbox write is reported, not flashed green");
}

async function testHeaderChipNeverUsesInnerHtml() {
  loadApp(JSON.parse(JSON.stringify(BUNDLE)));
  await flush();
  assert.deepStrictEqual(
    global.__innerHTMLWrites,
    [],
    "bundle.shift.date reaches this element from owner form input (B5): " +
      JSON.stringify(global.__innerHTMLWrites)
  );
  console.log("ok  B5: the header chip is built without innerHTML");
}

const TESTS = [
  testResolvedMatchWithMissingLineIsNotOk,
  testRealMatchStillWorks,
  testDuplicateRespectsQtyExpected,
  testFailedBundleIsFatalAndVisible,
  testOutboxWriteFailureIsNotAGreenFlash,
  testHeaderChipNeverUsesInnerHtml,
];

async function main() {
  // Every test runs even when an earlier one fails, so a regression report
  // names all of them rather than only the first.
  let failed = 0;
  for (const test of TESTS) {
    try {
      await test();
    } catch (err) {
      failed += 1;
      console.error("FAIL " + test.name + ": " + (err && err.message ? err.message : err));
    }
  }
  if (failed) {
    console.error(failed + " app.js test(s) failed");
    process.exit(1);
  }
  console.log("all app.js tests passed");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
