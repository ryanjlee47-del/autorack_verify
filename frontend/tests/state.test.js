import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applyServerState, checkLabel, classify, corrections, displayLines, isOrderCode, lastUndoable, nextLine,
  orderIdFromCode, progress, remaining, shippedTracking, sortForWalking, toWire,
} from "../w/js/state.js";

// A cached order as the API ships it (index rows computed by the server).
function order(overrides = {}) {
  return {
    id: "o1",
    version: 3,
    status: "in_progress",
    lines: [
      { id: "a", line_no: 1, expected_barcode: "025300000208", expected_quantity: 2, scanned_quantity: 0, location: "B-02" },
      { id: "b", line_no: 2, expected_barcode: "VND-1", expected_quantity: 1, scanned_quantity: 0, location: "A-01" },
    ],
    match: {
      loose_match_enabled: false,
      suffix_len: 8,
      disabled_keys: {},
      index: [
        { line_id: "a", tier: 0, key: "025300000208" },
        { line_id: "a", tier: 1, key: "025300000208" },
        { line_id: "a", tier: 2, key: "00025300000208" },
        { line_id: "a", tier: 3, key: "25300000208" },
        { line_id: "a", tier: 4, key: "0002530000020" },
        { line_id: "b", tier: 0, key: "VND-1" },
        { line_id: "b", tier: 1, key: "VND-1" },
      ],
    },
    ...overrides,
  };
}

test("classify: equivalent formats match, unknown is mismatch", () => {
  const o = order();
  assert.deepEqual(classify(o, o.lines, "02532038"), { result: "match", lineId: "a", tier: 2 });
  assert.equal(classify(o, o.lines, " vnd-1 ").result, "match");
  assert.equal(classify(o, o.lines, "nope").result, "mismatch");
  assert.equal(classify(o, o.lines, "constructor").result, "mismatch");
});

test("classify: full line is an over-pick", () => {
  const o = order();
  const lines = o.lines.map((l) => (l.id === "b" ? { ...l, scanned_quantity: 1 } : l));
  assert.equal(classify(o, lines, "VND-1").result, "over_pick");
});

test("classify: ambiguous goes to review", () => {
  const o = order();
  o.match.index.push({ line_id: "b", tier: 2, key: "00025300000208" }, { line_id: "b", tier: 4, key: "0002530000020" });
  assert.equal(classify({ ...o }, o.lines, "(01)00025300000208").result, "review");
});

test("displayLines replays the queue on top of the server baseline", () => {
  const o = order();
  const pending = [
    { order_id: "o1", kind: "scan", client_seq: 1, local: { result: "match", lineId: "a" } },
    { order_id: "o1", kind: "scan", client_seq: 2, local: { result: "mismatch", lineId: null } },
    { order_id: "o1", kind: "scan", client_seq: 3, local: { result: "match", lineId: "a" } },
    { order_id: "o1", kind: "void", client_seq: 4, local: { lineId: "a" } },
    { order_id: "other", kind: "scan", client_seq: 5, local: { result: "match", lineId: "b" } },
  ];
  const lines = displayLines(o, pending);
  assert.equal(lines.find((l) => l.id === "a").scanned_quantity, 1);
  assert.equal(lines.find((l) => l.id === "b").scanned_quantity, 0);
  assert.deepEqual(progress(lines), { done: 1, total: 3, short: 0, complete: false });
});

test("nextLine walks by location, honours an unfinished choice", () => {
  const o = order();
  assert.equal(nextLine(o.lines, null).id, "b"); // A-01 before B-02
  assert.equal(nextLine(o.lines, "a").id, "a");
  const done = o.lines.map((l) => (l.id === "b" ? { ...l, scanned_quantity: 1 } : l));
  assert.equal(nextLine(done, "b").id, "a");
  assert.deepEqual(sortForWalking([{ line_no: 2 }, { location: "Z", line_no: 1 }]).map((l) => l.line_no), [1, 2]);
});

test("applyServerState updates quantities and detects structural change", () => {
  const o = order();
  const same = applyServerState(o, { status: "in_progress", version: 3, open_flags: 0, lines: { a: 2, b: 0 } });
  assert.equal(same.order.lines[0].scanned_quantity, 2);
  assert.equal(same.needsRefetch, false);
  const grown = applyServerState(o, { status: "in_progress", version: 5, open_flags: 0, lines: { a: 0, b: 0, c: 0 } });
  assert.equal(grown.needsRefetch, true);
});

test("corrections describe what the worker must physically fix", () => {
  const events = [{ id: "e1", kind: "scan", scanned_barcode: "VND-1", local: { result: "match", lineId: "b" } }];
  const lines = new Map([["b", { id: "b", description: "Tape", expected_barcode: "VND-1" }]]);
  const out = corrections(events, [{ id: "e1", status: "applied", result: "over_pick", line_item_id: "b" }], lines);
  assert.deepEqual(out, [{ kind: "warn", key: "correctionOverPick", vars: { item: "Tape", code: "VND-1" } }]);
  assert.deepEqual(corrections(events, [{ id: "e1", status: "applied", result: "match" }], lines), []);
  assert.deepEqual(corrections(events, [{ id: "e1", status: "duplicate", result: "mismatch" }], lines), []);
});

test("lastUndoable skips voided and non-matching scans", () => {
  const history = [
    { id: "s1", orderId: "o1", kind: "scan", result: "match", lineId: "a" },
    { id: "s2", orderId: "o1", kind: "scan", result: "match", lineId: "a" },
    { id: "s3", orderId: "o1", kind: "scan", result: "mismatch" },
    { id: "v1", orderId: "o1", kind: "void", target: "s2" },
  ];
  assert.equal(lastUndoable(history, "o1").id, "s1");
  assert.equal(lastUndoable(history, "o2"), null);
});

test("toWire drops local bookkeeping", () => {
  const w = toWire({ id: "x", kind: "scan", local: { result: "match" }, note: null, scanned_barcode: "1" });
  assert.deepEqual(w, { id: "x", kind: "scan", scanned_barcode: "1" });
});

test("order QR codes", () => {
  const id = "3f2c1b9a-1234-4abc-9def-0123456789ab";
  assert.ok(isOrderCode(` autorack:order:${id}`));
  assert.equal(orderIdFromCode(`AUTORACK:ORDER:${id.toUpperCase()}`), id);
  assert.equal(orderIdFromCode("AUTORACK:ORDER:nope"), null);
  assert.equal(orderIdFromCode("012345678905"), null);
});


test("short picks count as accounted for, and block over-scanning", () => {
  const o = order();
  const pending = [
    { id: "s1", kind: "scan", order_id: "o1", client_seq: 1, local: { result: "match", lineId: "a" } },
    { id: "x1", kind: "short", order_id: "o1", client_seq: 2, line_item_id: "a", quantity: 5, local: { lineId: "a" } },
  ];
  const lines = displayLines(o, pending);
  const a = lines.find((l) => l.id === "a");
  assert.equal(a.scanned_quantity, 1);
  assert.equal(a.short_quantity, 1); // capped at what was left
  assert.equal(remaining(a), 0);
  assert.equal(classify(o, lines, "025300000208").result, "over_pick");
  assert.equal(nextLine(lines, "a").id, "b");
  const p = progress(lines);
  assert.deepEqual(p, { done: 2, total: 3, short: 1, complete: false });
});

test("server short quantities replace the baseline", () => {
  const o = order();
  const { order: updated } = applyServerState(o, {
    status: "flagged", version: 3, open_flags: 1, lines: { a: 1, b: 1 }, short: { a: 1 }, tracking_number: null,
  });
  assert.equal(updated.lines[0].short_quantity, 1);
  assert.equal(updated.lines[1].short_quantity, 0);
  assert.ok(progress(updated.lines).complete);
});

test("shipping labels: product barcodes and junk are refused", () => {
  const o = order();
  assert.deepEqual(checkLabel(o, "02532038"), { ok: false, reason: "product" });
  assert.deepEqual(checkLabel(o, "123"), { ok: false, reason: "short" });
  assert.deepEqual(checkLabel(o, "1z 999 aa1-0123456784"), { ok: true, tracking: "1Z999AA10123456784" });
});

test("shipped tracking comes from the queue or the server", () => {
  const o = order();
  assert.equal(shippedTracking(o, []), null);
  assert.equal(shippedTracking(o, [{ order_id: "o1", kind: "ship", tracking_number: "1Z9" }]), "1Z9");
  assert.equal(shippedTracking({ ...o, status: "shipped", tracking_number: "940" }, []), "940");
});

test("short and ship events keep their wire fields", () => {
  const w = toWire({
    id: "e", kind: "short", line_item_id: "a", quantity: 2, short_reason: "damaged", tracking_number: "T",
    local: { lineId: "a" },
  });
  assert.deepEqual(Object.keys(w).sort(), ["id", "kind", "line_item_id", "quantity", "short_reason", "tracking_number"]);
});

// ---------------------------------------------------------------------------
// Receiving, returns, counts
// ---------------------------------------------------------------------------

test("tally jobs count past expected and record unknown items as extras", async () => {
  const { isTally, tallySummary, extrasFor } = await import("../w/js/state.js");
  const o = order({ kind: "receive" });
  assert.equal(isTally(o), true);
  assert.equal(isTally(order()), false);
  const full = o.lines.map((l) => ({ ...l, scanned_quantity: l.expected_quantity }));
  assert.deepEqual(classify(o, full, "025300000208"), { result: "counted", lineId: "a", tier: 0 });
  assert.equal(classify(o, full, "999999999993").result, "extra");
  // The same scans on a pick are an over-pick and a mismatch.
  assert.equal(classify(order(), full, "025300000208").result, "over_pick");
  assert.equal(classify(order(), full, "999999999993").result, "mismatch");

  const pending = [
    { id: "s1", kind: "scan", order_id: "o1", client_seq: 1, local: { result: "counted", lineId: "a" } },
    { id: "s2", kind: "scan", order_id: "o1", client_seq: 2, local: { result: "counted", lineId: "a" } },
    { id: "s3", kind: "scan", order_id: "o1", client_seq: 3, local: { result: "counted", lineId: "a" } },
    { id: "s4", kind: "scan", order_id: "o1", client_seq: 4, local: { result: "extra", lineId: null } },
  ];
  const lines = displayLines(o, pending);
  assert.equal(lines.find((l) => l.id === "a").scanned_quantity, 3);

  const history = [
    { id: "s3", orderId: "o1", kind: "scan", result: "counted", lineId: "a" },
    { id: "s4", orderId: "o1", kind: "scan", result: "extra", lineId: null },
  ];
  assert.equal(lastUndoable(history, "o1").id, "s4");
  assert.equal(extrasFor(history, "o1"), 1);
  assert.equal(extrasFor([...history, { id: "v", kind: "void", target: "s4" }], "o1"), 0);
  assert.deepEqual(tallySummary(lines, 1), { counted: 3, over: 1, short: 1, extras: 1, matches: false });
});

test("lot, serial and expiry come from GS1 barcodes and are checked", async () => {
  const { gs1Date, unitDetails, neededDetails, traceProblem } = await import("../w/js/state.js");
  assert.equal(gs1Date("301231"), "2030-12-31");
  assert.equal(gs1Date("280200"), "2028-02-29");
  assert.equal(gs1Date("300229"), null);
  assert.deepEqual(unitDetails("0109501101530003172512311" + "0LOT-7"), { lot: "LOT-7", serial: null, expiry: "2025-12-31" });
  assert.deepEqual(unitDetails("(01)09501101530003(21)SN9"), { lot: null, serial: "SN9", expiry: null });
  assert.deepEqual(unitDetails("09501101530003"), { lot: null, serial: null, expiry: null });

  const line = { id: "a", track_lot: true, track_serial: false, track_expiry: true, required_lot: null };
  assert.deepEqual(neededDetails(line, { lot: null, serial: null, expiry: null }), ["lot", "expiry"]);
  assert.deepEqual(neededDetails(line, { lot: "L", serial: null, expiry: "2030-01-01" }), []);
  assert.deepEqual(neededDetails({ id: "b", required_lot: "A1" }, { lot: null }), ["lot"]);

  const today = "2026-09-27";
  assert.equal(traceProblem({ required_lot: "A100" }, { lot: "b200" }, [], today), "wrong_lot");
  assert.equal(traceProblem({ required_lot: "A100" }, { lot: "a100" }, [], today), null);
  assert.equal(traceProblem({}, { expiry: "2026-09-26" }, [], today), "expired");
  assert.equal(traceProblem({}, { expiry: "2026-09-27" }, [], today), null);
  const serialLine = { id: "s", track_serial: true };
  const hist = [{ id: "x", kind: "scan", result: "match", lineId: "s", serial: "SN1" }];
  assert.equal(traceProblem(serialLine, { serial: "SN1" }, hist, today), "serial_repeat");
  assert.equal(traceProblem(serialLine, { serial: "SN1" }, [...hist, { kind: "void", target: "x" }], today), null);
});

test("case barcodes count their pack size and substitutes are marked", async () => {
  const { scanExtras } = await import("../w/js/state.js");
  const o = order();
  o.match = {
    ...o.match,
    index: [...o.match.index, { line_id: "a", tier: 5, key: "10025300000205" }, { line_id: "b", tier: 5, key: "SUB-9" }],
    packs: { "10025300000205": 12 },
    subs: { "SUB-9": "Widget, new box" },
  };
  o.lines[0].expected_quantity = 13;
  assert.deepEqual(scanExtras(o, "10025300000205"), { qty: 12 });
  const first = classify(o, o.lines, "10025300000205");
  assert.deepEqual(first, { result: "match", lineId: "a", tier: 5, qty: 12 });
  const pending = [{ id: "c1", kind: "scan", order_id: "o1", client_seq: 1, local: { result: "match", lineId: "a", qty: 12 } }];
  const lines = displayLines(o, pending);
  assert.equal(lines[0].scanned_quantity, 12);
  // Only 1 left: a whole case is an over-pick.
  assert.equal(classify(o, lines, "10025300000205").result, "over_pick");
  const undo = [...pending, { id: "v", kind: "void", order_id: "o1", client_seq: 2, local: { lineId: "a", qty: 12 } }];
  assert.equal(displayLines(o, undo)[0].scanned_quantity, 0);
  assert.deepEqual(classify(o, o.lines, "SUB-9"), { result: "match", lineId: "b", tier: 5, sub: "Widget, new box" });
});
