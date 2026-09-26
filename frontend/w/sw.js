// Service worker for the worker PWA: keeps the app shell available offline.
//
// Strategy: network first (so a deploy reaches phones on their next load),
// falling back to the cache after a short timeout or when offline. Order data
// and the scan queue live in IndexedDB, never here, and API calls are never
// intercepted: a cached API response could show stale data as if it were live.

const CACHE = "autorack-shell-v4";
const SHELL = [
  "/w/",
  "/w/index.html",
  "/w/manifest.webmanifest",
  "/w/js/app.js",
  "/w/js/state.js",
  "/w/js/store.js",
  "/w/js/sync.js",
  "/w/js/scanner.js",
  "/w/js/feedback.js",
  "/w/js/i18n.js",
  "/w/js/photo.js",
  "/w/vendor/zxing.min.js",
  "/shared/api.js",
  "/shared/dom.js",
  "/shared/barcode.js",
  "/assets/css/tokens.css",
  "/assets/css/components.css",
  "/assets/css/worker.css",
  "/assets/icons/icon-192.png",
  "/assets/icons/favicon.svg",
  "/assets/brand/mark.svg",
  "/assets/brand/mark-reverse.svg",
  "/assets/fonts/inter-latin-wght-normal.woff2",
  "/config.js",
];
const NETWORK_TIMEOUT_MS = 3500;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function isShellRequest(url) {
  return url.origin === self.location.origin && !url.pathname.startsWith("/api/");
}

// Whatever happens, respondWith() must get a real Response: a bare cache miss
// resolves to undefined, which Safari reports as "Safari can't open the page".
async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  const key = new URL(request.url);
  key.search = ""; // ?link=CODE etc. must still hit the cached page
  try {
    const response = await Promise.race([
      fetch(request),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), NETWORK_TIMEOUT_MS)),
    ]);
    if (response.ok && request.method === "GET") cache.put(key.toString(), response.clone());
    return response;
  } catch (err) {
    const cached = (await cache.match(key.toString())) ||
      (request.mode === "navigate" ? await cache.match("/w/") : undefined);
    if (cached) return cached;
    return new Response("Offline and not cached yet. Connect once and reload.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !isShellRequest(url)) return;
  event.respondWith(networkFirst(event.request));
});
