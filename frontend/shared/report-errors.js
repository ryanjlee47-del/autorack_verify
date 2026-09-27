// Report our own pages' JavaScript errors to the server, which records them
// and emails the operators. At most a few reports per page load, and never
// anything typed into forms: just the error, where it happened, and the page.

import { API_BASE } from "./api.js";

const MAX_REPORTS = 5;
let sent = 0;
const seen = new Set();

function send(app, message, stack, source, line) {
  if (sent >= MAX_REPORTS || !message) return;
  const key = `${message}|${source}|${line}`;
  if (seen.has(key)) return;
  seen.add(key);
  sent += 1;
  const body = JSON.stringify({
    app,
    message: String(message).slice(0, 1000),
    stack: stack ? String(stack).slice(0, 8000) : null,
    source: source ? String(source).slice(0, 300) : null,
    line: Number.isFinite(line) ? line : null,
    page: location.pathname + location.hash.split("?")[0],
  });
  try {
    fetch(`${API_BASE}/api/client-errors`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true,
    }).catch(() => {});
  } catch {
    /* reporting must never cause another error */
  }
}

export function reportErrors(app) {
  window.addEventListener("error", (e) => {
    const err = e.error;
    send(app, (err && err.message) || e.message, err && err.stack, e.filename, e.lineno);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    // Our API errors are expected outcomes (offline, 4xx), not bugs.
    if (r && (r.name === "ApiError" || r.status !== undefined)) return;
    send(app, (r && r.message) || String(r), r && r.stack, null, null);
  });
}
