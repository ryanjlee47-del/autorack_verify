// Background sync: drain the outbox to POST /api/worker/sync whenever there
// is a connection. The server is idempotent on event id, so a batch re-sent
// after a dropped response is harmless.

import { request } from "../../shared/api.js";
import { isSettled, toWire } from "./state.js";
import { store } from "./store.js";

const BATCH = 200;
const BASE_DELAY_MS = 3000;
const MAX_DELAY_MS = 60000;

export class Sync {
  /**
   * handlers: {
   *   deviceToken: () => string,
   *   onResult(events, response)  -- after each successful batch
   *   onStatus({online, pending, syncing, lastError})
   *   onAuthLost(code)            -- device unlinked
   * }
   */
  constructor(handlers) {
    this.h = handlers;
    this.syncing = false;
    this.online = navigator.onLine;
    this.failures = 0;
    this.timer = null;
    this.lastError = null;
    this.stopped = false;
  }

  start() {
    window.addEventListener("online", () => {
      this.online = true;
      this.kick();
    });
    window.addEventListener("offline", () => {
      this.online = false;
      this.emit();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") this.kick();
    });
    this.kick();
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
  }

  async emit() {
    let pending = 0;
    try {
      pending = (await store.outboxCount()) + (await store.photoCount());
    } catch {
      pending = -1;
    }
    this.h.onStatus({ online: this.online, pending, syncing: this.syncing, lastError: this.lastError });
  }

  /** Sync now (debounced by the in-flight guard), then keep polling. */
  kick() {
    clearTimeout(this.timer);
    this.run();
  }

  schedule(ms) {
    clearTimeout(this.timer);
    if (!this.stopped) this.timer = setTimeout(() => this.run(), ms);
  }

  async run() {
    if (this.syncing || this.stopped) return;
    this.syncing = true;
    this.emit();
    let delay = BASE_DELAY_MS * 5;
    try {
      const more = await this.syncOnce();
      this.failures = 0;
      this.lastError = null;
      this.online = true;
      delay = more ? 0 : BASE_DELAY_MS * 5;
    } catch (e) {
      this.failures += 1;
      this.lastError = e;
      if (e.isNetwork) this.online = false;
      if (e.status === 401 && e.code === "device_unlinked") {
        this.h.onAuthLost(e.code);
        this.stopped = true;
      }
      delay = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** Math.min(this.failures, 5));
    } finally {
      this.syncing = false;
      this.emit();
      this.schedule(delay);
    }
  }

  /** One batch. Resolves true if more events are waiting. */
  async syncOnce() {
    const all = await store.outboxAll();
    if (!all.length) {
      await this.uploadPhotos();
      return false;
    }
    const batch = all.slice(0, BATCH);
    const resp = await request("/api/worker/sync", {
      method: "POST",
      deviceToken: this.h.deviceToken(),
      body: { events: batch.map(toWire) },
      timeoutMs: 30000,
    });
    const settled = resp.events.filter(isSettled).map((o) => o.id);
    await store.outboxRemove(settled);
    await this.h.onResult(batch, resp);
    if (all.length <= batch.length) await this.uploadPhotos();
    return all.length > batch.length;
  }

  /**
   * Photos go up after the flag they belong to has synced (the server 409s
   * until then). A photo the server refuses outright (too big, not an
   * image) is dropped rather than retried forever.
   */
  async uploadPhotos() {
    const photos = await store.photosAll();
    for (const p of photos) {
      try {
        await request(`/api/worker/photos?id=${encodeURIComponent(p.id)}&flag_id=${encodeURIComponent(p.flag_id)}`, {
          method: "POST",
          deviceToken: this.h.deviceToken(),
          blob: p.blob,
          timeoutMs: 60000,
        });
        await store.photoRemove(p.id);
      } catch (e) {
        if (e.isNetwork || e.status >= 500 || e.status === 429) throw e;
        // Its flag never made it (the order was cancelled meanwhile): give up after a few days.
        if (e.code === "flag_not_synced" && Date.now() - (p.created || 0) < 3 * 86400000) continue;
        await store.photoRemove(p.id);
        if (this.h.onPhotoRejected) this.h.onPhotoRejected(e);
      }
    }
  }

  /** Try hard to empty the outbox (used at end of shift). Resolves with what's left. */
  async flush(timeoutMs = 6000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const left = (await store.outboxCount().catch(() => 1)) + (await store.photoCount().catch(() => 1));
      if (!left) return 0;
      if (this.syncing) {
        await new Promise((r) => setTimeout(r, 150));
        continue;
      }
      try {
        this.syncing = true;
        await this.syncOnce();
      } catch {
        return this.leftCount();
      } finally {
        this.syncing = false;
      }
    }
    return this.leftCount();
  }

  async leftCount() {
    return (await store.outboxCount().catch(() => 1)) + (await store.photoCount().catch(() => 0));
  }
}
