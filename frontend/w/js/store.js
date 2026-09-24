// IndexedDB persistence for the worker app. Three stores:
//   orders  (keyPath "id")  -- order payloads with their match index, for offline picking
//   outbox  (keyPath "id")  -- scans/undos/flags not yet confirmed by the server
//   meta    (keyPath "key") -- small values: sequence counter, recent scan history
//
// A scan is written here BEFORE any network attempt. If this write fails the
// worker is told to scan again: a scan the phone can't remember must not be
// shown as recorded.

const DB_NAME = "autorack-worker";
const DB_VERSION = 1;

let dbPromise = null;

function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("orders")) db.createObjectStore("orders", { keyPath: "id" });
      if (!db.objectStoreNames.contains("outbox")) db.createObjectStore("outbox", { keyPath: "id" });
      if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "key" });
    };
    req.onsuccess = () => {
      const db = req.result;
      // Another tab upgrading, or the browser evicting storage: drop the
      // cached handle so the next call reopens instead of failing forever.
      db.onversionchange = () => {
        db.close();
        dbPromise = null;
      };
      db.onclose = () => {
        dbPromise = null;
      };
      resolve(db);
    };
    req.onerror = () => {
      dbPromise = null;
      reject(req.error);
    };
    req.onblocked = () => {
      dbPromise = null;
      reject(new Error("IndexedDB open blocked"));
    };
  });
  return dbPromise;
}

function tx(storeName, mode, fn) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        let t;
        try {
          t = db.transaction(storeName, mode);
        } catch (err) {
          dbPromise = null;
          reject(err);
          return;
        }
        let result;
        Promise.resolve(fn(t.objectStore(storeName)))
          .then((r) => {
            result = r;
          })
          .catch(reject);
        t.oncomplete = () => resolve(result);
        t.onerror = () => reject(t.error);
        t.onabort = () => reject(t.error || new Error("transaction aborted"));
      }),
  );
}

function req(r) {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

export const store = {
  getOrder: (id) => tx("orders", "readonly", (s) => req(s.get(id))),
  putOrder: (order) => tx("orders", "readwrite", (s) => req(s.put(order))),
  allOrders: () => tx("orders", "readonly", (s) => req(s.getAll())),
  deleteOrder: (id) => tx("orders", "readwrite", (s) => req(s.delete(id))),

  outboxAdd: (ev) => tx("outbox", "readwrite", (s) => req(s.put(ev))),
  outboxAll: () =>
    tx("outbox", "readonly", (s) => req(s.getAll())).then((rows) => rows.sort((a, b) => a.client_seq - b.client_seq)),
  outboxRemove: (ids) =>
    tx("outbox", "readwrite", (s) => {
      for (const id of ids) s.delete(id);
      return true;
    }),
  outboxCount: () => tx("outbox", "readonly", (s) => req(s.count())),

  metaGet: (key) => tx("meta", "readonly", (s) => req(s.get(key))).then((row) => (row ? row.value : undefined)),
  metaSet: (key, value) => tx("meta", "readwrite", (s) => req(s.put({ key, value }))),

  /** Monotonic per-phone sequence number; orders events that share a timestamp. */
  async nextSeq() {
    const current = (await this.metaGet("seq")) || 0;
    const next = Math.max(current + 1, Date.now());
    await this.metaSet("seq", next);
    return next;
  },
};

/** Ask the browser not to evict our data under storage pressure. */
export function requestPersistence() {
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
}
