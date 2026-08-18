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

  function openDb() {
    return new Promise(function (resolve, reject) {
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
        resolve(req.result);
      };
      req.onerror = function () {
        reject(req.error);
      };
    });
  }

  function withStore(storeName, mode, fn) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(storeName, mode);
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
