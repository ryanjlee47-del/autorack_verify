// Service worker for the worker PWA. Caches the app shell (JS/CSS/icons +
// the scan page markup itself) so a phone that goes into airplane mode
// mid-shift can still reload the page and keep scanning -- the shift
// data itself lives in IndexedDB (see idb.js/app.js), not here.
//
// /w/bundle, /w/sync, /w/heartbeat are deliberately network-only: caching
// them would risk serving stale data as if it were current, which is
// exactly the kind of silent staleness this whole architecture exists to
// avoid.
var CACHE_NAME = "autorack-shell-v3";
var SHELL_ASSETS = [
  "/static/css/tokens.css",
  "/static/css/worker.css",
  "/static/js/barcode.js",
  "/static/js/vendor/zxing.min.js",
  "/static/js/worker/i18n.js",
  "/static/js/worker/idb.js",
  "/static/js/worker/feedback.js",
  "/static/js/worker/scanner.js",
  "/static/js/worker/outbox.js",
  "/static/js/worker/appeals.js",
  "/static/js/worker/app.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/w/manifest.webmanifest",
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(SHELL_ASSETS);
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) {
          return k !== CACHE_NAME;
        }).map(function (k) {
          return caches.delete(k);
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

function isNetworkOnly(url) {
  return url.pathname.startsWith("/w/bundle/") || url.pathname === "/w/sync" || url.pathname.startsWith("/w/heartbeat/");
}

// Anything passed to event.respondWith() MUST resolve to a real Response.
// caches.match() resolves to *undefined* on a miss, so any branch that
// ends in a bare caches.match() -- or in a variable that might hold an
// uncached value -- silently hands respondWith() undefined. Safari
// surfaces that to the worker as:
//
//   "FetchEvent.respondWith received an error: Returned response is null"
//
// which reads on the phone as "Safari can't open the page", and looks
// like a server or network outage when it is neither.
//
// The trigger in practice is scanning a door QR: /w/join?t=TOKEN carries a
// fresh token every shift, cache lookups match on the full URL including
// the query string, so a newly-scanned token is never a cache hit. If the
// network fetch then fails for any reason -- flaky dock wifi, or (very
// commonly on iOS) serve.py's self-signed dev cert, which Safari will let
// a human click through for a top-level navigation but will still reject
// for fetch() inside a service worker -- the miss and the failure land
// together and the handler returns nothing at all.
//
// Deliberately NOT fixed with {ignoreSearch: true}: the join page has the
// shift token baked into its form and the scan page has data-session-id
// baked into its body, so serving a cached page from a *different* token
// would attach a worker's scans to someone else's session. A confusing
// error is bad; silently misattributed scans would be far worse. The fix
// is to always return a real Response, never to loosen the match.
function offlineFallback(message) {
  return new Response(
    '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      "<title>Offline -- Autorack Verify</title></head>" +
      '<body style="font-family:system-ui,-apple-system,sans-serif;background:#14171a;' +
      'color:#f4f3ef;margin:0;padding:2rem;display:flex;flex-direction:column;' +
      'align-items:center;justify-content:center;min-height:100vh;text-align:center;">' +
      '<h1 style="font-size:1.5rem;margin:0 0 0.75rem;">No connection</h1>' +
      '<p style="opacity:0.85;max-width:22rem;line-height:1.5;">' + message + "</p>" +
      '<button onclick="location.reload()" style="margin-top:1.5rem;padding:0.9em 1.8em;' +
      "font-size:1.1rem;font-weight:700;background:#f4b400;color:#14171a;border:none;" +
      'border-radius:6px;">Try again</button>' +
      "</body></html>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (event.request.method !== "GET" || isNetworkOnly(url)) {
    return; // let the browser handle it directly against the network
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/w/manifest.webmanifest") {
    event.respondWith(
      caches.match(event.request).then(function (cached) {
        var networkFetch = fetch(event.request).then(function (resp) {
          if (resp.ok) {
            var copy = resp.clone();
            caches.open(CACHE_NAME).then(function (cache) {
              cache.put(event.request, copy);
            });
          }
          return resp;
        }).catch(function () {
          // `cached` is undefined on a cache miss -- returning it here is
          // what handed respondWith() nothing. A 503 is a truthful answer
          // for a subresource we can neither serve nor fetch.
          return cached || new Response("", { status: 503, statusText: "Offline" });
        });
        return cached || networkFetch;
      })
    );
    return;
  }

  if (url.pathname.startsWith("/w/scan") || url.pathname.startsWith("/w/join") || url.pathname === "/w") {
    event.respondWith(
      fetch(event.request)
        .then(function (resp) {
          var copy = resp.clone();
          caches.open(CACHE_NAME).then(function (cache) {
            cache.put(event.request, copy);
          });
          return resp;
        })
        .catch(function () {
          return caches.match(event.request).then(function (cached) {
            if (cached) {
              return cached;
            }
            // Joining needs the server (it validates the shift token and
            // creates the session), so there is nothing useful to serve
            // offline -- say that plainly instead of returning null.
            if (url.pathname.startsWith("/w/join") || url.pathname === "/w") {
              return offlineFallback(
                "Joining a shift needs a connection. Move closer to the dock wifi " +
                  "and try again, or ask a supervisor to check the scanner network."
              );
            }
            return offlineFallback(
              "This scan session hasn't been loaded on this phone yet, so it " +
                "can't be opened offline. Reconnect and try again -- any scans " +
                "already queued on this device are safe and will sync."
            );
          });
        })
    );
  }
});
