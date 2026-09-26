// Thin fetch wrapper for the Autorack API.
//
// The API origin comes from /config.js (window.AUTORACK_CONFIG.apiBase),
// which the Cloudflare Pages build writes from AUTORACK_API_BASE. Empty means
// same origin, which is how local development and single-host deploys work.

export const API_BASE = ((typeof window !== "undefined" && window.AUTORACK_CONFIG && window.AUTORACK_CONFIG.apiBase) || "")
  .replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(status, code, message, detail) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail || {};
  }

  get isNetwork() {
    return this.status === 0;
  }
}

/**
 * request("/api/orders", {method, body, token, deviceToken, form, raw, timeoutMs})
 * - body: JSON-serialized. form: a FormData (multipart upload). blob: raw bytes.
 * - raw: resolve with the Response itself (file downloads).
 * Rejects with ApiError; status 0 means the network is unreachable.
 */
export async function request(path, opts = {}) {
  const { method = "GET", body, token, deviceToken, form, blob, raw = false, timeoutMs = 20000 } = opts;
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (deviceToken) headers["X-Device-Token"] = deviceToken;
  let payload;
  if (form) {
    payload = form;
  } else if (blob) {
    headers["Content-Type"] = blob.type || "application/octet-stream";
    payload = blob;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(API_BASE + path, { method, headers, body: payload, signal: controller.signal, cache: "no-store" });
  } catch (e) {
    throw new ApiError(0, "network", "No connection to the server.", { cause: String(e) });
  } finally {
    clearTimeout(timer);
  }
  if (raw && resp.ok) return resp;
  let data = null;
  const text = await resp.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!resp.ok) {
    const d = (data && data.detail) || {};
    const message = typeof d === "string" ? d : d.message || `Request failed (${resp.status})`;
    const code = (typeof d === "object" && d.code) || (resp.status >= 500 ? "server_error" : "error");
    throw new ApiError(resp.status, code, message, typeof d === "object" ? d : {});
  }
  return data;
}

/** Trigger a browser download of an authenticated file endpoint. */
export async function download(path, token, fallbackName) {
  const resp = await request(path, { token, raw: true, timeoutMs: 120000 });
  const disposition = resp.headers.get("Content-Disposition") || "";
  const match = /filename="([^"]+)"/.exec(disposition);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = match ? match[1] : fallbackName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/** Fetch an authenticated image (a worker's photo) as an object URL. */
export async function imageUrl(path, token) {
  const resp = await request(path, { token, raw: true, timeoutMs: 60000 });
  return URL.createObjectURL(await resp.blob());
}
