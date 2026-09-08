// Worker scan screen orchestrator: loads the shift bundle (network once,
// then IndexedDB forever), builds the in-memory match index, wires up
// camera + wedge-scanner + manual entry, and runs the scan loop --
// including the appeal flow and end-of-shift summary.
//
// Zero network calls on the scan path: normalize() and matchAgainstIndex()
// are pure in-memory Map lookups. The ONLY fetch calls in this file are
// the one-time bundle download and the periodic background outbox/appeal
// sync -- neither is on the critical path between a decode and the
// worker seeing a result.
(function () {
  "use strict";

  var T = window.AutorackI18n.T;

  var sessionId = document.body.getAttribute("data-session-id");
  var workerSelfResolve = document.body.getAttribute("data-worker-self-resolve") === "true";

  var bundleInfoEl = document.getElementById("bundle-info");
  var overlayEl = document.getElementById("result-overlay");
  var resultLabelEl = document.getElementById("result-label");
  var resultDetailEl = document.getElementById("result-detail");
  var resultSkuEl = document.getElementById("result-sku");
  var dismissBtn = document.getElementById("dismiss-button");
  var appealOpenBtn = document.getElementById("appeal-open-button");
  var wedgeInput = document.getElementById("wedge-input");
  var torchButton = document.getElementById("torch-toggle");
  var cameraButton = document.getElementById("camera-toggle");
  var cameraOnIcon = cameraButton.querySelector(".icon-camera-on");
  var cameraOffIcon = cameraButton.querySelector(".icon-camera-off");
  var cameraOffPanel = document.getElementById("camera-off-panel");
  var cameraOffTitleEl = document.getElementById("camera-off-title");
  var cameraOffHintEl = document.getElementById("camera-off-hint");
  var cameraCountdownEl = document.getElementById("camera-countdown");
  var logoutButton = document.getElementById("logout-button");
  var videoEl = document.getElementById("camera-video");
  var canvasEl = document.getElementById("decode-canvas");

  var manualEntryToggle = document.getElementById("manual-entry-toggle");
  var manualEntryModal = document.getElementById("manual-entry-modal");
  var manualEntryTitle = document.getElementById("manual-entry-title");
  var manualEntryInput = document.getElementById("manual-entry-input");
  var manualEntryCancel = document.getElementById("manual-entry-cancel");
  var manualEntrySubmit = document.getElementById("manual-entry-submit");

  var appealModal = document.getElementById("appeal-modal");
  var appealModalTitle = document.getElementById("appeal-modal-title");
  var appealInstructions = document.getElementById("appeal-instructions");
  var appealPhotoInput = document.getElementById("appeal-photo-input");
  var appealPhotoPreview = document.getElementById("appeal-photo-preview");
  var appealTakePhotoBtn = document.getElementById("appeal-take-photo-button");
  var appealNoteInput = document.getElementById("appeal-note-input");
  var appealCancelBtn = document.getElementById("appeal-cancel-button");
  var appealSubmitBtn = document.getElementById("appeal-submit-button");

  var summaryOverlay = document.getElementById("summary-overlay");
  var summaryTitleEl = document.getElementById("summary-title");
  var summaryStatsEl = document.getElementById("summary-stats");
  var summaryDoneBtn = document.getElementById("summary-done-button");

  var matchIndex = null; // {tier: {key: [lineId,...]}}
  var bundleFailed = false; // a bundle that could not be applied is fatal, not silent
  // Object.create(null) throughout: manifest line ids and barcode text are
  // both used as object keys, and a plain {} answers "constructor",
  // "toString" and friends for keys that were never inserted.
  var linesById = Object.create(null);
  var DEFAULT_SETTINGS = { looseMatchEnabled: false, looseSuffixLen: 8, workerSelfResolve: false };
  var settings = DEFAULT_SETTINGS;
  var currentBundleVersion = 0;
  var disabledKeys = {};
  // lineId -> units scanned so far this session. A count, not a boolean:
  // a manifest line with qty_expected 4 is four physical cartons, and
  // calling unit two a DUPLICATE is a wrong answer on a screen whose whole
  // promise is three unmistakable ones.
  var sessionScannedCounts = Object.create(null);
  var sessionTally = { ok: 0, reject: 0, duplicate: 0, unresolved: 0 };
  var lastScanUuid = null; // which scan an appeal, if opened, refers to
  var selectedAppealPhoto = null;
  var outbox = new window.AutorackOutbox(sessionId);
  var appealQueue = new window.AutorackAppealQueue(sessionId);
  var scanner = null;
  var awaitingDismiss = false;
  var HEARTBEAT_MS = 30000;

  // The camera only ever runs for a fixed 15-second window per activation
  // (battery/privacy) -- it does not stay on indefinitely just because
  // scans keep happening. The wedge scanner input is unaffected and
  // always works regardless of camera state.
  var CAMERA_ON_DURATION_MS = 15000;
  var cameraOn = false;
  var cameraOffTimer = null;
  var cameraCountdownTimer = null;
  var cameraOffAt = 0;

  function uuidv4() {
    // RFC4122-ish v4 via crypto.getRandomValues -- no external lib needed.
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map.call(bytes, function (b) {
      return b.toString(16).padStart(2, "0");
    });
    return (
      hex.slice(0, 4).join("") + "-" + hex.slice(4, 6).join("") + "-" +
      hex.slice(6, 8).join("") + "-" + hex.slice(8, 10).join("") + "-" +
      hex.slice(10, 16).join("")
    );
  }

  function nowIso() {
    return new Date().toISOString().replace(/(\.\d{3})\d*Z$/, "$1Z");
  }

  function applyStaticText() {
    bundleInfoEl.textContent = T("bundleLoading");
    dismissBtn.textContent = T("dismissButton");
    appealOpenBtn.textContent = T("appealButton");
    logoutButton.textContent = T("logoutButton");
    manualEntryTitle.textContent = T("manualEntryTitle");
    manualEntryInput.placeholder = T("manualEntryPlaceholder");
    manualEntryCancel.textContent = T("manualEntryCancel");
    manualEntrySubmit.textContent = T("manualEntrySubmit");
    appealModalTitle.textContent = T("appealModalTitle");
    appealInstructions.textContent = T("appealInstructions");
    appealTakePhotoBtn.textContent = T("appealTakePhoto");
    appealNoteInput.placeholder = T("appealNotePlaceholder");
    appealCancelBtn.textContent = T("appealCancel");
    appealSubmitBtn.textContent = T("appealSubmit");
    summaryTitleEl.textContent = T("summaryTitle");
    summaryDoneBtn.textContent = T("summaryDone");
    cameraOffTitleEl.textContent = T("cameraOffTitle");
    cameraOffHintEl.textContent = T("cameraOffHint");
    appealOpenBtn.style.display = workerSelfResolve ? "inline-block" : "none";
  }

  // Built out of elements and textContent, never innerHTML. window.__bundleDate
  // is bundle.shift.date, which originates in an owner's form field and is
  // rendered on every worker phone that joins the shift -- innerHTML here was
  // stored XSS with an owner-to-worker delivery path.
  function setHeaderChip(stale) {
    var lineCount = Object.keys(linesById).length;
    bundleInfoEl.textContent = (window.__bundleDate || "") + " · " + lineCount + " lines -- ";
    var status = document.createElement("span");
    status.className = stale ? "status-stale" : "status-ok";
    status.textContent = stale ? T("bundleStale") : T("bundleOfflineReady");
    bundleInfoEl.appendChild(status);
  }

  function loadBundleFromNetwork() {
    return fetch("/w/bundle/" + sessionId).then(function (resp) {
      if (!resp.ok) throw new Error("bundle fetch failed");
      return resp.json();
    });
  }

  // Returns true if the bundle is now live. A failure here must be fatal and
  // visible: matchIndex staying null makes handleDecoded return early on
  // every scan, so the phone looks powered on and silently records nothing
  // for the rest of the shift -- no beep, no flash, nothing in the outbox.
  function applyBundle(bundle) {
    try {
      if (!bundle || !bundle.shift || !Array.isArray(bundle.lines) || !Array.isArray(bundle.keys)) {
        throw new Error("bundle is missing lines/keys/shift");
      }
      var nextLines = Object.create(null);
      bundle.lines.forEach(function (line) {
        nextLines[line.id] = line;
      });
      var nextIndex = window.Barcode.buildIndex(bundle.keys);
      linesById = nextLines;
      matchIndex = nextIndex;
      // A bundle cached by an older build can be missing settings entirely,
      // or missing a field this build reads. Merging over the defaults keeps
      // looseSuffixLen a number rather than undefined, which normalize()
      // would otherwise silently turn back into DEFAULT_SUFFIX_LEN on one
      // side of an already-delicate parity contract.
      var s = bundle.settings || {};
      settings = {
        looseMatchEnabled: !!s.looseMatchEnabled,
        looseSuffixLen: typeof s.looseSuffixLen === "number" && s.looseSuffixLen > 0
          ? s.looseSuffixLen
          : DEFAULT_SETTINGS.looseSuffixLen,
        workerSelfResolve: !!s.workerSelfResolve,
      };
      // tier -> [key]; mirrors the server's MatchIndex.disabled_keys. Omitting
      // a colliding key from the bundle is not the same suppression the
      // server applies -- see manifest_ingest.bundle_payload.
      disabledKeys = bundle.disabledKeys || {};
      currentBundleVersion = bundle.shift.bundleVersion;
      window.__bundleDate = bundle.shift.date;
      bundleFailed = false;
      setHeaderChip(false);
      return true;
    } catch (err) {
      matchIndex = null;
      bundleFailed = true;
      bundleInfoEl.textContent = T("bundleCouldNotLoad");
      return false;
    }
  }

  function initBundle() {
    return window.AutorackIDB.metaGet("bundle").then(function (cached) {
      if (cached) {
        applyBundle(cached);
      }
      // Always attempt a fresh fetch if online (first load has no cache;
      // subsequent loads refresh if the network happens to be up, but we
      // never block on it -- the cached bundle is what makes the phone
      // work through a full offline shift).
      if (navigator.onLine) {
        return refreshBundle().then(function () {
          if (!matchIndex) {
            bundleInfoEl.textContent = T("bundleCouldNotLoad");
          }
        });
      }
      if (!cached) {
        bundleInfoEl.textContent = T("bundleNoneDownloaded");
      }
    });
  }

  // Detecting staleness and stopping there left the phone in the one state
  // that costs money: w_sync refuses to bill any reject whose bundleVersion
  // is behind the shift's, so after a mid-shift manifest edit the phone kept
  // scanning and stopped producing billable catches until someone happened to
  // reload the page. The refresh logic already exists -- call it.
  function checkStaleness() {
    if (!navigator.onLine) return;
    fetch("/w/heartbeat/" + sessionId)
      .then(function (r) {
        return r.json();
      })
      .then(function (info) {
        if (info.bundleVersion === currentBundleVersion) return;
        setHeaderChip(true);
        return refreshBundle();
      })
      .catch(function () {});
  }

  // Fetch and persist a fresh bundle. Shared by initBundle and checkStaleness
  // so a stale bundle is repaired by exactly the path that installed it.
  function refreshBundle() {
    return loadBundleFromNetwork()
      .then(function (bundle) {
        return window.AutorackIDB.metaSet("bundle", bundle).then(function () {
          applyBundle(bundle);
        });
      })
      .catch(function () {
        // Offline or the fetch failed. The cached bundle stays live; the
        // header keeps saying stale, and the next heartbeat tries again.
      });
  }

  function recordAndShowResult(rawPayload, normalized, matchResult, decodeMs, matchStartedAt) {
    var matchMs = performance.now() - matchStartedAt;
    var result, lineId = null, sku = null, description = null, needsConfirmation = false;
    var visualKind; // what the worker sees -- only ever ok/reject/duplicate (3 signals, per spec)

    var line = matchResult.resolved ? linesById[matchResult.manifestLineId] : null;

    if (matchResult.resolved && !line) {
      // Resolved to a manifest line the bundle does not contain. This is not
      // a match, it is an internal inconsistency, and it must never be shown
      // as a green OK -- that waves an item through the dock on the strength
      // of a lookup that found nothing. Treat it the way we treat any
      // non-confident outcome: unresolved, which is never auto-billed.
      result = "unresolved";
      visualKind = "reject";
    } else if (matchResult.resolved) {
      lineId = matchResult.manifestLineId;
      sku = line.sku;
      description = line.description;
      needsConfirmation = !!matchResult.needsConfirmation;
      // qty_expected is how many physical units this line covers. Units two
      // through N are legitimate scans, not duplicates; only unit N+1 is.
      // A missing/zero qtyExpected means one unit, the previous behaviour.
      var expected = typeof line.qtyExpected === "number" && line.qtyExpected > 0 ? line.qtyExpected : 1;
      var already = sessionScannedCounts[lineId] || 0;
      if (already >= expected) {
        result = "duplicate";
      } else {
        result = "ok";
        sessionScannedCounts[lineId] = already + 1;
      }
      visualKind = result;
    } else {
      // Nothing resolved. Distinguish WHY, for billing/audit purposes --
      // the worker sees the identical "wrong item" treatment either way:
      //   - every tier had exactly zero candidates: this barcode matches
      //     nothing anywhere in the shift's manifests. That's a confident,
      //     unambiguous absence -- a genuine caught mis-ship -- so it's
      //     stored as 'reject' and is billing-eligible on its own.
      //   - some tier hit the mandatory ambiguity guard (2+ candidates)
      //     before falling through: we are NOT confident this item is
      //     wrong, just that we can't tell. Stored as 'unresolved' and
      //     is NEVER auto-billed -- it needs an owner-confirmed exception.
      var wasAmbiguous = Object.keys(matchResult.candidatesByTier).some(function (t) {
        return matchResult.candidatesByTier[t].length > 1;
      });
      result = wasAmbiguous ? "unresolved" : "reject";
      visualKind = "reject";
    }

    sessionTally[result] = (sessionTally[result] || 0) + 1;

    var scanUuid = uuidv4();
    lastScanUuid = scanUuid;
    var scan = {
      uuid: scanUuid,
      rawPayload: rawPayload,
      normalized: normalized,
      manifestLineId: lineId,
      matchedTier: matchResult.resolved ? matchResult.tier : null,
      result: result,
      decodeMs: decodeMs,
      matchMs: matchMs,
      tsClient: nowIso(),
      bundleVersion: currentBundleVersion,
      needsConfirmation: needsConfirmation,
    };
    // The outbox write is the load-bearing durability claim in the whole
    // system ("every scan is written to IndexedDB immediately, before any
    // network attempt"). It was fire-and-forget: nothing awaited it, nothing
    // caught it, and showResult fired on the next line regardless -- so on
    // an iOS storage eviction or a quota failure the worker got a green
    // flash for a scan that no longer existed anywhere.
    outbox.add(scan).then(
      function () {
        showResult(visualKind, sku, description);
      },
      function () {
        sessionTally[result] = Math.max(0, (sessionTally[result] || 1) - 1);
        if (result === "ok" && lineId !== null) {
          sessionScannedCounts[lineId] = Math.max(0, (sessionScannedCounts[lineId] || 1) - 1);
        }
        showScanNotSaved();
      }
    );
  }

  // visualKind is always one of "ok"/"reject"/"duplicate" -- the 3 signals
  // in the spec. The stored scan.result (which also has an "unresolved"
  // value, for billing/audit purposes) never reaches this function.
  function showResult(visualKind, sku, description) {
    window.AutorackFeedback.play(visualKind);
    overlayEl.className = "result-overlay showing result-" + visualKind;
    var labels = { ok: T("resultOk"), reject: T("resultRejectLabel"), duplicate: T("resultDuplicateLabel") };
    resultLabelEl.textContent = labels[visualKind] || visualKind;
    resultDetailEl.textContent = visualKind === "reject" ? T("resultRejectDetail") : (visualKind === "duplicate" ? T("resultDuplicateDetail") : "");
    resultSkuEl.textContent = sku || description || "";

    if (visualKind === "reject") {
      awaitingDismiss = true;
      dismissBtn.style.display = "inline-block";
      appealOpenBtn.style.display = workerSelfResolve ? "inline-block" : "none";
    } else {
      dismissBtn.style.display = "none";
      appealOpenBtn.style.display = "none";
      setTimeout(hideResult, 900);
    }
  }

  function hideResult() {
    awaitingDismiss = false;
    overlayEl.className = "result-overlay";
  }

  function modalIsOpen() {
    return manualEntryModal.style.display !== "none" ||
      appealModal.style.display !== "none" ||
      summaryOverlay.style.display !== "none";
  }

  // A scan that did not reach durable storage. Deliberately shaped like the
  // reject treatment -- it requires a dismiss tap, so it cannot be missed the
  // way a 900ms auto-hiding flash can.
  function showScanNotSaved(message) {
    window.AutorackFeedback.play("reject");
    overlayEl.className = "result-overlay showing result-reject";
    resultLabelEl.textContent = T("scanNotSavedLabel");
    resultDetailEl.textContent = message || T("scanNotSavedDetail");
    resultSkuEl.textContent = "";
    awaitingDismiss = true;
    dismissBtn.style.display = "inline-block";
    appealOpenBtn.style.display = "none";
  }

  dismissBtn.addEventListener("click", function () {
    hideResult();
    wedgeInput.focus();
  });

  function handleDecoded(rawPayload, decodeMs) {
    if (awaitingDismiss) return;
    if (!matchIndex) {
      // Never fail silently here: a phone with no index looks identical to a
      // working one. Say so on every attempted scan.
      showScanNotSaved(bundleFailed ? T("bundleCouldNotLoad") : null);
      return;
    }
    if (modalIsOpen()) return; // a wedge burst while a modal is open is not a scan
    var matchStart = performance.now();
    var norm = window.Barcode.normalize(rawPayload, settings.looseSuffixLen);
    var matchResult = window.Barcode.matchAgainstIndex(matchIndex, rawPayload, {
      looseMatchEnabled: settings.looseMatchEnabled,
      suffixLen: settings.looseSuffixLen,
      disabledKeys: disabledKeys,
    });
    recordAndShowResult(rawPayload, norm.normalized, matchResult, decodeMs, matchStart);
  }

  // --- Wedge scanner input: always-focused, invisible, Enter/Tab terminate ---
  // While a modal is open the wedge scanner is still a keyboard, and its
  // burst used to type itself into whatever was focused -- usually the appeal
  // note field. Keep the (invisible) wedge input focused unless the worker
  // has deliberately put the caret in a field of their own, so a stray scan
  // is swallowed there and dropped by handleDecoded's modalIsOpen() guard
  // rather than appearing as text in the note.
  function workerIsTypingInAField() {
    var el = document.activeElement;
    if (!el || el === wedgeInput) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
  }

  function keepWedgeFocused() {
    if (awaitingDismiss) return;
    if (workerIsTypingInAField()) return;
    if (document.activeElement !== wedgeInput) {
      wedgeInput.focus();
    }
  }
  wedgeInput.addEventListener("blur", function () {
    setTimeout(keepWedgeFocused, 50);
  });
  wedgeInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      var value = wedgeInput.value;
      wedgeInput.value = "";
      if (value) handleDecoded(value, 0);
    }
  });
  setInterval(keepWedgeFocused, 1000);

  // --- Manual barcode entry: always available, independent of camera/wedge
  // state -- for damaged or unreadable labels neither can pick up. ---
  function openManualEntry() {
    manualEntryInput.value = "";
    manualEntryModal.style.display = "flex";
    manualEntryInput.focus();
  }
  function closeManualEntry() {
    manualEntryModal.style.display = "none";
    wedgeInput.focus();
  }
  manualEntryToggle.addEventListener("click", openManualEntry);
  manualEntryCancel.addEventListener("click", closeManualEntry);
  manualEntrySubmit.addEventListener("click", function () {
    var value = manualEntryInput.value.trim();
    closeManualEntry();
    if (value) handleDecoded(value, 0);
  });
  manualEntryInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      manualEntrySubmit.click();
    }
  });

  // --- Appeal: only offered when the account allows worker self-resolve,
  // and only from a REJECT. A photo is mandatory -- this feeds the
  // owner's exception-review queue (kind='worker_reported'), not a
  // one-tap override. ---
  function resetAppealModal() {
    selectedAppealPhoto = null;
    appealPhotoInput.value = "";
    appealPhotoPreview.style.display = "none";
    appealPhotoPreview.src = "";
    appealNoteInput.value = "";
    appealTakePhotoBtn.textContent = T("appealTakePhoto");
    appealSubmitBtn.disabled = true;
  }
  function openAppeal() {
    resetAppealModal();
    overlayEl.className = "result-overlay";
    appealModal.style.display = "flex";
  }
  function closeAppeal() {
    appealModal.style.display = "none";
    hideResult();
    wedgeInput.focus();
  }
  appealOpenBtn.addEventListener("click", openAppeal);
  appealCancelBtn.addEventListener("click", closeAppeal);
  appealTakePhotoBtn.addEventListener("click", function () {
    appealPhotoInput.click();
  });
  appealPhotoInput.addEventListener("change", function () {
    var file = appealPhotoInput.files && appealPhotoInput.files[0];
    if (!file) return;
    selectedAppealPhoto = file;
    appealPhotoPreview.src = URL.createObjectURL(file);
    appealPhotoPreview.style.display = "block";
    appealTakePhotoBtn.textContent = T("appealRetakePhoto");
    appealSubmitBtn.disabled = false;
  });
  appealSubmitBtn.addEventListener("click", function () {
    if (!selectedAppealPhoto || !lastScanUuid) {
      alert(T("appealPhotoRequired"));
      return;
    }
    appealQueue.add({
      uuid: uuidv4(),
      scanUuid: lastScanUuid,
      sessionId: sessionId,
      note: appealNoteInput.value.trim(),
      photoBlob: selectedAppealPhoto,
    }).then(
      function () {
        appealModal.style.display = "none";
        hideResult();
        showToast(T("appealQueued"));
        wedgeInput.focus();
      },
      function () {
        // "Appeal saved" was shown unconditionally, before anything knew
        // whether the write happened. Photo Blobs are exactly what storage
        // pressure evicts first.
        alert(T("appealNotSaved"));
      }
    );
  });

  function showToast(message) {
    resultLabelEl.textContent = "";
    resultDetailEl.textContent = message;
    resultSkuEl.textContent = "";
    overlayEl.className = "result-overlay showing result-ok";
    setTimeout(function () {
      overlayEl.className = "result-overlay";
    }, 1400);
  }

  // --- Camera scanner: on/off is explicit and worker-controlled, and
  // every activation is a hard 15-second window, not an idle timeout --
  // it turns itself back off whether or not the worker is still scanning.
  function setCameraButtonState(on) {
    cameraButton.classList.toggle("active", on);
    cameraOnIcon.style.display = on ? "block" : "none";
    cameraOffIcon.style.display = on ? "none" : "block";
    cameraOffPanel.style.display = on ? "none" : "flex";
  }

  function tickCountdown() {
    var remainingMs = cameraOffAt - Date.now();
    if (remainingMs <= 0) {
      cameraCountdownEl.style.display = "none";
      return;
    }
    cameraCountdownEl.style.display = "inline";
    cameraCountdownEl.textContent = Math.ceil(remainingMs / 1000) + "s";
  }

  function turnCameraOff() {
    cameraOn = false;
    if (cameraOffTimer) clearTimeout(cameraOffTimer);
    if (cameraCountdownTimer) clearInterval(cameraCountdownTimer);
    cameraOffTimer = null;
    cameraCountdownTimer = null;
    cameraCountdownEl.style.display = "none";
    if (scanner) scanner.stop();
    setCameraButtonState(false);
    torchButton.style.display = "none";
    wedgeInput.focus();
  }

  function turnCameraOn() {
    if (cameraOn) return;
    cameraOn = true;
    setCameraButtonState(true);
    scanner = new window.AutorackScanner(videoEl, canvasEl, handleDecoded);
    scanner.start().then(function () {
      if (!cameraOn) return; // turned off again before the promise resolved
      torchButton.style.display = scanner.hasTorch() ? "flex" : "none";
    }).catch(function () {
      // Camera unavailable/denied -- wedge scanner and manual entry still
      // work, they're first-class, not a fallback.
      cameraOn = false;
      setCameraButtonState(false);
      cameraOffTitleEl.textContent = T("cameraUnavailableTitle");
      cameraOffHintEl.textContent = T("cameraUnavailableHint");
    });

    cameraOffAt = Date.now() + CAMERA_ON_DURATION_MS;
    cameraOffTimer = setTimeout(turnCameraOff, CAMERA_ON_DURATION_MS);
    cameraCountdownTimer = setInterval(tickCountdown, 250);
    tickCountdown();
  }

  cameraButton.addEventListener("click", function () {
    if (cameraOn) {
      turnCameraOff();
    } else {
      turnCameraOn();
    }
  });

  torchButton.addEventListener("click", function () {
    if (scanner) scanner.toggleTorch();
  });

  // --- End-of-shift summary: factual counts only, no dollar figures --
  // that framing is for the owner's "Workers" page; a worker's own
  // screen stays neutral, not punitive. ---
  function renderSummary(tally) {
    var rows = [
      [tally.ok, T("summaryScanned")],
      [tally.reject, T("summaryCaught")],
      [tally.duplicate, T("summaryDuplicate")],
      [tally.unresolved, T("summaryNeedsReview")],
    ];
    // Element construction, not innerHTML. The counts are numbers today, but
    // they arrive from /w/session-summary and this is the same pattern that
    // made the header chip an XSS sink.
    summaryStatsEl.textContent = "";
    rows.forEach(function (r) {
      var wrap = document.createElement("div");
      wrap.className = "summary-stat";
      var value = document.createElement("div");
      value.className = "summary-stat-value";
      value.textContent = String(r[0]);
      var label = document.createElement("div");
      label.className = "summary-stat-label";
      label.textContent = r[1];
      wrap.appendChild(value);
      wrap.appendChild(label);
      summaryStatsEl.appendChild(wrap);
    });
    summaryOverlay.style.display = "flex";
  }

  function fetchServerSummaryOrLocal() {
    if (!navigator.onLine) return Promise.resolve(sessionTally);
    return fetch("/w/session-summary/" + sessionId)
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        return {
          ok: data.okCount, reject: data.rejectCount,
          duplicate: data.duplicateCount, unresolved: data.unresolvedCount,
        };
      })
      .catch(function () {
        return sessionTally;
      });
  }

  // --- Log out: stop the camera, try to flush this session's outbox
  // before navigating away (a page change kills the running sync loop),
  // warn rather than silently strand data if that flush can't finish,
  // then show the shift summary before leaving. ---
  function logOut() {
    turnCameraOff();
    logoutButton.disabled = true;
    logoutButton.textContent = T("loggingOut");
    outbox.flushCurrentSession(4000).then(function (remaining) {
      if (remaining > 0) {
        var proceed = confirm(T("logoutConfirmUnsynced", { count: remaining }));
        if (!proceed) {
          logoutButton.disabled = false;
          logoutButton.textContent = T("logoutButton");
          return;
        }
      }
      // Tell the server the session is over. Purely client-side logout left
      // the token valid, so the scan screen could be reopened from browser
      // history or by the next person to pick up a shared phone. Queued
      // scans still sync afterwards -- ending a session revokes acquiring
      // anything new, not draining what is already recorded.
      endServerSession().then(function () {
        fetchServerSummaryOrLocal().then(renderSummary);
      });
    });
  }

  function endServerSession() {
    if (!navigator.onLine) return Promise.resolve();
    return fetch("/w/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: sessionId }),
      keepalive: true,
    }).catch(function () {
      // Offline or the request failed. The summary still renders; the
      // session simply outlives the logout, as it always did.
    });
  }
  logoutButton.addEventListener("click", logOut);
  summaryDoneBtn.addEventListener("click", function () {
    window.location.href = "/w";
  });

  // --- Bootstrap: unlock audio + start the camera's first 15s window on
  // the first tap, so the shift's first beep isn't swallowed by autoplay
  // policy, and camera permission is requested from a genuine user gesture. ---
  // Two separate things, deliberately not one flag.
  //
  // Audio unlock must happen on the FIRST input of any kind, including a
  // wedge scan, or the shift's first beep is swallowed by autoplay policy.
  // Starting the camera must happen only on a real tap: a wedge scanner is a
  // keyboard, so a single bootstrap bound to keydown raised a camera
  // permission prompt on phones that are wedge-only and never want one --
  // and getUserMedia from a keydown is not a qualifying user gesture in
  // every browser regardless.
  //
  // Sharing one `started` flag between them is what makes this subtle: a
  // wedge scan would consume the bootstrap, and the real tap that followed
  // would find started === true and never start the camera at all. Two
  // flags, two lifetimes.
  var audioUnlocked = false;
  var cameraBootstrapped = false;

  function bootstrapAudio() {
    if (audioUnlocked) return;
    audioUnlocked = true;
    window.AutorackFeedback.unlockAudio();
    wedgeInput.focus();
  }

  function bootstrapCamera() {
    bootstrapAudio();
    if (cameraBootstrapped) return;
    cameraBootstrapped = true;
    turnCameraOn();
  }

  // Not {once: true} on keydown: it only unlocks audio, and it must stay
  // armed until a tap arrives so the camera bootstrap is still available.
  document.addEventListener("click", bootstrapCamera, { once: true });
  document.addEventListener("touchend", bootstrapCamera, { once: true });
  document.addEventListener("keydown", bootstrapAudio);

  // --- Init ---
  applyStaticText();
  // outbox.init() loads seqCounter from IndexedDB, and it must complete
  // BEFORE any scan can be recorded. It used to run after initBundle()
  // resolved -- but initBundle applies the cached bundle synchronously and
  // then awaits a network refresh, so on a reload with flaky wifi there was a
  // multi-second window where the index was live, the wedge scanner worked,
  // and this.seq was still 0. init() then overwrote it from storage, and
  // syncOnce sorts by seq.
  outbox
    .init()
    .catch(function () {})
    .then(function () {
      return initBundle();
    })
    .catch(function () {
      // initBundle already handles its own failures; this catches anything
      // it could not (a rejected metaGet, say). The loops MUST still start:
      // without them a phone that hit one IndexedDB hiccup at load never
      // syncs again for the life of the page, and an unhandled rejection is
      // all the signal there would have been.
    })
    .then(function () {
      outbox.startLoop();
      appealQueue.startLoop();
      setInterval(checkStaleness, HEARTBEAT_MS);
    });

  if ("serviceWorker" in navigator) {
    // /w/join never loads this script (it has no <script> tags at all),
    // so it can never register or update the service worker itself --
    // only this page (/w/scan) does. That means a worker whose phone
    // already has an old service worker installed from a previous visit
    // can scan a fresh door QR straight into /w/join and get answered by
    // the STALE worker, with no code anywhere forcing a swap. A fixed
    // sw.js on the server doesn't help until the browser has actually
    // finished installing and activating it, which happens
    // asynchronously and, on iOS Safari in particular, can lag well
    // behind a normal page load.
    //
    // Two things close that gap:
    //   1. reg.update() here forces an immediate check instead of
    //      waiting on the browser's own (sometimes very lazy,
    //      especially on iOS) background schedule.
    //   2. controllerchange fires the moment a new worker actually takes
    //      over this page -- reloading right then means the NEXT scan
    //      is answered by the new code, without the worker needing to
    //      know anything about clearing site data or force-quitting
    //      Safari to get unstuck.
    navigator.serviceWorker.register("/w/sw.js", { scope: "/w/" }).then(function (reg) {
      reg.update().catch(function () {});
    }).catch(function () {});

    var reloadedForNewWorker = false;
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (reloadedForNewWorker) return; // a worker can fire this more than once
      reloadedForNewWorker = true;
      window.location.reload();
    });
  }
})();
