import assert from "node:assert/strict";
import { test } from "node:test";

import { GONE_MS, ScanGate, aimRect, pickAimed } from "../w/js/scangate.js";

/** Feed reads every `step` ms; returns what fired, with times. */
function run(gate, reads, step = 100, start = 0) {
  const fired = [];
  reads.forEach((text, i) => {
    const t = start + i * step;
    const out = gate.see(text, t, false);
    if (out) fired.push([out, t]);
  });
  return fired;
}

test("a code fires once it is read on two frames", () => {
  const g = new ScanGate();
  assert.deepEqual(run(g, ["A", "A"]), [["A", 100]]);
});

test("sweeping across a shelf fires nothing", () => {
  const g = new ScanGate();
  assert.deepEqual(run(g, ["A", "B", "C", "D", null, "E"]), []);
});

test("sweeping then settling fires only the code the worker stops on", () => {
  const g = new ScanGate();
  const fired = run(g, ["A", "B", "C", "D", "D", "D", "D"]);
  assert.deepEqual(fired.map((f) => f[0]), ["D"]);
});

test("a missed frame in between still confirms", () => {
  const g = new ScanGate();
  assert.deepEqual(run(g, ["A", null, "A"], 150), [["A", 300]]);
});

test("an item held in the box scans once, however long it stays", () => {
  const g = new ScanGate();
  const fired = run(g, Array(60).fill("A"));
  assert.equal(fired.length, 1);
});

test("the next unit of the same item scans after the first leaves the box", () => {
  const g = new ScanGate();
  const gap = Array(Math.ceil(GONE_MS / 100) + 1).fill(null);
  const fired = run(g, ["A", "A", "A", ...gap, "A", "A"]);
  assert.equal(fired.length, 2);
});

test("a brief dropout doesn't count as leaving", () => {
  const g = new ScanGate();
  const fired = run(g, ["A", "A", null, null, null, "A", "A", "A"]);
  assert.equal(fired.length, 1);
});

test("an item still in the box when a result closes doesn't scan again", () => {
  const g = new ScanGate();
  assert.equal(g.see("A", 0, false), null);
  assert.equal(g.see("A", 100, false), "A");
  // Result on screen for 2 seconds; the wrong item never moves.
  for (let t = 200; t < 2200; t += 100) assert.equal(g.see("A", t, true), null);
  g.reset();
  for (let t = 2200; t < 4000; t += 100) assert.equal(g.see("A", t, false), null);
  // Moving to the right item scans it straight away.
  assert.equal(g.see("B", 4000, false), null);
  assert.equal(g.see("B", 4100, false), "B");
});

test("nothing fires while paused", () => {
  const g = new ScanGate();
  for (let t = 0; t < 1000; t += 100) assert.equal(g.see("A", t, true), null);
  assert.equal(g.see("A", 1000, false), null);
  assert.equal(g.see("A", 1100, false), "A");
});

test("aim box follows object-fit: cover", () => {
  // Portrait 720x1280 video in a 400x300 element: only the middle band shows.
  const r = aimRect(720, 1280, 400, 300);
  const visH = 300 * (720 / 400); // 540
  const offY = (1280 - visH) / 2;
  assert.ok(r.y > offY && r.y + r.h < offY + visH);
  assert.ok(r.x >= 0 && r.x + r.w <= 720);
  // Not laid out yet: the whole frame.
  assert.deepEqual(aimRect(720, 1280, 0, 0), { x: 0, y: 0, w: 720, h: 1280 });
  assert.equal(aimRect(0, 0, 400, 300), null);
});

test("of several barcodes in view, the one in the box nearest the middle wins", () => {
  const rect = { x: 100, y: 100, w: 400, h: 200 };
  const box = (x, y) => ({ x: x - 50, y: y - 10, width: 100, height: 20 });
  const results = [
    { rawValue: "shelf-above", boundingBox: box(300, 40) },
    { rawValue: "edge", boundingBox: box(140, 200) },
    { rawValue: "aimed", boundingBox: box(310, 205) },
  ];
  assert.equal(pickAimed(results, rect).rawValue, "aimed");
  assert.equal(pickAimed([results[0]], rect), null);
  assert.equal(pickAimed([], rect), null);
});
