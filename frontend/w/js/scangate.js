// Which camera reads count as a scan. Pure logic, no DOM, so it is tested
// on its own (tests/scangate.test.js); scanner.js feeds it detections.
//
// Three rules, each fixing something workers hit on a real shelf:
//
// 1. Aim box. Only a barcode inside the on-screen box counts. The camera
//    sees the whole shelf; reading whichever barcode the decoder found
//    first meant the bin next door scanned while the worker was still
//    aiming ("wrong, wrong, wrong, right").
// 2. Confirmation. The same code must be read on two frames close together
//    before it fires. Sweeping the phone across a shelf passes barcodes
//    through the box for a single frame; those no longer fire.
// 3. Leave the box before scanning again. Once a code fires it cannot fire
//    again until it has been out of the box for GONE_MS. Showing a result
//    used to reset this, so an item still in front of the lens scanned a
//    second time the moment the result closed. Counting multiple units of
//    the same item still works: take one away, bring the next.

export var CONFIRM_WINDOW_MS = 700; // two reads of the same code within this (default)
export var GONE_MS = 900; // out of the box this long before it can fire again

// The .camera-frame box, as fractions of the visible video (worker.css:
// inset 22% 10%). AIM_SLACK widens it a little: a code half over the line
// is still what the worker is pointing at.
export var AIM = { left: 0.10, right: 0.90, top: 0.22, bottom: 0.78 };
var AIM_SLACK = 0.06;

/** The aim box in video pixels, for a video shown with object-fit: cover.
 *  viewW/viewH are the element's on-screen size; when it has none (not laid
 *  out yet) the whole frame counts. */
export function aimRect(videoW, videoH, viewW, viewH) {
  if (!videoW || !videoH) return null;
  if (!viewW || !viewH) return { x: 0, y: 0, w: videoW, h: videoH };
  var scale = Math.max(viewW / videoW, viewH / videoH);
  var visW = viewW / scale;
  var visH = viewH / scale;
  var offX = (videoW - visW) / 2;
  var offY = (videoH - visH) / 2;
  var x0 = offX + visW * Math.max(0, AIM.left - AIM_SLACK);
  var x1 = offX + visW * Math.min(1, AIM.right + AIM_SLACK);
  var y0 = offY + visH * Math.max(0, AIM.top - AIM_SLACK);
  var y1 = offY + visH * Math.min(1, AIM.bottom + AIM_SLACK);
  return { x: Math.round(x0), y: Math.round(y0), w: Math.round(x1 - x0), h: Math.round(y1 - y0) };
}

/** From BarcodeDetector results, the one the worker is aiming at: its centre
 *  inside the aim box, nearest the middle. Null when none is. */
export function pickAimed(results, rect) {
  if (!results || !results.length) return null;
  if (!rect) return results[0];
  var cx = rect.x + rect.w / 2;
  var cy = rect.y + rect.h / 2;
  var best = null;
  var bestD = Infinity;
  for (var i = 0; i < results.length; i++) {
    var r = results[i];
    var b = r.boundingBox;
    if (!b) continue;
    var x = b.x + b.width / 2;
    var y = b.y + b.height / 2;
    if (x < rect.x || x > rect.x + rect.w || y < rect.y || y > rect.y + rect.h) continue;
    var d = (x - cx) * (x - cx) + (y - cy) * (y - cy);
    if (d < bestD) {
      bestD = d;
      best = r;
    }
  }
  return best;
}

/** Decides when a stream of per-frame reads becomes a scan. */
export function ScanGate(opts) {
  opts = opts || {};
  this.confirmations = opts.confirmations || 2;
  this.confirmWindowMs = opts.confirmWindowMs || CONFIRM_WINDOW_MS;
  this.fired = null; // { text, lastSeenAt }
  this.candidate = null; // { text, hits, lastAt }
}

/** One frame's read (text, or null for nothing in the box). Returns the text
 *  when it should fire as a scan, else null. While paused (a result is on
 *  screen) reads still count as "seen" but never fire. */
ScanGate.prototype.see = function (text, now, paused) {
  if (!text) return null;
  var fired = this.fired;
  if (fired && fired.text === text) {
    if (now - fired.lastSeenAt < GONE_MS) {
      fired.lastSeenAt = now;
      this.candidate = null;
      return null; // still in the box since it last fired
    }
    this.fired = null; // it left and came back: a new unit, confirmed like any other
  }
  if (paused) {
    this.candidate = null;
    return null;
  }
  var c = this.candidate;
  if (c && c.text === text && now - c.lastAt <= this.confirmWindowMs) {
    c.hits += 1;
    c.lastAt = now;
  } else {
    c = this.candidate = { text: text, hits: 1, lastAt: now };
  }
  if (c.hits < this.confirmations) return null;
  this.candidate = null;
  this.fired = { text: text, lastSeenAt: now };
  return text;
};

/** A result closed: start confirming afresh, but keep remembering what
 *  last fired, so an item still in the box doesn't fire again. */
ScanGate.prototype.reset = function () {
  this.candidate = null;
};
