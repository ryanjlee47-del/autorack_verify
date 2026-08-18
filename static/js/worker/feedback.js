// Audio + haptic + visual feedback for scan results. Audio/haptic come
// first because the phone is often in a holster or the worker isn't
// looking at the screen -- three unmistakably distinct signals:
//   OK        short high beep,  40ms  vibrate, green flash
//   REJECT    low double buzz, 300ms  vibrate, full-screen red, requires dismiss
//   DUPLICATE mid two-tone,   double-short vibrate, amber
//
// Audio is synthesized with the Web Audio API (oscillator tones) rather
// than shipping audio files -- keeps this offline-safe with zero extra
// assets to vendor/cache, and the tones are unlocked on the first user
// gesture so the shift's first beep isn't swallowed by autoplay policy.
(function (root) {
  "use strict";

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

  function unlockAudio() {
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
    ok: {
      sound: function () {
        tone(1800, 90, 0, 0.18);
      },
      vibrate: [40],
    },
    reject: {
      sound: function () {
        tone(220, 160, 0, 0.28);
        tone(220, 160, 220, 0.28);
      },
      vibrate: [300],
    },
    duplicate: {
      sound: function () {
        tone(900, 110, 0, 0.2);
        tone(1300, 110, 140, 0.2);
      },
      vibrate: [80, 60, 80],
    },
    unresolved: {
      sound: function () {
        tone(500, 250, 0, 0.2);
      },
      vibrate: [200],
    },
  };

  function play(kind) {
    var signal = SIGNALS[kind] || SIGNALS.unresolved;
    signal.sound();
    vibrate(signal.vibrate);
  }

  var Feedback = {
    unlockAudio: unlockAudio,
    play: play,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = Feedback;
  }
  if (root) {
    root.AutorackFeedback = Feedback;
  }
})(typeof window !== "undefined" ? window : null);
