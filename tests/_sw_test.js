// Node-only test for static/js/worker/sw.js's fetch handler.
//
// Stubs the service worker globals (self, caches, fetch, Response) so the
// real handler runs under plain Node -- the same pattern
// tests/_outbox_test.js and tests/_parity_harness.js use.
//
// The bug under test: any respondWith() path that resolves to undefined
// makes Safari report "FetchEvent.respondWith received an error: Returned
// response is null", which a worker scanning a door QR sees as "Safari
// can't open the page". See sw.js's offlineFallback() comment.
const assert = require("assert");
const path = require("path");

// --- Minimal service worker environment -------------------------------

class FakeResponse {
  constructor(body, init) {
    this.body = body || "";
    init = init || {};
    this.status = init.status === undefined ? 200 : init.status;
    this.ok = this.status >= 200 && this.status < 300;
    this.headers = (init && init.headers) || {};
  }
  clone() {
    return new FakeResponse(this.body, { status: this.status, headers: this.headers });
  }
}
global.Response = FakeResponse;

let cacheStore = {};
let networkShouldFail = false;

global.caches = {
  match: (req) => Promise.resolve(cacheStore[req.url]),
  open: () =>
    Promise.resolve({
      put: (req, resp) => {
        cacheStore[req.url] = resp;
        return Promise.resolve();
      },
      addAll: () => Promise.resolve(),
    }),
  keys: () => Promise.resolve([]),
  delete: () => Promise.resolve(true),
};

global.fetch = () =>
  networkShouldFail
    ? Promise.reject(new Error("network down"))
    : Promise.resolve(new FakeResponse("<html>live</html>", { status: 200 }));

const listeners = {};
global.self = {
  addEventListener: (name, fn) => {
    listeners[name] = fn;
  },
  skipWaiting: () => Promise.resolve(),
  clients: { claim: () => Promise.resolve() },
};

require(path.join(__dirname, "..", "static", "js", "worker", "sw.js"));

// --- Harness ----------------------------------------------------------

// Returns whatever the handler passed to respondWith(), or the string
// "NOT_HANDLED" if it declined to intercept (which is a valid outcome --
// the browser then goes to the network itself).
function dispatchFetch(url, method) {
  let responded = "NOT_HANDLED";
  const event = {
    request: { url: url, method: method || "GET" },
    respondWith: (p) => {
      responded = p;
    },
  };
  listeners.fetch(event);
  return Promise.resolve(responded);
}

function assertRealResponse(resp, label) {
  assert.ok(
    resp !== undefined && resp !== null,
    label + ": respondWith() received " + resp + " -- Safari reports this as " +
      '"Returned response is null" / "Safari can\'t open the page"'
  );
  assert.ok(resp instanceof FakeResponse, label + ": expected a Response, got " + typeof resp);
}

async function main() {
  const ORIGIN = "https://192.168.1.50:8443";

  // ---- The reported bug: scan a door QR while the network is unusable.
  // A freshly-scanned token is never a cache hit (cache keys include the
  // query string), so miss + failure land together.
  cacheStore = {};
  networkShouldFail = true;
  let resp = await dispatchFetch(ORIGIN + "/w/join?t=BRANDNEWTOKEN");
  assertRealResponse(resp, "uncached /w/join with network down");
  assert.strictEqual(resp.status, 503, "offline join page should be a 503");
  assert.ok(/needs a connection/i.test(resp.body), "join fallback should explain the situation");

  // ---- Same shape on the scan page.
  resp = await dispatchFetch(ORIGIN + "/w/scan?sid=UNSEENSESSION");
  assertRealResponse(resp, "uncached /w/scan with network down");
  assert.ok(/queued on this device are safe/i.test(resp.body), "scan fallback should reassure about queued scans");

  // ---- Static asset, uncached, network down: must not be undefined.
  resp = await dispatchFetch(ORIGIN + "/static/js/worker/app.js");
  assertRealResponse(resp, "uncached static asset with network down");
  assert.strictEqual(resp.status, 503);

  // ---- The offline promise still holds: a scan page cached while online
  // must be served from cache when the network later dies. This is the
  // whole point of the service worker, so the fix must not break it.
  networkShouldFail = false;
  const scanUrl = ORIGIN + "/w/scan?sid=REALSESSION";
  resp = await dispatchFetch(scanUrl);
  assertRealResponse(resp, "online /w/scan");
  assert.strictEqual(resp.body, "<html>live</html>", "online should serve the network response");

  networkShouldFail = true;
  resp = await dispatchFetch(scanUrl);
  assertRealResponse(resp, "cached /w/scan with network down");
  assert.strictEqual(resp.body, "<html>live</html>", "offline should serve the cached page, not the fallback");
  assert.strictEqual(resp.status, 200, "a real cached page should not be a 503");

  // ---- A cached page must NOT be served for a different session token.
  // Doing so would attach this worker's scans to someone else's session,
  // which is why the fix is a fallback Response rather than ignoreSearch.
  resp = await dispatchFetch(ORIGIN + "/w/scan?sid=SOMEONEELSE");
  assertRealResponse(resp, "different sid with network down");
  assert.strictEqual(resp.status, 503, "must not serve another session's cached page");
  assert.notStrictEqual(resp.body, "<html>live</html>");

  // ---- Network-only routes stay untouched: caching a bundle or a sync
  // response would serve stale manifest data as if it were current.
  for (const p of ["/w/sync", "/w/bundle/TOKEN", "/w/heartbeat/TOKEN"]) {
    const r = await dispatchFetch(ORIGIN + p);
    assert.strictEqual(r, "NOT_HANDLED", p + " must be left to the network");
  }

  // ---- Non-GET is never intercepted (POSTs carry scan data).
  const post = await dispatchFetch(ORIGIN + "/w/appeal", "POST");
  assert.strictEqual(post, "NOT_HANDLED", "POST must be left to the network");

  console.log("sw.js fetch handler tests passed");
}

main().catch((err) => {
  console.error(err && err.message ? err.message : err);
  process.exit(1);
});
