// Owner dashboard entry point: hash router.

import { h, mount } from "../../shared/dom.js";
import { ctx, fail, getToken, loadMe, logout, stopPolling, toLogin } from "./core.js";
import { dashboardView } from "./views/dashboard.js";
import { billingView, insightsView, settingsView } from "./views/more.js";
import { boardView, reportsView } from "./views/reports.js";
import { importView, newOrderView, orderDetailView, ordersView } from "./views/orders.js";
import { devicesView, workersView } from "./views/people.js";

const ROUTES = [
  [/^\/?$/, () => dashboardView()],
  [/^\/orders$/, (m, p) => ordersView(p)],
  [/^\/orders\/new$/, () => newOrderView()],
  [/^\/orders\/import$/, () => importView()],
  [/^\/orders\/([0-9a-f-]{36})$/, (m) => orderDetailView(m[1])],
  [/^\/workers$/, (m, p) => workersView(p)],
  [/^\/devices$/, () => devicesView()],
  [/^\/insights$/, (m, p) => insightsView(p)],
  [/^\/reports$/, (m, p) => reportsView(p)],
  [/^\/board$/, (m, p) => boardView(p)],
  [/^\/billing$/, (m, p) => billingView(p)],
  [/^\/settings$/, () => settingsView()],
];

async function route() {
  stopPolling();
  document.body.classList.remove("tv");
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  for (const [re, view] of ROUTES) {
    const m = re.exec(path);
    if (m) {
      try {
        await view(m, params);
      } catch (e) {
        fail(e);
        const main = document.getElementById("main");
        if (main) mount(main, h("div", { class: "empty" }, "Couldn't load this page. ", h("a", { href: location.hash }, "Try again")));
      }
      return;
    }
  }
  location.hash = "#/";
}

async function boot() {
  if (!getToken()) return toLogin();
  try {
    await loadMe();
  } catch (e) {
    mount(document.getElementById("app"), h("div", { class: "empty" }, e.message || "Couldn't reach Autorack. ", h("a", { href: "" }, "Retry")));
    return;
  }
  if (!ctx.me.warehouse) {
    // Signed in, but no warehouse to look at: the operator, or someone who
    // was removed from every team.
    if (ctx.me.is_operator) return location.replace("/admin/");
    mount(document.getElementById("app"), h("div", { class: "auth-page" }, h("div", { class: "auth-card" },
      h("h1", null, "No warehouse yet"),
      h("p", { class: "muted" }, "You're signed in, but you're not on any warehouse's team. Ask an owner to invite you."),
      h("button", { class: "btn", onclick: logout }, "Sign out"))));
    return;
  }
  document.title = `${ctx.me.warehouse.name} · Autorack`;
  window.addEventListener("hashchange", route);
  // Keep the access banner honest (e.g. after a checkout completes elsewhere).
  setInterval(() => loadMe().catch(() => {}), 5 * 60 * 1000);
  route();
}

boot();
