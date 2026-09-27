// Owner dashboard entry point: hash router.

import { h, mount } from "../../shared/dom.js";
import { agreementGate, loadAgreement } from "./agreement.js";
import { api, ctx, fail, getToken, loadMe, logout, stopPolling, switchWarehouse, toLogin } from "./core.js";
import { connectionsView } from "./views/connections.js";
import { dashboardView } from "./views/dashboard.js";
import { clientView, clientsView, insertsView, restockView, timeView } from "./views/floor.js";
import { productView, productsView } from "./views/products.js";
import { billingView, insightsView, settingsView } from "./views/more.js";
import { boardView, reportsView } from "./views/reports.js";
import { importView, newOrderView, orderDetailView, ordersView } from "./views/orders.js";
import { devicesView, workersView } from "./views/people.js";
import { helpView } from "./views/help.js";
import { reportErrors } from "../../shared/report-errors.js";
import { T } from "./i18n.js";

reportErrors("dashboard");

const ROUTES = [
  [/^\/?$/, () => dashboardView()],
  [/^\/orders$/, (m, p) => ordersView(p)],
  [/^\/orders\/new$/, (m, p) => newOrderView(p)],
  [/^\/orders\/import$/, (m, p) => importView(p)],
  [/^\/orders\/([0-9a-f-]{36})$/, (m) => orderDetailView(m[1])],
  [/^\/workers$/, (m, p) => workersView(p)],
  [/^\/devices$/, () => devicesView()],
  [/^\/insights$/, (m, p) => insightsView(p)],
  [/^\/reports$/, (m, p) => reportsView(p)],
  [/^\/board$/, (m, p) => boardView(p)],
  [/^\/billing$/, (m, p) => billingView(p)],
  [/^\/connections$/, () => connectionsView()],
  [/^\/products$/, (m, p) => productsView(p)],
  [/^\/products\/([0-9a-f-]{36})$/, (m) => productView(m[1])],
  [/^\/settings$/, () => settingsView()],
  [/^\/restock$/, (m, p) => restockView(p)],
  [/^\/time$/, (m, p) => timeView(p)],
  [/^\/inserts$/, (m, p) => insertsView(p)],
  [/^\/clients$/, (m, p) => clientsView(p)],
  [/^\/clients\/([0-9a-f-]{36})$/, (m, p) => clientView(m[1], p)],
  [/^\/help$/, (m, p) => helpView(p)],
];

async function route() {
  stopPolling();
  document.body.classList.remove("tv");
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  if (path !== "/help") ctx.lastRoute = raw;
  const params = new URLSearchParams(query || "");
  for (const [re, view] of ROUTES) {
    const m = re.exec(path);
    if (m) {
      try {
        await view(m, params);
      } catch (e) {
        fail(e);
        const main = document.getElementById("main");
        if (main) mount(main, h("div", { class: "empty" }, T("Couldn't load this page.") + " ", h("a", { href: location.hash }, T("Try again"))));
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
    mount(document.getElementById("app"), h("div", { class: "empty" }, e.message || T("Couldn't reach Autorack.") + " ", h("a", { href: "" }, T("Retry"))));
    return;
  }
  if (ctx.me.membership && ctx.me.membership.role === "client") {
    // A 3PL client's login: their own portal, not the warehouse's dashboard.
    location.replace("/portal/");
    return;
  }
  if (!ctx.me.warehouse) {
    // Signed in, but no warehouse to look at: the operator, or someone who
    // was removed from every team.
    if (ctx.me.is_operator) return location.replace("/admin/");
    mount(document.getElementById("app"), h("div", { class: "auth-page" }, h("div", { class: "auth-card" },
      h("h1", null, T("No warehouse yet")),
      h("p", { class: "muted" }, T("You're signed in, but you're not on any warehouse's team. Ask an owner to invite you.")),
      h("button", { class: "btn", onclick: logout }, T("Sign out")))));
    return;
  }
  document.title = T("{name} · Autorack", { name: ctx.me.warehouse.name });
  if (ctx.me.agreement && ctx.me.agreement.required) {
    const host = document.getElementById("app");
    mount(host, h("div", { class: "boot" }, h("div", { class: "skeleton" })));
    agreementGate(host, {
      me: ctx.me,
      info: await loadAgreement(),
      api,
      onSigned: () => location.reload(),
      onSignOut: logout,
      onSwitch: (id) => switchWarehouse(id).catch(fail),
    });
    return;
  }
  window.addEventListener("hashchange", route);
  // Keep the access banner honest (e.g. after a checkout completes elsewhere).
  setInterval(() => loadMe().catch(() => {}), 5 * 60 * 1000);
  route();
}

boot();
