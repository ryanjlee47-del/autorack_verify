// Node-only test for static/js/worker/outbox.js's session-grouping fix.
// Mocks window.AutorackIDB, navigator.onLine, and fetch so the module's
// real logic (not a reimplementation of it) runs under plain Node, the
// same pattern tests/_parity_harness.js uses for barcode.js.
const assert = require("assert");
const path = require("path");

let storedOutbox = [];
let removedCalls = [];
let postedRequests = [];

global.window = {
  AutorackIDB: {
    metaGet: () => Promise.resolve(0),
    metaSet: () => Promise.resolve(),
    outboxAdd: (scan) => {
      storedOutbox.push(scan);
      return Promise.resolve();
    },
    outboxAll: () => Promise.resolve(storedOutbox.slice()),
    outboxRemoveMany: (uuids) => {
      removedCalls.push(uuids);
      storedOutbox = storedOutbox.filter((s) => uuids.indexOf(s.uuid) === -1);
      return Promise.resolve();
    },
    outboxCount: () => Promise.resolve(storedOutbox.length),
  },
};
global.navigator = { onLine: true };
global.fetch = (url, opts) => {
  const body = JSON.parse(opts.body);
  postedRequests.push(body);
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ accepted: body.scans.map((s) => s.uuid), serverTime: "x", bundleVersion: 0 }),
  });
};

const Outbox = require(path.join(__dirname, "..", "static", "js", "worker", "outbox.js"));

async function main() {
  // Simulate: worker A scans twice under session 1, logs out, worker B
  // (or the same worker rejoining) starts session 2 and scans once,
  // *then* the outbox drains everything -- items from session 1 must
  // still be posted under sessionId 1, not silently reattributed to 2.
  const outboxA = new Outbox(1);
  await outboxA.init();
  await outboxA.add({ uuid: "scan-a1", rawPayload: "X" });
  await outboxA.add({ uuid: "scan-a2", rawPayload: "Y" });

  const outboxB = new Outbox(2);
  await outboxB.init();
  await outboxB.add({ uuid: "scan-b1", rawPayload: "Z" });

  await outboxB.syncOnce();

  assert.strictEqual(postedRequests.length, 2, "expected two separate POSTs, one per session group");
  const bySession = {};
  postedRequests.forEach((req) => {
    bySession[req.sessionId] = req.scans.map((s) => s.uuid).sort();
  });
  assert.deepStrictEqual(bySession[1], ["scan-a1", "scan-a2"], "session 1's scans must post under sessionId 1");
  assert.deepStrictEqual(bySession[2], ["scan-b1"], "session 2's scan must post under sessionId 2");
  assert.strictEqual(storedOutbox.length, 0, "all scans should have been removed after successful sync");

  console.log("outbox session-grouping test passed");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
