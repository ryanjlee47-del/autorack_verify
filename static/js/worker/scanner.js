// Camera scanning: native BarcodeDetector first (fast path on Android
// Chrome), vendored ZXing-JS fallback otherwise. Both paths run through
// the same manually-throttled tick loop so behavior (cadence, resolution,
// debounce) is identical regardless of which decoder is active --
// ZXing's own continuous-video loop is deliberately NOT used, so we keep
// full control over the ~10/sec budget instead of decoding as fast as
// the browser will let us (bad for battery, no accuracy benefit at
// reading distance).
(function (root) {
  "use strict";

  var DECODE_INTERVAL_MS = 100; // ~10 attempts/sec on the native fast path

  // The ZXing fallback decodes at half that rate, deliberately.
  // MultiFormatReader.decode() signals "no barcode in this frame" by
  // THROWING NotFoundException, and the vendored exception base extends
  // Error and calls Error.captureStackTrace -- so the miss path costs a
  // full exception construction plus stack capture, on every frame without
  // a barcode, which is nearly all of them. At 10 Hz that is ~10 exceptions
  // per second for the whole shift, on iOS Safari, which is already both
  // the slower decoder and the more battery-constrained platform. Removing
  // the throw means patching vendored ZXing; halving how often we pay for
  // it does not, and the 1200ms debounce means 5 Hz loses nothing at
  // reading distance.
  var ZXING_DECODE_INTERVAL_MS = 200;
  var DEBOUNCE_MS = 1200; // ignore identical consecutive decodes
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

  function Scanner(videoEl, canvasEl, onDecode) {
    this.video = videoEl;
    this.canvas = canvasEl;
    this.onDecode = onDecode;
    this.stream = null;
    this.videoTrack = null;
    this.usingNative = "BarcodeDetector" in window;
    this.detector = null;
    this.zxingReader = null;
    this.lastCode = null;
    this.lastCodeAt = 0;
    this.intervalHandle = null;
    this.torchOn = false;
    this.running = false;
    this.stopped = false; // stop() called, possibly before start() resolved
    this.decodeInFlight = false;
    this.canvasCtx = null;
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
      }
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

  Scanner.prototype._handleNativeResults = function (results, startedAt) {
    if (!results || !results.length) return;
    var decodeMs = performance.now() - startedAt;
    this._handleDecodedText(results[0].rawValue, startedAt, decodeMs);
  };

  Scanner.prototype._decodeZxing = function () {
    var w = this.video.videoWidth;
    var h = this.video.videoHeight;
    if (!w || !h) return null;
    // getContext("2d") is cheap but not free, and this runs every frame for
    // the whole shift. Cache it; the canvas element never changes.
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    if (!this.canvasCtx) this.canvasCtx = this.canvas.getContext("2d");
    this.canvasCtx.drawImage(this.video, 0, 0, w, h);
    var luminanceSource = new window.ZXing.HTMLCanvasElementLuminanceSource(this.canvas);
    var binaryBitmap = new window.ZXing.BinaryBitmap(new window.ZXing.HybridBinarizer(luminanceSource));
    var result = this.zxingReader.decode(binaryBitmap);
    return result ? result.getText() : null;
  };

  Scanner.prototype._handleDecodedText = function (text, startedAt, decodeMsOverride) {
    if (!text) return;
    var now = performance.now();
    var decodeMs = decodeMsOverride != null ? decodeMsOverride : now - startedAt;
    var wallNow = Date.now();
    if (text === this.lastCode && wallNow - this.lastCodeAt < DEBOUNCE_MS) {
      return; // same barcode still in frame -- don't fire fifty times
    }
    this.lastCode = text;
    this.lastCodeAt = wallNow;
    this.onDecode(text, decodeMs);
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

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Scanner;
  }
  if (root) {
    root.AutorackScanner = Scanner;
  }
})(typeof window !== "undefined" ? window : null);
