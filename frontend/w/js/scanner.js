// Camera scanning: the native BarcodeDetector where it exists (Android
// Chrome), the vendored ZXing decoder (window.ZXing) everywhere else. Both
// run through one throttled loop and one ScanGate (scangate.js), which
// decides which reads count: only inside the on-screen box, confirmed on
// two frames, and never the same item twice without it leaving the box.
// Video never leaves the phone: only the decoded text does.

import { ScanGate, aimRect, pickAimed } from "./scangate.js";

var DECODE_INTERVAL_MS = 100; // ~10 attempts/sec on the native fast path

// The ZXing fallback decodes less often, deliberately.
// MultiFormatReader.decode() signals "no barcode in this frame" by
// THROWING NotFoundException, and the vendored exception base extends
// Error and calls Error.captureStackTrace -- so the miss path costs a
// full exception construction plus stack capture, on every frame without
// a barcode, which is nearly all of them. ZXing only decodes the aim box
// (about a third of the frame), so ~7 Hz costs less than the old 5 Hz on
// the whole frame did.
var ZXING_DECODE_INTERVAL_MS = 140;
// Half the ZXing frames take the quick pass; the others try harder,
// alternately TRY_HARDER (more rows, for faded or creased labels) and the
// box turned 90 degrees, for a barcode standing on end. The bundle's own
// rotation under TRY_HARDER doesn't find rotated barcodes, so we turn the
// pixels ourselves. Both are too slow for every frame on an older iPhone.
var ZXING_PASSES = ["quick", "turned", "quick", "hard"];
// A barcode only the slower passes can read is seen every 4 frames, so
// ZXing confirms over a longer window than the native detector.
var ZXING_CONFIRM_WINDOW_MS = 1000;
var TARGET_WIDTH = 1280;
var TARGET_HEIGHT = 720;

var RELEVANT_FORMATS_NATIVE = [
  "code_128", "ean_13", "ean_8", "upc_a", "upc_e", "code_39", "codabar", "itf", "qr_code",
];

function zxingFormats() {
  var Z = window.ZXing;
  return [
    Z.BarcodeFormat.CODE_128, Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8,
    Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E, Z.BarcodeFormat.CODE_39,
    Z.BarcodeFormat.CODABAR, Z.BarcodeFormat.ITF, Z.BarcodeFormat.QR_CODE,
  ];
}

export function Scanner(videoEl, canvasEl, onDecode) {
  this.video = videoEl;
  this.canvas = canvasEl;
  this.onDecode = onDecode;
  this.stream = null;
  this.videoTrack = null;
  this.usingNative = "BarcodeDetector" in window;
  this.detector = null;
  this.zxingReader = null;
  this.zxingHardReader = null;
  this.zxingTicks = 0;
  this.turnedCanvas = null;
  this.gate = new ScanGate({ confirmWindowMs: this.usingNative ? undefined : ZXING_CONFIRM_WINDOW_MS });
  this.intervalHandle = null;
  this.torchOn = false;
  this.running = false;
  this.stopped = false; // stop() called, possibly before start() resolved
  this.decodeInFlight = false;
  this.canvasCtx = null;
  this.paused = false;
}

Scanner.prototype.start = function () {
  var self = this;
  var constraints = {
    audio: false,
    video: {
      facingMode: { ideal: "environment" },
      width: { ideal: TARGET_WIDTH, max: TARGET_WIDTH },
      height: { ideal: TARGET_HEIGHT, max: TARGET_HEIGHT },
    },
  };
  return navigator.mediaDevices.getUserMedia(constraints).then(function (stream) {
    // stop() may have been called while getUserMedia was still pending --
    // toggling the camera button on then off is enough. Without this
    // check the stream arrives after stop() has already run, nothing ever
    // stops its tracks, and this Scanner instance keeps a live
    // MediaStream and its own setInterval forever, invisible to every
    // later turnCameraOff() (which only holds the newest scanner). The
    // camera indicator stays lit, which is exactly what the hard
    // 15-second window exists to prevent.
    if (self.stopped) {
      stream.getTracks().forEach(function (t) {
        t.stop();
      });
      throw new Error("scanner stopped before start completed");
    }
    self.stream = stream;
    self.video.srcObject = stream;
    self.videoTrack = stream.getVideoTracks()[0];
    return self.video.play();
  }).then(function () {
    if (self.stopped) {
      self._teardownStream();
      throw new Error("scanner stopped before start completed");
    }
    if (self.usingNative) {
      self.detector = new window.BarcodeDetector({ formats: RELEVANT_FORMATS_NATIVE });
    } else {
      var hints = new Map();
      hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, zxingFormats());
      hints.set(window.ZXing.DecodeHintType.TRY_HARDER, false);
      self.zxingReader = new window.ZXing.MultiFormatReader();
      self.zxingReader.setHints(hints);
      var hard = new Map(hints);
      hard.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
      self.zxingHardReader = new window.ZXing.MultiFormatReader();
      self.zxingHardReader.setHints(hard);
    }
    self._focus();
    self.running = true;
    self.intervalHandle = setInterval(function () {
      self._tick();
    }, self.usingNative ? DECODE_INTERVAL_MS : ZXING_DECODE_INTERVAL_MS);
  });
};

Scanner.prototype._teardownStream = function () {
  if (this.intervalHandle) {
    clearInterval(this.intervalHandle);
    this.intervalHandle = null;
  }
  if (this.stream) {
    this.stream.getTracks().forEach(function (t) {
      t.stop();
    });
    this.stream = null;
  }
  this.videoTrack = null;
};

Scanner.prototype.stop = function () {
  this.stopped = true;
  this.running = false;
  this._teardownStream();
};

Scanner.prototype._tick = function () {
  if (!this.running || this.video.readyState < 2) return;
  // detect() is async and can take longer than the tick interval on a
  // loaded phone. Without this guard the ticks overlap and detections
  // accumulate; the 1200ms debounce hid that as "mostly fine" rather
  // than preventing it.
  if (this.decodeInFlight) return;
  var start = performance.now();
  var text = null;
  try {
    if (this.usingNative) {
      this.decodeInFlight = true;
      text = this._decodeNativeSync();
      if (text && text.then) {
        // BarcodeDetector.detect() is always async; handle promise path.
        var self = this;
        text.then(function (results) {
          self.decodeInFlight = false;
          if (!self.running) return;
          self._handleNativeResults(results, start);
        }).catch(function () {
          self.decodeInFlight = false;
        });
        return;
      }
      this.decodeInFlight = false;
    } else {
      text = this._decodeZxing();
      this._handleDecodedText(text, start);
    }
  } catch (e) {
    // No barcode found this frame -- expected most ticks, not an error.
    this.decodeInFlight = false;
  }
};

Scanner.prototype._decodeNativeSync = function () {
  // detect() always returns a Promise; kept as its own method for clarity.
  return this.detector.detect(this.video);
};

// Phones that can refocus on their own don't always start out doing it;
// a camera fixed at arm's length can't read a label held close.
Scanner.prototype._focus = function () {
  var track = this.videoTrack;
  if (!track || !track.getCapabilities || !track.applyConstraints) return;
  var caps = track.getCapabilities();
  if (caps.focusMode && caps.focusMode.indexOf("continuous") >= 0) {
    track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(function () {});
  }
};

Scanner.prototype._aim = function () {
  return aimRect(this.video.videoWidth, this.video.videoHeight, this.video.clientWidth, this.video.clientHeight);
};

Scanner.prototype._handleNativeResults = function (results, startedAt) {
  var hit = pickAimed(results, this._aim());
  if (!hit) return;
  var decodeMs = performance.now() - startedAt;
  this._handleDecodedText(hit.rawValue, startedAt, decodeMs);
};

Scanner.prototype._decodeZxing = function () {
  var rect = this._aim();
  if (!rect || !rect.w || !rect.h) return null;
  // Only the aim box goes to the decoder: barcodes elsewhere on the shelf
  // can't be read, and fewer pixels decode faster.
  if (this.canvas.width !== rect.w || this.canvas.height !== rect.h) {
    this.canvas.width = rect.w;
    this.canvas.height = rect.h;
  }
  // getContext("2d") is cheap but not free, and this runs every frame for
  // the whole shift. Cache it; the canvas element never changes.
  if (!this.canvasCtx) this.canvasCtx = this.canvas.getContext("2d", { willReadFrequently: true });
  this.canvasCtx.drawImage(this.video, rect.x, rect.y, rect.w, rect.h, 0, 0, rect.w, rect.h);
  var pass = ZXING_PASSES[this.zxingTicks++ % ZXING_PASSES.length];
  var source = pass === "turned" ? this._turned(rect) : this.canvas;
  var reader = pass === "hard" ? this.zxingHardReader : this.zxingReader;
  var luminanceSource = new window.ZXing.HTMLCanvasElementLuminanceSource(source);
  var binaryBitmap = new window.ZXing.BinaryBitmap(new window.ZXing.HybridBinarizer(luminanceSource));
  // The bundled QR readers throw a "not found" type MultiFormatReader
  // doesn't recognise, so it console.warn()s the error, stack and all, on
  // nearly every empty frame. Silence it for just this call.
  var warn = console.warn;
  console.warn = function () {};
  try {
    var result = reader.decode(binaryBitmap);
    return result ? result.getText() : null;
  } finally {
    console.warn = warn;
  }
};

Scanner.prototype._turned = function (rect) {
  if (!this.turnedCanvas) this.turnedCanvas = document.createElement("canvas");
  var c = this.turnedCanvas;
  if (c.width !== rect.h || c.height !== rect.w) {
    c.width = rect.h;
    c.height = rect.w;
  }
  var ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.setTransform(0, 1, -1, 0, rect.h, 0); // 90 degrees clockwise
  ctx.drawImage(this.canvas, 0, 0);
  return c;
};

Scanner.prototype._handleDecodedText = function (text, startedAt, decodeMsOverride) {
  if (!text) return;
  var decodeMs = decodeMsOverride != null ? decodeMsOverride : performance.now() - startedAt;
  // Reads keep counting while paused: an item still in the box when the
  // result closes must not scan again.
  var fire = this.gate.see(text, Date.now(), this.paused);
  if (fire) this.onDecode(fire, decodeMs);
};

// While a result is on screen the item is usually still in front of the
// lens; decoding it again would stack a second result on the first.
Scanner.prototype.pause = function () {
  this.paused = true;
};

Scanner.prototype.resume = function () {
  this.paused = false;
  this.gate.reset();
};

Scanner.prototype.hasTorch = function () {
  if (!this.videoTrack || !this.videoTrack.getCapabilities) return false;
  var caps = this.videoTrack.getCapabilities();
  return !!caps.torch;
};

Scanner.prototype.toggleTorch = function () {
  var self = this;
  if (!this.hasTorch()) return Promise.resolve(false);
  this.torchOn = !this.torchOn;
  return this.videoTrack.applyConstraints({ advanced: [{ torch: this.torchOn }] }).then(function () {
    return self.torchOn;
  });
};
