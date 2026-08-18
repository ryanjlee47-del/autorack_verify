// Appeal upload queue: mirrors outbox.js's offline-safe pattern (write
// locally first, drain in the background whenever online) but for
// worker-submitted photo appeals instead of scan records. Uploaded as
// multipart/form-data since a photo Blob doesn't belong in a JSON batch.
//
// The server may return 409 if the underlying scan hasn't synced yet
// (the appeal queue and the scan outbox drain independently) -- that's
// treated as "not ready", not a failure, and retried on the next tick.
(function (root) {
  "use strict";

  var RETRY_INTERVAL_MS = 5000;

  function AppealQueue(sessionId) {
    this.sessionId = sessionId;
    this.syncing = false;
  }

  AppealQueue.prototype.add = function (appeal) {
    // appeal: {uuid, scanUuid, sessionId, note, photoBlob}
    return window.AutorackIDB.appealAdd(appeal);
  };

  AppealQueue.prototype.pendingCount = function () {
    return window.AutorackIDB.appealAll().then(function (all) {
      return all.length;
    });
  };

  function uploadOne(appeal) {
    var form = new FormData();
    form.append("sessionId", appeal.sessionId);
    form.append("scanUuid", appeal.scanUuid);
    if (appeal.note) form.append("note", appeal.note);
    form.append("photo", appeal.photoBlob, "appeal.jpg");
    return fetch("/w/appeal", { method: "POST", body: form }).then(function (resp) {
      if (resp.status === 409) return false; // scan not synced yet -- retry later
      if (!resp.ok) throw new Error("appeal upload failed: " + resp.status);
      return window.AutorackIDB.appealRemove(appeal.uuid).then(function () {
        return true;
      });
    });
  }

  AppealQueue.prototype.syncOnce = function () {
    var self = this;
    if (this.syncing || !navigator.onLine) return Promise.resolve(null);
    this.syncing = true;

    return window.AutorackIDB.appealAll().then(function (all) {
      if (!all.length) {
        self.syncing = false;
        return null;
      }
      return all
        .reduce(function (chain, appeal) {
          return chain.then(function () {
            return uploadOne(appeal).catch(function () {});
          });
        }, Promise.resolve())
        .then(function () {
          self.syncing = false;
        });
    });
  };

  AppealQueue.prototype.startLoop = function () {
    var self = this;
    var tick = function () {
      self.syncOnce().finally(function () {
        setTimeout(tick, RETRY_INTERVAL_MS);
      });
    };
    tick();
    window.addEventListener("online", function () {
      self.syncOnce();
    });
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = AppealQueue;
  }
  if (root) {
    root.AutorackAppealQueue = AppealQueue;
  }
})(typeof window !== "undefined" ? window : null);
