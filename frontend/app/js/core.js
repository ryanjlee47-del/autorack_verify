// Owner dashboard plumbing: session token, API access, layout, routing.

import { ApiError, download as rawDownload, request } from "../../shared/api.js";
import { brandLockup, h, mount, toast } from "../../shared/dom.js";

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
  return ctx.me ? ctx.me.warehouse.timezone : undefined;
}

export function isOwner() {
  return ctx.me && ctx.me.user.role === "owner";
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

const NAV = [
  ["#/", "Dashboard"],
  ["#/orders", "Orders"],
  ["#/workers", "Workers"],
  ["#/devices", "Phones"],
  ["#/insights", "Insights"],
  ["#/billing", "Billing"],
  ["#/settings", "Settings"],
];

export function accessBanner() {
  const a = ctx.me && ctx.me.access;
  if (!a) return null;
  if (!a.allowed) {
    return h("div", { class: "banner banner-bad app-banner" },
      a.message, " ", h("a", { href: "#/billing" }, "Go to billing →"));
  }
  if (a.state === "grace") {
    return h("div", { class: "banner banner-warn app-banner" }, a.message, " ", h("a", { href: "#/billing" }, "Update payment →"));
  }
  if (a.state === "trialing" && a.trial_days_left !== null && a.trial_days_left <= 5) {
    return h("div", { class: "banner banner-info app-banner" },
      `Free trial: ${a.trial_days_left} day${a.trial_days_left === 1 ? "" : "s"} left. `,
      h("a", { href: "#/billing" }, "Subscribe →"));
  }
  return null;
}

export function layout(active, content) {
  const root = document.getElementById("app");
  const me = ctx.me;
  const navLinks = NAV.map(([href, label]) =>
    h("a", { href, class: ["nav-link", active === href && "active"], "aria-current": active === href ? "page" : null }, label));
  mount(root,
    h("div", { class: "shell" },
      h("aside", { class: "sidebar" },
        h("div", { class: "sidebar-brand" }, brandLockup({ tagline: true, href: "#/" })),
        h("div", { class: "sidebar-wh", title: me.warehouse.name }, me.warehouse.name),
        h("nav", { class: "nav" }, ...navLinks),
        h("div", { class: "sidebar-foot" },
          h("div", { class: "small muted", title: me.user.email }, me.user.email),
          h("button", { class: "link-btn small", onclick: logout }, "Sign out"))),
      h("main", { class: "main", id: "main" }, accessBanner(), content)));
}

export async function logout() {
  try {
    await request("/api/auth/logout", { method: "POST", token: getToken() });
  } catch {
    /* signing out locally is what matters */
  }
  setToken(null);
  location.replace("/app/login.html");
}

export function pageHeader(title, subtitle, ...actions) {
  return h("div", { class: "page-head" },
    h("div", null, h("h1", null, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
    actions.length ? h("div", { class: "row" }, ...actions) : null);
}

export function card(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", { class: "card-title" }, title) : null, ...children);
}

export function table(columns, rows, { empty = "Nothing here yet.", onRow } = {}) {
  if (!rows.length) return h("div", { class: "empty" }, empty);
  return h("div", { class: "table-wrap" },
    h("table", { class: "table" },
      h("thead", null, h("tr", null, ...columns.map((c) => h("th", { class: c.align ? `t-${c.align}` : null }, c.label)))),
      h("tbody", null, ...rows.map((r) =>
        h("tr", { class: onRow ? "clickable" : null, onclick: onRow ? () => onRow(r) : null },
          ...columns.map((c) => h("td", { class: c.align ? `t-${c.align}` : null }, c.render ? c.render(r) : r[c.key])))))));
}

export function statusBadge(status) {
  return h("span", { class: `badge badge-${status}` }, String(status).replace("_", " "));
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
