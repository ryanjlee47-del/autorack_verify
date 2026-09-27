// Operator console: every warehouse at once. For the people running Autorack
// (OPERATOR_EMAILS). Signs in with the same session as the dashboard.

import {
  brandLockup, confirmDialog, dialog, fmtAgo, fmtDate, fmtDateTime, fmtMoney, fmtNumber, fmtPercent, h, mount, toast,
} from "../../shared/dom.js";
import { columnChart } from "../../app/js/chart.js";
import {
  api, card, ctx, download, fail, getToken, loadMe, logout, photoThumb, poll, stopPolling, table, toLogin,
} from "../../app/js/core.js";
import { REASONS } from "../../app/js/views/flags.js";
import { reportErrors } from "../../shared/report-errors.js";

reportErrors("operator");

const TABS = [
  ["#/", "Overview"],
  ["#/warehouses", "Warehouses"],
  ["#/usage", "Feature usage"],
  ["#/photos", "Photos"],
  ["#/activity", "Activity"],
  ["#/notices", "Notices"],
  ["#/errors", "Errors"],
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
  if (r.closed_at) return h("span", { class: "badge badge-bad" }, `Closed · deletes ${fmtDate(r.deletion_due_at)}`);
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
  const post = async (url, body, question) => {
    if (!(await confirmDialog(question, "Recorded in the warehouse's activity log under your name.", { confirmLabel: "Confirm" }))) return;
    try {
      await api(url, { method: "POST", body });
      toast("Done", "ok");
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
        h("button", { class: "btn", onclick: () => download(`/api/admin/warehouses/${id}/export.zip`, "export.zip") }, "Export data"),
        w.closed_at
          ? h("button", { class: "btn", onclick: () => post(`/api/admin/warehouses/${id}/reopen`, {}, `Reopen ${w.name}? The scheduled deletion is cancelled.`) }, "Reopen")
          : h("button", { class: "btn btn-danger", onclick: () => closeWarehouse(w, reload) }, "Close account…"),
        w.closed_at ? h("button", { class: "btn btn-danger", onclick: () => purgeWarehouse(w) }, "Delete data now…") : null)),
    w.closed_at ? h("div", { class: "banner banner-bad" },
      `Closed ${fmtDateTime(w.closed_at)} by ${w.closed_by || "?"}${w.close_reason ? ` (“${w.close_reason}”)` : ""}. `,
      h("strong", null, `Data is deleted automatically on ${fmtDate(w.deletion_due_at)}.`)) : null,
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
    card("License agreement", w.agreement.signature
      ? h("div", { class: "row-between" },
        h("span", null, `Signed by ${w.agreement.signature.signer_name} (${w.agreement.signature.signer_title}) for ${w.agreement.signature.company_name}, ${fmtDateTime(w.agreement.signature.signed_at)}, version ${w.agreement.signature.version}`,
          w.agreement.signed_current ? "" : h("span", { class: "badge badge-warn badge-inline" }, "needs the current version")),
        h("button", { class: "btn btn-sm", onclick: () => download(`/api/admin/warehouses/${id}/agreement.pdf`, "agreement.pdf") }, "Download signed copy"))
      : h("p", { class: "muted" }, "Not signed yet. The owner is asked to sign on their next sign-in.")),
    card("Photos from the floor", photoGrid(w.photos, false)),
    card("Recent activity",
      table([
        { label: "When", render: (e) => h("span", { class: "muted nowrap" }, fmtDateTime(e.at)) },
        { label: "Who", key: "actor" },
        { label: "What", render: (e) => h("span", { class: "mono small" }, e.action) },
      ], w.events, { empty: "Nothing yet." })));
}

async function closeWarehouse(w, reload) {
  const reason = await dialog(`Close ${w.name}?`, (close) => {
    const input = h("input", { class: "input", maxlength: "500", placeholder: "Reason (shown to you, emailed nowhere)" });
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close(input.value); } },
      h("p", null, "Termination by Autorack. Same as the owner closing it: the Stripe subscription is cancelled, every phone stops, owners are emailed, and the data is deleted automatically after the retention period unless reopened."),
      input,
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn btn-danger", type: "submit" }, "Close account")));
  });
  if (reason === null) return;
  try {
    await api(`/api/admin/warehouses/${w.id}/close`, { method: "POST", body: { reason: reason || null } });
    toast("Account closed", "ok");
    reload();
  } catch (e) {
    fail(e);
  }
}

async function purgeWarehouse(w) {
  const typed = await dialog(`Delete ${w.name}'s data now?`, (close) => {
    const input = h("input", { class: "input mono", autocomplete: "off", placeholder: "DELETE" });
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close(input.value); } },
      h("p", null, "Permanently deletes every order, scan, photo, worker, phone and log entry for this warehouse, now instead of on the scheduled date. Only the signed license agreement is kept. This can't be undone."),
      h("p", { class: "muted small" }, "Use this when the customer asks for immediate deletion. Export a copy for them first if they want one."),
      h("label", null, "Type DELETE to confirm"), input,
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn btn-danger", type: "submit" }, "Delete permanently")));
  });
  if (!typed) return;
  try {
    const r = await api(`/api/admin/warehouses/${w.id}/purge`, { method: "POST", body: { confirm: typed } });
    toast(`Deleted: ${r.deleted.orders} orders, ${r.deleted.scans} scans, ${r.deleted.photos} photos.`, "ok", 8000);
    location.hash = "#/warehouses";
  } catch (e) {
    fail(e);
  }
}

// ---------------------------------------------------------------------------
// Notices to customers
// ---------------------------------------------------------------------------

async function notices() {
  const past = await api("/api/admin/notices");
  const subject = h("input", { class: "input", maxlength: "200", placeholder: "e.g. Scheduled maintenance Sunday 2–3am PT" });
  const message = h("textarea", { class: "input", rows: "8", maxlength: "10000", placeholder: "Plain text. A blank line starts a new paragraph." });
  const status = h("p", { class: "muted small" });
  const send = async (really) => {
    const body = { subject: subject.value.trim(), message: message.value.trim(), send: really };
    try {
      const preview = await api("/api/admin/notices", { method: "POST", body: { ...body, send: false } });
      if (!really) {
        status.textContent = `Would email ${preview.recipients} owner(s) of ${preview.warehouses} open account(s).`;
        return;
      }
      if (!(await confirmDialog("Send this notice?", `Emails ${preview.recipients} owner(s) of ${preview.warehouses} open account(s) now. It's logged in each warehouse's activity log.`, { confirmLabel: "Send now", danger: true }))) return;
      const r = await api("/api/admin/notices", { method: "POST", body });
      toast(`Sent to ${r.sent} owner(s)${r.failed ? `, ${r.failed} failed` : ""}.`, r.failed ? "warn" : "ok", 7000);
      notices().catch(fail);
    } catch (e) {
      fail(e);
    }
  };
  shell("#/notices",
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Notices"),
      h("p", { class: "muted" }, "Email the owners of every open account: a security incident, planned maintenance, a change to the terms. See docs/INCIDENT_RESPONSE.md for when and what to send."))),
    card("New notice",
      h("div", { class: "stack" },
        h("label", null, "Subject"), subject,
        h("label", null, "Message"), message,
        status,
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: () => send(false) }, "Preview recipients"),
          h("button", { class: "btn btn-danger", onclick: () => send(true) }, "Send to all owners…")))),
    card("Sent", table([
      { label: "When", render: (n) => h("span", { class: "muted nowrap" }, fmtDateTime(n.at)) },
      { label: "Subject", render: (n) => h("strong", null, n.subject) },
      { label: "By", key: "by" },
      { label: "Owners emailed", align: "right", render: (n) => fmtNumber(n.recipients) },
      { label: "Failed", align: "right", render: (n) => fmtNumber(n.failed || 0) },
    ], past, { empty: "No notices sent yet." })));
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

async function errors(params) {
  const show = params.get("show") || "open";
  const list = await api(`/api/admin/errors?show=${show}`);
  const detailHost = h("div");
  const openDetail = async (e) => {
    const d = await api(`/api/admin/errors/${e.id}`);
    mount(detailHost, card(`${d.kind}`,
      h("p", null, d.message),
      h("p", { class: "muted small" }, `${d.count} time${d.count === 1 ? "" : "s"} · first ${fmtDateTime(d.first_seen)} · last ${fmtDateTime(d.last_seen)}`),
      h("pre", { class: "error-detail" }, JSON.stringify(d.context, null, 2)),
      d.detail ? h("pre", { class: "error-detail" }, d.detail) : null,
      d.resolved_at ? h("p", { class: "ok-text" }, `Resolved ${fmtDateTime(d.resolved_at)}`) : h("button", {
        class: "btn btn-primary",
        onclick: async () => {
          await api(`/api/admin/errors/${d.id}/resolve`, { method: "POST" }).catch(fail);
          toast("Marked resolved. You'll be alerted if it happens again.", "ok");
          errors(params).catch(fail);
        },
      }, "Mark resolved")));
    detailHost.scrollIntoView({ behavior: "smooth" });
  };
  shell("#/errors",
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Errors"),
      h("p", { class: "muted" }, "Server crashes, failed background jobs, and JavaScript errors from the dashboard, phones and this console. You're emailed within a minute of anything new, and at most hourly for repeats."))),
    h("div", { class: "toolbar" }, h("div", { class: "tabs" }, ...[["open", "Open"], ["resolved", "Resolved"], ["all", "All"]].map(([v, label]) =>
      h("button", { class: ["tab", show === v && "active"], onclick: () => { location.hash = `#/errors?show=${v}`; } }, label)))),
    card(null, table([
      { label: "Last seen", render: (e) => h("span", { class: "muted nowrap" }, fmtAgo(e.last_seen)) },
      { label: "Where", render: (e) => h("span", { class: "badge" }, e.source) },
      { label: "Error", render: (e) => h("div", null, h("strong", null, e.kind), h("div", { class: "muted small" }, e.message.slice(0, 160))) },
      { label: "Times", align: "right", render: (e) => fmtNumber(e.count) },
      { label: "Page / path", render: (e) => h("span", { class: "mono small" }, e.context.path || e.context.page || e.context.job || "") },
    ], list, { empty: show === "open" ? "No open errors. Nice." : "Nothing here.", onRow: openDetail })),
    detailHost);
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
    "account.closed": "Closed the account", "account.reopened": "Reopened the account", "account.purged": "Data deleted",
    "device.linked": "Linked a phone", "worker.created": "Added a worker", "agreement.signed": "Signed the agreement",
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
  [/^\/notices$/, () => notices()],
  [/^\/errors$/, (m, p) => errors(p)],
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
