// Small DOM helpers. No framework, no build step.
//
// Everything user- or scanner-controlled goes into the page as text nodes via
// h(); nothing here assigns innerHTML. The only markup we insert is the QR
// SVG the API generates, parsed as XML by svg() rather than as HTML.

/**
 * h("button", {class: "btn", onclick: fn, disabled: true}, "Save")
 * attrs: class, dataset {}, style {} (CSSOM, allowed under our CSP),
 * on<event> functions, booleans, and plain attributes.
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = Array.isArray(v) ? v.filter(Boolean).join(" ") : v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (k === "value" && "value" in el) el.value = v;
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}

/** Parse trusted SVG markup (our API's QR codes) into a node. */
export function svg(markup, className) {
  // Inline SVG (e.g. segno's) often omits xmlns; parsed as XML without it,
  // the element isn't SVG and renders as nothing.
  const src = /<svg[^>]*\sxmlns=/.test(markup) ? markup : markup.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"');
  const doc = new DOMParser().parseFromString(src, "image/svg+xml");
  const node = document.importNode(doc.documentElement, true);
  if (node.nodeName.toLowerCase() !== "svg") return h("span");
  for (const el of node.querySelectorAll("script, foreignObject")) el.remove();
  if (className) node.setAttribute("class", className);
  return node;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function fmtNumber(n) {
  return n === null || n === undefined ? "–" : new Intl.NumberFormat().format(n);
}

export function fmtPercent(x, digits = 1) {
  return x === null || x === undefined ? "–" : `${(x * 100).toFixed(digits)}%`;
}

export function fmtMoney(cents, currency = "usd") {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: currency.toUpperCase(), maximumFractionDigits: 0 })
    .format(cents / 100);
}

export function fmtDateTime(iso, timeZone) {
  if (!iso) return "–";
  const opts = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  if (timeZone) opts.timeZone = timeZone;
  try {
    return new Intl.DateTimeFormat(undefined, opts).format(new Date(iso));
  } catch {
    return new Date(iso).toLocaleString();
  }
}

export function fmtDate(iso, timeZone) {
  if (!iso) return "–";
  const opts = { year: "numeric", month: "short", day: "numeric" };
  if (timeZone) opts.timeZone = timeZone;
  return new Intl.DateTimeFormat(undefined, opts).format(new Date(iso));
}

export function fmtAgo(iso, now = Date.now()) {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// ---------------------------------------------------------------------------
// Toasts and dialogs
// ---------------------------------------------------------------------------

let toastHost = null;

export function toast(message, kind = "info", ms = 4000) {
  if (!toastHost) {
    toastHost = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(toastHost);
  }
  const t = h("div", { class: `toast toast-${kind}` }, message);
  toastHost.appendChild(t);
  setTimeout(() => {
    t.classList.add("toast-out");
    setTimeout(() => t.remove(), 300);
  }, ms);
}

/**
 * Modal dialog built on <dialog>. Resolves with the value passed to close(),
 * or null when dismissed. `render(close)` returns the body content.
 */
export function dialog(title, render, { wide = false } = {}) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: ["dialog", wide && "dialog-wide"] });
    let settled = false;
    const close = (value = null) => {
      if (settled) return;
      settled = true;
      dlg.close();
      dlg.remove();
      resolve(value);
    };
    dlg.addEventListener("cancel", (e) => {
      e.preventDefault();
      close(null);
    });
    mount(
      dlg,
      h("div", { class: "dialog-head" },
        h("h2", null, title),
        h("button", { class: "icon-btn", "aria-label": "Close", onclick: () => close(null) }, "✕")),
      h("div", { class: "dialog-body" }, render(close)),
    );
    document.body.appendChild(dlg);
    dlg.showModal();
    const first = dlg.querySelector("input, select, textarea, button.btn-primary");
    if (first) first.focus();
  });
}

export function confirmDialog(title, message, { confirmLabel = "Confirm", danger = false } = {}) {
  return dialog(title, (close) => [
    h("p", null, message),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", onclick: () => close(false) }, "Cancel"),
      h("button", { class: ["btn", danger ? "btn-danger" : "btn-primary"], onclick: () => close(true) }, confirmLabel)),
  ]).then((v) => v === true);
}

export const BARCODE_ICON =
  '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="3" y="5" width="2" height="14"/>' +
  '<rect x="7" y="5" width="1" height="14"/><rect x="10" y="5" width="2" height="14"/><rect x="14" y="5" width="1" height="14"/>' +
  '<rect x="17" y="5" width="1.5" height="14"/><rect x="20" y="5" width="1" height="14"/></svg>';

export function brandMark() {
  return h("span", { class: "brand-mark" }, svg(BARCODE_ICON));
}

export function uuid4() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
