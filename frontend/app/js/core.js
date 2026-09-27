// Owner dashboard plumbing: session token, API access, layout, routing.

import { ApiError, imageUrl, download as rawDownload, request } from "../../shared/api.js";
import { brandLockup, dialog, h, mount, toast } from "../../shared/dom.js";
import { LANGUAGES, T, getLang, setLang } from "./i18n.js";

const TOKEN_KEY = "ar.owner_token";

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked: session lasts for this page only */
  }
}

export function toLogin() {
  setToken(null);
  const next = encodeURIComponent(location.hash || "");
  location.replace(`/app/login.html${next ? `?next=${next}` : ""}`);
}

/** API call as the signed-in owner. 401 sends them to sign in. */
export async function api(path, opts = {}) {
  try {
    return await request(path, { ...opts, token: getToken() });
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      toLogin();
      return new Promise(() => {}); // navigation is under way
    }
    if (e instanceof ApiError && e.code === "agreement_required" && !path.startsWith("/api/agreement")) {
      // A new version of the agreement was published: the boot gate shows it.
      location.reload();
      return new Promise(() => {});
    }
    throw e;
  }
}

export function download(path, name) {
  return rawDownload(path, getToken(), name).catch((e) => toast(e.message, "bad"));
}

/** Show an API error the way every view should: a toast, never a blank page. */
export function fail(e) {
  toast(e instanceof ApiError ? e.message : String(e), "bad", 6000);
}

// ---------------------------------------------------------------------------
// Session context (who, which warehouse, can they scan)
// ---------------------------------------------------------------------------

export const ctx = { me: null };

export async function loadMe() {
  ctx.me = await api("/api/auth/me");
  return ctx.me;
}

export function tz() {
  return ctx.me && ctx.me.warehouse ? ctx.me.warehouse.timezone : undefined;
}

export function role() {
  return ctx.me && ctx.me.membership ? ctx.me.membership.role : null;
}

/** Billing, warehouse settings, the team. */
export function isOwner() {
  return role() === "owner";
}

/** Create and edit orders, workers, phones and barcode rules. Supervisors can't. */
export function canManage() {
  return role() === "owner" || role() === "manager";
}

export async function switchWarehouse(id) {
  await api("/api/auth/switch", { method: "POST", body: { warehouse_id: id } });
  location.hash = "#/";
  location.reload();
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

/** Dashboard language: English, Español, 中文, Tiếng Việt. */
export function languagePicker() {
  return h("select", {
    class: "input input-inline lang-select", "aria-label": T("Language"),
    onchange: (e) => setLang(e.target.value),
  }, ...LANGUAGES.map(([code, name]) => h("option", { value: code, selected: code === getLang(), lang: code }, name)));
}

/** The sidebar, grouped by what you're doing. */
function navGroups() {
  const wh = ctx.me.warehouse;
  return [
    [null, [["#/", T("Dashboard")]]],
    [T("Work"), [
      ["#/orders", T("Orders")],
      ["#/restock", T("Restock")],
      wh.leaderboard_enabled ? ["#/board", T("Floor board")] : null,
    ]],
    [T("Products"), [["#/products", T("Catalog")], ["#/inserts", T("Pack inserts")]]],
    [T("Team"), [["#/workers", T("Workers")], ["#/time", T("Time clock")], ["#/devices", T("Phones")]]],
    [T("Results"), [["#/reports", T("Reports")], ["#/insights", T("Insights")]]],
    [T("Setup"), [
      ["#/clients", T("Clients")],
      ["#/connections", T("Connections")],
      isOwner() ? ["#/billing", T("Billing")] : null,
      ["#/settings", T("Settings")],
    ]],
  ].map(([title, links]) => [title, links.filter(Boolean)]);
}

function warehousePicker(me) {
  if (me.warehouses.length < 2) {
    return h("div", { class: "sidebar-wh", title: me.warehouse.name }, me.warehouse.name);
  }
  return h("select", {
    class: "sidebar-wh sidebar-wh-select",
    "aria-label": T("Warehouse"),
    onchange: (e) => switchWarehouse(e.target.value).catch(fail),
  }, ...me.warehouses.map((w) => h("option", { value: w.id, selected: w.id === me.warehouse.id }, w.name)));
}

export function accessBanner() {
  const a = ctx.me && ctx.me.access;
  if (!a) return null;
  if (a.state === "closed") {
    const due = ctx.me.warehouse.deletion_due_at;
    return h("div", { class: "banner banner-bad app-banner" },
      T("This account is closed. Scanning is off"),
      due ? T(", and its data will be permanently deleted on {p0}.", { p0: new Date(due).toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" }) }) + " " : ". ",
      h("a", { href: "#/settings" }, isOwner() ? T("Download your data or reopen →") : T("Details →")));
  }
  if (!a.allowed) {
    return h("div", { class: "banner banner-bad app-banner" },
      a.message, " ", isOwner() ? h("a", { href: "#/billing" }, T("Go to billing →")) : null);
  }
  if (a.state === "grace") {
    return h("div", { class: "banner banner-warn app-banner" }, a.message, " ", h("a", { href: "#/billing" }, T("Update payment →")));
  }
  if (a.state === "trialing" && a.trial_days_left !== null && a.trial_days_left <= 5) {
    return h("div", { class: "banner banner-info app-banner" },
      T("Free trial: {trial_days_left} day{p1} left.", { trial_days_left: a.trial_days_left, p1: a.trial_days_left === 1 ? "" : "s" }) + " ",
      h("a", { href: "#/billing" }, T("Subscribe →")));
  }
  return null;
}

export function layout(active, content) {
  const root = document.getElementById("app");
  const me = ctx.me;
  const link = ([href, label]) =>
    h("a", { href, class: ["nav-link", active === href && "active"], "aria-current": active === href ? "page" : null }, label);
  const navLinks = navGroups().map(([title, links]) => h("div", { class: "nav-group" },
    title ? h("div", { class: "nav-group-title" }, title) : null, ...links.map(link)));
  mount(root,
    h("div", { class: "shell" },
      h("aside", { class: "sidebar" },
        h("div", { class: "sidebar-brand" }, brandLockup({ tagline: true, href: "#/" }),
          h("button", {
            class: "btn btn-sm nav-toggle", type: "button", "aria-expanded": "false",
            onclick: (e) => {
              const open = e.currentTarget.closest(".sidebar").classList.toggle("nav-open");
              e.currentTarget.setAttribute("aria-expanded", String(open));
            },
          }, T("☰ Menu"))),
        warehousePicker(me),
        h("nav", { class: "nav" }, ...navLinks),
        h("div", { class: "sidebar-foot" },
          me.is_operator ? h("a", { class: "small", href: "/admin/" }, T("Operator console →")) : null,
          h("div", { class: "small muted", title: me.user.email }, me.user.email,
            role() && role() !== "owner" ? ` · ${role()}` : ""),
          h("a", { class: ["small", "help-link", active === "#/help" && "active"], href: "#/help" }, T("Help & support")),
          h("button", { class: "link-btn small", onclick: logout }, T("Sign out")),
          languagePicker())),
      h("main", { class: "main", id: "main" }, topBar(), accessBanner(), content)));
}

// ---------------------------------------------------------------------------
// Top bar: search everything (Ctrl+K), and "+ New"
// ---------------------------------------------------------------------------

/** What "+ New" and the command palette can create. */
function newItems() {
  if (!canManage()) return [];
  return [
    [T("New order"), "#/orders/new"],
    [T("Import orders (CSV)"), "#/orders/import"],
    [T("New receipt (receiving)"), "#/orders/new?kind=receive"],
    [T("New cycle count"), "#/orders/new?kind=count"],
    [T("New product"), "#/products?new=1"],
    [T("Add a worker"), "#/workers?new=1"],
    [T("Link a phone or pack station"), "#/devices"],
    [T("New pack insert"), "#/inserts?new=1"],
    [T("New client"), "#/clients?new=1"],
  ];
}

function topBar() {
  const items = newItems();
  const menu = items.length ? h("details", { class: "new-menu" },
    h("summary", { class: "btn btn-primary" }, T("+ New")),
    h("div", { class: "new-menu-list", role: "menu" }, ...items.map(([label, href]) =>
      h("a", { href, role: "menuitem", onclick: (e) => e.target.closest("details").removeAttribute("open") }, label)))) : null;
  return h("div", { class: "app-top" },
    h("button", { class: "search-trigger", type: "button", onclick: openPalette },
      h("span", { class: "search-icon", "aria-hidden": "true" }, "⌕"),
      h("span", null, T("Search orders, tracking, products, lots…")),
      h("kbd", null, navigator.platform && /Mac/.test(navigator.platform) ? "⌘K" : T("Ctrl K"))),
    menu);
}

/** Every page, for jumping around from the palette. */
function pageItems() {
  return [...navGroups(), [null, [["#/help", T("Help & support")]]]]
    .flatMap(([group, links]) => links.map(([href, label]) => [`Go to ${label}`, href, group || ""]));
}

let paletteOpen = false;

export function openPalette() {
  if (paletteOpen) return;
  paletteOpen = true;
  let active = 0;
  let items = [];
  let timer = null;
  let seq = 0;
  const input = h("input", {
    class: "input palette-input", type: "search", autocomplete: "off", spellcheck: "false",
    placeholder: T("Search, or type a command (new order, restock, settings…)"), "aria-label": T("Search"),
  });
  const list = h("div", { class: "palette-list", role: "listbox" });
  const staticItems = () => [
    ...newItems().map(([label, href]) => ({ label, href, group: T("Create") })),
    ...pageItems().map(([label, href, group]) => ({ label, href, group: group ? T("Pages · {group}", { group }) : T("Pages") })),
  ];
  const render = () => {
    active = Math.max(0, Math.min(active, items.length - 1));
    let lastGroup = null;
    const rows = [];
    items.forEach((it, i) => {
      if (it.group !== lastGroup) {
        rows.push(h("div", { class: "palette-group" }, it.group));
        lastGroup = it.group;
      }
      rows.push(h("a", {
        class: ["palette-item", i === active && "active"], href: it.href, role: "option",
        onmouseenter: () => { active = i; render(); },
        onclick: () => close(),
      }, h("span", null, it.label), it.hint ? h("span", { class: "muted small" }, it.hint) : null));
    });
    mount(list, ...(rows.length ? rows : [h("p", { class: "muted palette-empty" }, T("Nothing found."))]));
    const el = list.querySelector(".palette-item.active");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  };
  const filterStatic = (q) => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    return staticItems().filter((it) => words.every((w) => it.label.toLowerCase().includes(w)));
  };
  const search = async (q) => {
    const mine = ++seq;
    let r;
    try {
      r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    } catch {
      return;
    }
    if (mine !== seq) return;
    const found = [
      ...r.orders.map((o) => ({
        group: T("Orders"), href: `#/orders/${o.id}`, label: o.number || o.id.slice(0, 8),
        hint: [o.kind !== "pick" ? o.kind : null, o.status.replace("_", " "), o.customer, o.tracking_number].filter(Boolean).join(" · "),
      })),
      ...r.products.map((p) => ({ group: T("Products"), href: `#/products/${p.id}`, label: p.name, hint: [p.sku, p.barcode].filter(Boolean).join(" · ") })),
      ...r.workers.map((w) => ({ group: T("Workers"), href: "#/workers", label: w.name, hint: w.active ? "" : "inactive" })),
      ...r.clients.map((c) => ({ group: T("Clients"), href: `#/clients/${c.id}`, label: c.name })),
    ];
    items = [...found, ...filterStatic(q)];
    render();
  };
  input.addEventListener("input", () => {
    const q = input.value.trim();
    active = 0;
    items = q ? filterStatic(q) : staticItems();
    render();
    clearTimeout(timer);
    if (q) timer = setTimeout(() => search(q), 180);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active += 1; render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active -= 1; render(); e.preventDefault(); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const it = items[active];
      if (it) { location.hash = it.href; close(); }
      else if (input.value.trim()) { location.hash = `#/orders?status=&q=${encodeURIComponent(input.value.trim())}`; close(); }
    }
  });
  const dlg = h("dialog", { class: "dialog palette", "aria-label": T("Search") }, input, list,
    h("div", { class: "palette-foot muted small" }, T("↑↓ to move · Enter to open · Esc to close")));
  function close() {
    if (!paletteOpen) return;
    paletteOpen = false;
    clearTimeout(timer);
    dlg.close();
    dlg.remove();
  }
  dlg.addEventListener("close", () => { if (paletteOpen) close(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
  document.body.appendChild(dlg);
  items = staticItems();
  render();
  dlg.showModal();
  input.focus();
}

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
    if (!ctx.me || !ctx.me.warehouse || !document.getElementById("main")) return;
    e.preventDefault();
    openPalette();
  } else if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "")
    && document.getElementById("main") && !document.querySelector("dialog[open]")) {
    e.preventDefault();
    openPalette();
  }
});

export async function logout() {
  try {
    await request("/api/auth/logout", { method: "POST", token: getToken() });
  } catch {
    /* signing out locally is what matters */
  }
  setToken(null);
  location.replace("/app/login.html");
}

/** `?new=1` from "+ New" or the palette: open the create dialog once, then drop the flag. */
export function wantsNew(params) {
  if (!params || params.get("new") !== "1") return false;
  const [path] = location.hash.split("?");
  history.replaceState(null, "", path);
  return canManage();
}

export function pageHeader(title, subtitle, ...actions) {
  return h("div", { class: "page-head" },
    h("div", null, h("h1", null, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
    actions.length ? h("div", { class: "row" }, ...actions) : null);
}

export function card(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", { class: "card-title" }, title) : null, ...children);
}

export function table(columns, rows, { empty = T("Nothing here yet."), onRow } = {}) {
  if (!rows.length) return h("div", { class: "empty" }, empty);
  return h("div", { class: "table-wrap" },
    h("table", { class: "table" },
      h("thead", null, h("tr", null, ...columns.map((c) => h("th", { class: c.align ? `t-${c.align}` : null }, c.label)))),
      h("tbody", null, ...rows.map((r) =>
        h("tr", { class: onRow ? "clickable" : null, onclick: onRow ? () => onRow(r) : null },
          ...columns.map((c) => h("td", { class: c.align ? `t-${c.align}` : null }, c.render ? c.render(r) : r[c.key])))))));
}

const STATUS_LABELS = {
  pending: T("Not started"),
  in_progress: T("In progress"),
  flagged: T("Flagged"),
  completed: T("Completed"),
  finished: T("Finished"),
  shipped: T("Shipped"),
  cancelled: T("Cancelled"),
};

export function statusBadge(status) {
  return h("span", { class: `badge badge-${status}` }, STATUS_LABELS[status] || String(status).replace("_", " "));
}

// ---------------------------------------------------------------------------
// Worker photos: fetched with the session token, shown as thumbnails
// ---------------------------------------------------------------------------

/** Thumbnails for photo ids; click one to see it full size. `base` lets the
 *  operator console reuse this with its own endpoint. */
export function photoStrip(ids, { base = "/api/photos/" } = {}) {
  if (!ids || !ids.length) return null;
  return h("div", { class: "photo-strip" }, ...ids.map((id) => photoThumb(id, base)));
}

export function photoThumb(id, base = "/api/photos/", caption = null) {
  const img = h("img", { class: "photo-thumb", alt: T("Photo from the floor"), loading: "lazy" });
  const btn = h("button", { class: "photo-btn", type: "button", onclick: () => openPhoto(img.src, caption), "aria-label": T("Open photo") }, img);
  imageUrl(`${base}${id}`, getToken()).then((url) => { img.src = url; }, () => btn.classList.add("photo-missing"));
  return btn;
}

function openPhoto(src, caption) {
  if (!src) return;
  dialog(T("Photo from the floor"), (close) => [
    h("img", { class: "photo-full", src, alt: caption || T("Photo from the floor") }),
    caption ? h("p", { class: "muted small" }, caption) : null,
    h("div", { class: "dialog-actions" },
      h("a", { class: "btn", href: src, download: "autorack-photo.jpg" }, T("Download")),
      h("button", { class: "btn btn-primary", onclick: () => close(true) }, T("Close"))),
  ], { wide: true });
}

// ---------------------------------------------------------------------------
// Polling that stops when the tab is hidden or the view changes
// ---------------------------------------------------------------------------

let activePoll = null;

export function poll(fn, intervalMs) {
  stopPolling();
  let timer = null;
  const state = { stopped: false };
  const tick = async () => {
    if (state.stopped) return;
    if (document.visibilityState === "visible") {
      try {
        await fn();
      } catch {
        /* next tick will retry */
      }
    }
    if (!state.stopped) timer = setTimeout(tick, intervalMs);
  };
  activePoll = () => {
    state.stopped = true;
    clearTimeout(timer);
  };
  timer = setTimeout(tick, intervalMs);
}

export function stopPolling() {
  if (activePoll) activePoll();
  activePoll = null;
}
