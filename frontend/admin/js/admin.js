// Operator console: every warehouse at once. For the people running Autorack
// (OPERATOR_EMAILS). Signs in with the same session as the dashboard.

import {
  brandLockup, confirmDialog, fmtAgo, fmtDate, fmtDateTime, fmtMoney, fmtNumber, fmtPercent, h, mount, toast,
} from "../../shared/dom.js";
import { columnChart } from "../../app/js/chart.js";
import { api, card, ctx, fail, getToken, loadMe, logout, photoThumb, poll, stopPolling, table, toLogin } from "../../app/js/core.js";
import { REASONS } from "../../app/js/views/flags.js";

const TABS = [
  ["#/", "Overview"],
  ["#/warehouses", "Warehouses"],
  ["#/usage", "Feature usage"],
  ["#/photos", "Photos"],
  ["#/activity", "Activity"],
];

const STATUS_LABEL = {
  pilot: "Pilot", trialing: "Trial", active: "Paying", past_due: "Past due", canceled: "Canceled",
  unpaid: "Unpaid", incomplete: "Incomplete", incomplete_expired: "Expired", paused: "Paused",
};
const HEALTH = {
  active: ["Active", "badge-ok"],
  light: ["Light use", "badge-in_progress"],
  idle: ["Gone quiet", "badge-warn"],
  not_started: ["Not started", "badge-pending"],
};

function shell(active, ...content) {
  mount(document.getElementById("app"),
    h("div", { class: "ops" },
      h("header", { class: "ops-top" },
        h("div", { class: "row" }, brandLockup({ href: "#/" }), h("span", { class: "ops-tag" }, "Operator")),
        h("nav", { class: "ops-nav" }, ...TABS.map(([href, label]) =>
          h("a", { href, class: ["ops-link", active === href && "active"] }, label))),
        h("div", { class: "row small" },
          ctx.me.warehouse ? h("a", { href: "/app/" }, "My dashboard") : null,
          h("span", { class: "muted" }, ctx.me.user.email),
          h("button", { class: "link-btn", onclick: logout }, "Sign out"))),
      h("main", { class: "ops-main" }, ...content)));
}

function tile(label, value, hint, cls) {
  return h("div", { class: ["tile", cls] }, h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value),
    hint ? h("div", { class: "tile-hint" }, hint) : null);
}

function statusBadge(r) {
  const tone = { pilot: "badge-in_progress", trialing: "badge-pending", active: "badge-ok", past_due: "badge-warn" }[r.status] || "badge-bad";
  const days = r.status === "trialing" && r.trial_days_left !== null ? ` · ${r.trial_days_left}d` : "";
  return h("span", { class: `badge ${tone}` }, `${STATUS_LABEL[r.status] || r.status}${days}`);
}

function healthBadge(r) {
  const [label, cls] = HEALTH[r.health] || [r.health, ""];
  return h("span", { class: `badge ${cls}` }, label);
}

function warehouseTable(rows, empty = "No warehouses.") {
  return table([
    { label: "Warehouse", render: (r) => h("div", null, h("a", { href: `#/w/${r.id}`, class: "strong" }, r.name), h("div", { class: "muted small" }, r.owner_email)) },
    { label: "Plan", render: statusBadge },
    { label: "Health", render: healthBadge },
    { label: "Scans 7d", align: "right", render: (r) => fmtNumber(r.scans_7d) },
    { label: "Caught 7d", align: "right", render: (r) => fmtNumber(r.errors_7d) },
    { label: "Orders 7d", align: "right", render: (r) => fmtNumber(r.orders_7d) },
    { label: "Workers", align: "right", render: (r) => fmtNumber(r.workers) },
    { label: "Phones", align: "right", render: (r) => fmtNumber(r.phones) },
    { label: "Last scan", render: (r) => h("span", { class: "muted nowrap" }, fmtAgo(r.last_scan_at)) },
    { label: "Last login", render: (r) => h("span", { class: "muted nowrap" }, fmtAgo(r.last_login_at)) },
    { label: "Signed up", render: (r) => h("span", { class: "muted nowrap" }, fmtDate(r.created_at)) },
  ], rows, { empty, onRow: (r) => { location.hash = `#/w/${r.id}`; } });
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

async function overview() {
  const o = await api("/api/admin/overview");
  const t = o.totals;
  shell("#/",
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Overview"), h("p", { class: "muted" }, "Every warehouse on Autorack."))),
    h("div", { class: "tiles" },
      tile("Warehouses", fmtNumber(t.warehouses), `${fmtNumber(t.active_7d)} scanned this week`, "tile-hero"),
      tile("MRR", fmtMoney(t.mrr_cents), `${fmtNumber(t.paying)} paying`, "tile-hero"),
      tile("Sign-ups (7 days)", fmtNumber(t.signups_7d), `${fmtNumber(t.signups_30d)} in 30 days`),
      tile("Trials ending ≤ 7 days", fmtNumber(t.trials_ending_7d), null, t.trials_ending_7d ? "tile-warn" : null),
      tile("Scans (7 days)", fmtNumber(t.scans_7d)),
      tile("Mistakes caught (7 days)", fmtNumber(t.errors_caught_7d))),
    card("Sign-ups per day",
      columnChart({ title: "New warehouses, last 30 days", unit: "sign-ups", points: o.signups.map((s) => ({ date: s.date, value: s.count })) })),
    h("div", { class: "grid-2col" },
      card("Trials ending soon",
        h("p", { class: "muted small" }, "Talk to these before their trial runs out. Owners get an automatic email 3 days and 1 day before."),
        warehouseTable(o.trials_ending, "No trials ending in the next 7 days.")),
      card("Pilots",
        h("p", { class: "muted small" }, "Free pilots you set up. Is each one actually using it?"),
        warehouseTable(o.pilots, "No pilots. Make a warehouse a pilot from its page."))),
    card("By plan", h("div", { class: "row" }, ...Object.entries(o.by_status).map(([k, n]) =>
      h("span", { class: "badge" }, `${STATUS_LABEL[k] || k}: ${n}`)))));
}

async function warehouses(params) {
  const o = await api("/api/admin/overview");
  const q = (params.get("q") || "").toLowerCase();
  const status = params.get("status") || "";
  const rows = o.warehouses.filter((r) =>
    (!q || r.name.toLowerCase().includes(q) || r.owner_email.includes(q)) && (!status || r.status === status));
  const search = h("input", { class: "input", type: "search", placeholder: "Name or email", value: q });
  const go = (next) => {
    const p = new URLSearchParams({ q: next.q ?? search.value, status: next.status ?? status });
    location.hash = `#/warehouses?${p}`;
  };
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") go({}); });
  shell("#/warehouses",
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Warehouses"), h("p", { class: "muted" }, `${rows.length} of ${o.warehouses.length}`))),
    h("div", { class: "toolbar" },
      h("div", { class: "tabs" }, ...[["", "All"], ["trialing", "Trials"], ["pilot", "Pilots"], ["active", "Paying"], ["past_due", "Past due"], ["canceled", "Canceled"]]
        .map(([v, label]) => h("button", { class: ["tab", status === v && "active"], onclick: () => go({ status: v }) }, label))),
      search),
    card(null, warehouseTable(rows)));
}

async function warehouseDetail(id) {
  const w = await api(`/api/admin/warehouses/${id}`);
  const reload = () => warehouseDetail(id).catch(fail);
  const setStatus = async (body, question) => {
    if (!(await confirmDialog(question, "Recorded in the warehouse's activity log under your name.", { confirmLabel: "Confirm" }))) return;
    try {
      await api(`/api/admin/warehouses/${id}/status`, { method: "POST", body });
      toast("Updated", "ok");
      reload();
    } catch (e) {
      fail(e);
    }
  };
  const s = w.summary;
  shell("#/warehouses",
    h("a", { href: "#/warehouses", class: "back-link" }, "← Warehouses"),
    h("div", { class: "page-head" },
      h("div", null,
        h("h1", { class: "row" }, w.name, statusBadge(w), healthBadge(w)),
        h("p", { class: "muted" }, `${w.owner_email} · ${w.timezone} · signed up ${fmtDate(w.created_at)}`,
          w.trial_ends_at ? ` · trial ends ${fmtDate(w.trial_ends_at)}` : "", w.has_stripe ? " · billed through Stripe" : "")),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: () => setStatus({ status: "trialing", trial_days: 14 }, "Give a fresh 14-day trial?") }, "Extend trial 14 days"),
        w.status !== "pilot" ? h("button", { class: "btn btn-primary", onclick: () => setStatus({ status: "pilot" }, `Make ${w.name} a free pilot?`) }, "Make pilot") : null,
        w.status !== "canceled" ? h("button", { class: "btn btn-ghost", onclick: () => setStatus({ status: "canceled" }, `Cancel ${w.name}? Scanning stops; data stays.`) }, "Cancel") : null)),
    h("div", { class: "tiles" },
      tile("Scans (7 days)", fmtNumber(w.scans_7d)),
      tile("Mistakes caught (7 days)", fmtNumber(w.errors_7d)),
      tile("Orders created (7 days)", fmtNumber(w.orders_7d)),
      tile("Workers · phones · team", `${w.workers} · ${w.phones} · ${w.team}`),
      tile("Today: units", fmtNumber(s.today.units_picked)),
      tile("Today: caught", fmtNumber(s.today.errors_caught), fmtMoney(s.today.money_saved_cents)),
      tile("Open problems", fmtNumber(s.orders.open_problems)),
      tile("Accuracy (all time)", fmtPercent(s.all_time.accuracy))),
    h("div", { class: "grid-2col" },
      card("Getting set up",
        h("ol", { class: "checklist-steps" }, ...w.onboarding.steps.map((st) =>
          h("li", { class: st.done ? "done" : null }, h("span", { class: "step-check" }, st.done ? "✓" : ""), st.label))),
        w.onboarding.sample_loaded ? h("p", { class: "muted small" }, "Loaded the sample orders.") : null),
      card("Last 14 days",
        columnChart({ title: "Units verified", unit: "units", points: w.trend.map((d) => ({ date: d.date, value: d.units_picked })) }))),
    h("div", { class: "grid-2col" },
      card("Features used (30 days)",
        table([
          { label: "Feature", key: "label" },
          { label: "Times", align: "right", render: (u) => fmtNumber(u.count) },
          { label: "Last used", render: (u) => h("span", { class: "muted" }, u.last_used || "–") },
        ], w.usage, { empty: "Nothing tracked yet." })),
      card("Team",
        table([
          { label: "Email", render: (u) => h("span", null, u.email, u.name ? h("span", { class: "muted" }, ` (${u.name})`) : null) },
          { label: "Role", key: "role" },
          { label: "Last sign-in", render: (u) => h("span", { class: "muted" }, fmtAgo(u.last_login_at)) },
        ], w.members))),
    card("Photos from the floor", photoGrid(w.photos, false)),
    card("Recent activity",
      table([
        { label: "When", render: (e) => h("span", { class: "muted nowrap" }, fmtDateTime(e.at)) },
        { label: "Who", key: "actor" },
        { label: "What", render: (e) => h("span", { class: "mono small" }, e.action) },
      ], w.events, { empty: "Nothing yet." })));
}

function photoGrid(photos, showWarehouse = true) {
  if (!photos.length) return h("div", { class: "empty" }, "No photos yet.");
  return h("div", { class: "photo-grid" }, ...photos.map((p) => h("figure", { class: "photo-card" },
    photoThumb(p.id, "/api/admin/photos/", `${p.warehouse} · ${REASONS[p.reason] || p.reason} · ${p.worker || ""}${p.note ? ` · “${p.note}”` : ""}`),
    h("figcaption", null,
      showWarehouse ? h("a", { href: `#/w/${p.warehouse_id}`, class: "strong" }, p.warehouse) : null,
      h("div", { class: "muted small" }, `${REASONS[p.reason] || p.reason} · ${p.worker || "worker"} · order ${p.order_number || "–"}`),
      h("div", { class: "muted small" }, `${fmtAgo(p.at)} · ${Math.round(p.size_bytes / 1024)} KB${p.resolved ? " · resolved" : ""}`)))));
}

async function photos() {
  const list = await api("/api/admin/photos?limit=120");
  shell("#/photos",
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Photos"), h("p", { class: "muted" }, "What workers photographed when reporting problems, across every warehouse. Newest first."))),
    card(null, photoGrid(list)));
}

async function usage(params) {
  const days = Number(params.get("days") || 30);
  const u = await api(`/api/admin/usage?days=${days}`);
  const max = Math.max(1, ...u.features.map((f) => f.count));
  shell("#/usage",
    h("div", { class: "page-head" },
      h("div", null, h("h1", null, "Feature usage"), h("p", { class: "muted" }, "Counts only, never content. Which parts of the product get used, and by how many warehouses.")),
      h("select", { class: "input input-inline", onchange: (e) => { location.hash = `#/usage?days=${e.target.value}`; } },
        ...[7, 30, 90, 365].map((d) => h("option", { value: String(d), selected: d === days }, `Last ${d} days`)))),
    card(null, table([
      { label: "Feature", render: (f) => h("div", null, h("strong", null, f.label), h("div", { class: "muted small mono" }, f.feature)) },
      { label: "", render: (f) => h("div", { class: "bar usage-bar" }, h("span", { style: { width: `${Math.round((100 * f.count) / max)}%` } })) },
      { label: "Times", align: "right", render: (f) => fmtNumber(f.count) },
      { label: "Warehouses", align: "right", render: (f) => fmtNumber(f.warehouses) },
      { label: "Last used", render: (f) => h("span", { class: "muted" }, f.last_used || "never") },
    ], u.features)));
}

async function activity(params) {
  const q = params.get("q") || "";
  const rows = await api(`/api/admin/activity?limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`);
  const search = h("input", { class: "input", type: "search", placeholder: "Warehouse or person", value: q });
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") location.hash = `#/activity?q=${encodeURIComponent(search.value)}`; });
  const label = {
    "warehouse.created": "Signed up", "user.login": "Signed in", "orders.imported": "Imported orders",
    "team.invited": "Invited someone", "billing.status_changed": "Billing changed", "warehouse.status_set": "Plan set by operator",
    "device.linked": "Linked a phone", "worker.created": "Added a worker",
  };
  shell("#/activity",
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Activity"), h("p", { class: "muted" }, "Sign-ups, sign-ins, imports and billing changes across all warehouses.")), search),
    card(null, table([
      { label: "When", render: (e) => h("span", { class: "muted nowrap" }, fmtDateTime(e.at)) },
      { label: "Warehouse", render: (e) => (e.warehouse_id ? h("a", { href: `#/w/${e.warehouse_id}` }, e.warehouse || "–") : "–") },
      { label: "Who", key: "actor" },
      { label: "What", render: (e) => label[e.action] || e.action },
    ], rows, { empty: "Nothing yet." })));
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

const ROUTES = [
  [/^\/?$/, () => overview()],
  [/^\/warehouses$/, (m, p) => warehouses(p)],
  [/^\/w\/([0-9a-f-]{36})$/, (m) => warehouseDetail(m[1])],
  [/^\/usage$/, (m, p) => usage(p)],
  [/^\/photos$/, () => photos()],
  [/^\/activity$/, (m, p) => activity(p)],
];

async function route() {
  stopPolling();
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  for (const [re, view] of ROUTES) {
    const m = re.exec(path);
    if (m) {
      try {
        await view(m, params);
        window.scrollTo(0, 0);
      } catch (e) {
        fail(e);
      }
      return;
    }
  }
  location.hash = "#/";
}

async function boot() {
  if (!getToken()) return toLogin();
  await loadMe();
  if (!ctx.me.is_operator) {
    mount(document.getElementById("app"), h("div", { class: "auth-page" }, h("div", { class: "auth-card" },
      h("h1", null, "Not available"), h("p", { class: "muted" }, "This page is for Autorack staff."),
      h("a", { class: "btn", href: "/app/" }, "Go to your dashboard"))));
    return;
  }
  window.addEventListener("hashchange", route);
  route();
  // Keep the overview fresh if it's left open on a screen.
  poll(() => (location.hash === "" || location.hash === "#/" ? overview() : null), 60000);
}

boot().catch((e) => {
  mount(document.getElementById("app"), h("div", { class: "empty" }, e.message || "Couldn't reach Autorack."));
});
