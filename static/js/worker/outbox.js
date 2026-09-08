// Outbox: every scan is written to IndexedDB immediately (before any
// network attempt), then a background loop drains it in batches of 200
// whenever the network is up. The server ingest endpoint is idempotent on
// scan uuid, so double-sends (retry after a dropped response, or a resend
// after the phone died mid-batch and rebooted) are safe no-ops.
//
// Each scan is tagged with the sessionId that was active when it was
// scanned (not "whatever session is active right now"). That matters
// because a worker can log out with scans still unsynced (see
// static/js/worker/app.js's logOut()) and a *different* session can
// become active before those drain -- without per-scan tagging, leftover
// scans would sync under the wrong worker's session_id. syncOnce() groups
// the outbox by sessionId and sends one request per group.
(function (root) {
  "use strict";

  var BATCH_SIZE = 200;
  var RETRY_INTERVAL_MS = 4000;

  function Outbox(sessionId) {
    this.sessionId = sessionId;
    this.seq = 0;
    this.syncing = false;
    this.onSync = null; // callback(result) after each successful sync round
  }

  Outbox.prototype.init = function () {
    var self = this;
    return window.AutorackIDB.metaGet("seqCounter").then(function (v) {
      self.seq = v || 0;
    });
  };

  Outbox.prototype.nextSeq = function () {
    this.seq += 1;
    // Persisting the counter is best-effort -- the scan write itself is the
    // durable one -- but the rejection must be swallowed explicitly rather
    // than left as an unhandled promise rejection on the scan hot path.
    window.AutorackIDB.metaSet("seqCounter", this.seq).catch(function () {});
    return this.seq;
  };

  Outbox.prototype.add = function (scan) {
    scan.seq = this.nextSeq();
    scan.sessionId = this.sessionId;
    return window.AutorackIDB.outboxAdd(scan);
  };

  Outbox.prototype.pendingCount = function () {
    return window.AutorackIDB.outboxCount();
  };

  function groupBySession(rows) {
    var groups = {};
    var order = [];
    rows.forEach(function (row) {
      var key = String(row.sessionId);
      if (!groups[key]) {
        groups[key] = [];
        order.push(key);
      }
      groups[key].push(row);
    });
    return order.map(function (key) {
      return { sessionId: groups[key][0].sessionId, scans: groups[key] };
    });
  }

  function postBatch(sessionId, scans) {
    var body = {
      sessionId: sessionId,
      clientNow: new Date().toISOString().replace(/(\.\d{3})\d*Z$/, "$1Z"),
      scans: scans,
    };
    return fetch("/w/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      if (!resp.ok) throw new Error("sync failed: " + resp.status);
      return resp.json();
    });
  }

  Outbox.prototype.syncOnce = function () {
    var self = this;
    if (this.syncing) return Promise.resolve(null);
    if (!navigator.onLine) return Promise.resolve(null);
    this.syncing = true;

    // Every exit from here must clear self.syncing. The outer promise
    // previously had no .catch, so if outboxAll() itself rejected (storage
    // evicted, connection torn down) syncing stayed true for the life of
    // the page: the loop kept ticking and returning early, the outbox never
    // drained again, and flushCurrentSession at logout returned instantly.
    // No visible symptom, and every scan after that point was lost.
    return window.AutorackIDB.outboxAll().then(function (all) {
      if (!all.length) {
        self.syncing = false;
        return null;
      }
      all.sort(function (a, b) {
        return a.seq - b.seq;
      });
      var groups = groupBySession(all.slice(0, BATCH_SIZE));
      var lastResult = null;

      return groups
        .reduce(function (chain, group) {
          return chain.then(function () {
            return postBatch(group.sessionId, group.scans).then(function (result) {
              lastResult = result;
              // Drop both what the server stored and what it refused as
              // malformed. A scan the server will never accept has to
              // leave the outbox, or it is retried on every sync round
              // forever and blocks nothing but wastes every request.
              var done = (result.accepted || []).concat(result.rejected || []);
              return window.AutorackIDB.outboxRemoveMany(done);
            });
          });
        }, Promise.resolve())
        .then(function () {
          self.syncing = false;
          if (self.onSync && lastResult) self.onSync(lastResult);
          return lastResult;
        })
        .catch(function () {
          self.syncing = false;
          return null;
        });
    }).catch(function () {
      self.syncing = false;
      return null;
    });
  };

  // Best-effort: try to drain everything belonging to THIS session before
  // logging out, so we don't strand unsynced scans behind a page
  // navigation that would kill the running JS (and therefore this sync
  // loop). Resolves either way -- callers decide what to do if scans are
  // still pending afterward (see app.js's logOut()).
  Outbox.prototype.flushCurrentSession = function (timeoutMs) {
    var self = this;
    var deadline = Date.now() + (timeoutMs || 3000);
    var POLL_MS = 150;

    function wait(ms) {
      return new Promise(function (resolve) {
        setTimeout(resolve, ms);
      });
    }

    function attempt() {
      return window.AutorackIDB.outboxAll().then(function (all) {
        var mine = all.filter(function (row) {
          return String(row.sessionId) === String(self.sessionId);
        });
        if (!mine.length || Date.now() > deadline || !navigator.onLine) {
          return mine.length;
        }
        return self.syncOnce().then(function (result) {
          // syncOnce() returns null immediately when the background loop is
          // already mid-sync. Recursing on that with no delay is a tight
          // loop for the whole timeout, and each turn of it calls
          // outboxAll() again. Yield instead and let the in-flight sync
          // finish -- it is draining the same rows we are waiting on.
          if (result === null) return wait(POLL_MS).then(attempt);
          return attempt();
        });
      }).catch(function () {
        // Cannot read the outbox at all. Report "unknown but non-zero" so
        // logOut() warns rather than silently claiming everything drained.
        return 1;
      });
    }
    return attempt();
  };

  Outbox.prototype.startLoop = function () {
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
    module.exports = Outbox;
  }
  if (root) {
    root.AutorackOutbox = Outbox;
  }
})(typeof window !== "undefined" ? window : null);
