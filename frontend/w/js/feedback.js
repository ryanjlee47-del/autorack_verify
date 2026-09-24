// Sound, vibration and colour for scan results. Sound and vibration come
// first: the phone is often on a lanyard and the worker isn't looking.
// Tones are synthesized with Web Audio, so there are no audio files to cache.

var audioCtx = null;
var unlocked = false;

function ensureAudioContext() {
  if (!audioCtx) {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    audioCtx = new Ctx();
  }
  return audioCtx;
}

export function unlockAudio() {
  if (unlocked) return;
  var ctx = ensureAudioContext();
  if (!ctx) return;
  // Play a near-silent buffer to unlock the context under autoplay policy.
  var buffer = ctx.createBuffer(1, 1, 22050);
  var src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  src.start(0);
  if (ctx.state === "suspended") ctx.resume();
  unlocked = true;
}

function tone(freq, durationMs, delayMs, volume) {
  var ctx = ensureAudioContext();
  if (!ctx) return;
  var startAt = ctx.currentTime + (delayMs || 0) / 1000;
  var osc = ctx.createOscillator();
  var gain = ctx.createGain();
  osc.type = "square";
  osc.frequency.setValueAtTime(freq, startAt);
  gain.gain.setValueAtTime(0, startAt);
  gain.gain.linearRampToValueAtTime(volume || 0.2, startAt + 0.01);
  gain.gain.linearRampToValueAtTime(0, startAt + durationMs / 1000);
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start(startAt);
  osc.stop(startAt + durationMs / 1000 + 0.02);
}

function vibrate(pattern) {
  if (navigator.vibrate) {
    try {
      navigator.vibrate(pattern);
    } catch (e) {
      // Some browsers throw if called outside a user gesture context;
      // scanning itself is user-initiated (camera stream running from a
      // tap), so this should not normally happen.
    }
  }
}

var SIGNALS = {
  // Right item: one short high beep, a tap.
  ok: {
    sound: function () {
      tone(1800, 90, 0, 0.18);
    },
    vibrate: [40],
  },
  // Wrong item: low double buzz, long vibration. Unmistakable in a holster.
  bad: {
    sound: function () {
      tone(220, 160, 0, 0.28);
      tone(220, 160, 220, 0.28);
    },
    vibrate: [300, 80, 300],
  },
  // Stop and look (over-pick, needs review): two-tone, double short buzz.
  warn: {
    sound: function () {
      tone(900, 110, 0, 0.2);
      tone(1300, 110, 140, 0.2);
    },
    vibrate: [80, 60, 80],
  },
};

export function play(kind) {
  // hasOwnProperty, not a bare lookup: play("constructor") would otherwise
  // find Function.prototype.constructor, and `signal.sound()` on it throws
  // -- so an unexpected kind would kill the feedback instead of falling
  // back to it. Same defect class as the index lookups in barcode.js.
  var signal = Object.prototype.hasOwnProperty.call(SIGNALS, kind)
    ? SIGNALS[kind]
    : SIGNALS.warn;
  signal.sound();
  vibrate(signal.vibrate);
}
