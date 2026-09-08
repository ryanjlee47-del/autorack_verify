// Minimal promise-based IndexedDB wrapper. No external lib -- IndexedDB's
// callback API is verbose but small enough to hand-write for the stores
// we need:
//   meta    (keyPath "key")   -- current bundle JSON, session info, outbox seq counter
//   outbox  (keyPath "uuid")  -- scans pending sync, drained in batches of 200
//   appeals (keyPath "uuid")  -- worker-submitted photo appeals pending upload
(function (root) {
  "use strict";

  var DB_NAME = "autorack-verify";
  var DB_VERSION = 2;

  // One connection for the life of the page, not one per operation.
  //
  // Every metaGet/outboxAdd/outboxCount and every sync tick used to call
  // indexedDB.open() and never close the result. Over a twelve-hour shift
  // that is thousands of live IDBDatabase handles, each one of which blocks
  // any future versionchange -- so the next schema bump would hang instead
  // of upgrading. The promise is cached, not the database, so concurrent
  // callers during the initial open share one request.
  var dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", { keyPath: "key" });
        }
        if (!db.objectStoreNames.contains("outbox")) {
          db.createObjectStore("outbox", { keyPath: "uuid" });
        }
        if (!db.objectStoreNames.contains("appeals")) {
          db.createObjectStore("appeals", { keyPath: "uuid" });
        }
      };
      req.onsuccess = function () {
        var db = req.result;
        // If another tab requests a version upgrade, close this handle and
        // drop the cache so the next call reopens rather than throwing
        // InvalidStateError on a connection the browser has torn down.
        db.onversionchange = function () {
          db.close();
          dbPromise = null;
        };
        db.onclose = function () {
          dbPromise = null;
        };
        resolve(db);
      };
      req.onerror = function () {
        // A failed open must not be cached, or every later operation on the
        // page inherits one transient failure permanently.
        dbPromise = null;
        reject(req.error);
      };
      req.onblocked = function () {
        dbPromise = null;
        reject(new Error("indexedDB open blocked"));
      };
    });
    return dbPromise;
  }

  function withStore(storeName, mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx;
        try {
          tx = db.transaction(storeName, mode);
        } catch (err) {
          // The cached handle went stale (tab closed it, storage evicted).
          // Drop it so the next caller reopens, and report the failure --
          // callers depend on knowing a durable write did not happen.
          dbPromise = null;
          reject(err);
          return;
        }
        var store = tx.objectStore(storeName);
        var result;
        Promise.resolve(fn(store))
          .then(function (r) {
            result = r;
          })
          .catch(reject);
        tx.oncomplete = function () {
          resolve(result);
        };
        tx.onerror = function () {
          reject(tx.error);
        };
      });
    });
  }

  function reqToPromise(req) {
    return new Promise(function (resolve, reject) {
      req.onsuccess = function () {
        resolve(req.result);
      };
      req.onerror = function () {
        reject(req.error);
      };
    });
  }

  function metaGet(key) {
    return withStore("meta", "readonly", function (store) {
      return reqToPromise(store.get(key)).then(function (row) {
        return row ? row.value : undefined;
      });
    });
  }

  function metaSet(key, value) {
    return withStore("meta", "readwrite", function (store) {
      return reqToPromise(store.put({ key: key, value: value }));
    });
  }

  function outboxAdd(scan) {
    return withStore("outbox", "readwrite", function (store) {
      return reqToPromise(store.put(scan));
    });
  }

  function outboxAll() {
    return withStore("outbox", "readonly", function (store) {
      return reqToPromise(store.getAll());
    });
  }

  function outboxRemoveMany(uuids) {
    return withStore("outbox", "readwrite", function (store) {
      uuids.forEach(function (u) {
        store.delete(u);
      });
      return true;
    });
  }

  function outboxCount() {
    return withStore("outbox", "readonly", function (store) {
      return reqToPromise(store.count());
    });
  }

  function appealAdd(appeal) {
    return withStore("appeals", "readwrite", function (store) {
      return reqToPromise(store.put(appeal));
    });
  }

  function appealAll() {
    return withStore("appeals", "readonly", function (store) {
      return reqToPromise(store.getAll());
    });
  }

  function appealRemove(uuid) {
    return withStore("appeals", "readwrite", function (store) {
      store.delete(uuid);
      return true;
    });
  }

  var IDB = {
    metaGet: metaGet,
    metaSet: metaSet,
    outboxAdd: outboxAdd,
    outboxAll: outboxAll,
    outboxRemoveMany: outboxRemoveMany,
    outboxCount: outboxCount,
    appealAdd: appealAdd,
    appealAll: appealAll,
    appealRemove: appealRemove,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = IDB;
  }
  if (root) {
    root.AutorackIDB = IDB;
  }
})(typeof window !== "undefined" ? window : null);
