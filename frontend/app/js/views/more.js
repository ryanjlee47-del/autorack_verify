// Insights, billing, settings.

import {
  confirmDialog, dialog, fmtAgo, fmtDate, fmtDateTime, fmtMoney, fmtNumber, fmtPercent, h, toast,
} from "../../../shared/dom.js";
import { columnChart } from "../chart.js";
import { api, card, ctx, download, fail, isOwner, layout, loadMe, pageHeader, table, tz } from "../core.js";

// ---------------------------------------------------------------------------
// Insights
// ---------------------------------------------------------------------------

export async function insightsView(params) {
  const days = Number(params.get("days") || 30);
  const [trend, skus, workers] = await Promise.all([
    api(`/api/dashboard/trend?days=${days}`),
    api(`/api/dashboard/skus?days=${days}`),
    api(`/api/dashboard/workers?days=${days}`),
  ]);
  const exportDays = h("select", { class: "input input-inline" },
    ...[7, 30, 90, 365].map((d) => h("option", { value: String(d), selected: d === days }, `Last ${d} days`)));

  layout("#/insights", [
    pageHeader("Insights", "Where mistakes come from, and whether they're going down.",
      h("select", {
        class: "input input-inline",
        onchange: (e) => { location.hash = `#/insights?days=${e.target.value}`; },
      }, ...[14, 30, 90, 180].map((d) => h("option", { value: String(d), selected: d === days }, `Last ${d} days`)))),
    card("Daily activity",
      columnChart({ title: "Units picked per day", unit: "units", points: trend.series.map((p) => ({ date: p.date, value: p.units_picked })) }),
      columnChart({ title: "Mistakes caught per day", unit: "mistakes", points: trend.series.map((p) => ({ date: p.date, value: p.errors_caught })) })),
    h("div", { class: "grid-2col" },
      card("Most mis-picked items",
        h("p", { class: "muted small" }, "The item the worker was trying to pick when they scanned the wrong thing. Frequent entries often mean a look-alike product in a nearby bin, or a bad shelf label."),
        table([
          { label: "Item", render: (r) => h("div", null, r.description || r.sku || "–", h("div", { class: "muted small mono" }, r.barcode)) },
          { label: "Wrong picks", align: "right", render: (r) => fmtNumber(r.errors) },
        ], skus.most_mispicked, { empty: "No mis-picks in this period." })),
      card("Barcodes most often grabbed by mistake",
        h("p", { class: "muted small" }, "What was actually scanned instead. If one of these is legitimately the same product, open the order and use “This is actually…” to teach it."),
        table([
          { label: "Scanned barcode", render: (r) => h("span", { class: "mono" }, r.barcode) },
          { label: "Times", align: "right", render: (r) => fmtNumber(r.times) },
          { label: "Orders", align: "right", render: (r) => fmtNumber(r.orders) },
        ], skus.most_grabbed_wrong, { empty: "Nothing yet." }))),
    card("Workers",
      table([
        { label: "Name", render: (w) => h("strong", null, w.name) },
        { label: "Units picked", align: "right", render: (w) => fmtNumber(w.units_picked) },
        { label: "Mistakes caught", align: "right", render: (w) => fmtNumber(w.mismatches + w.over_picks) },
        { label: "Mistake rate", align: "right", render: (w) => h("span", { class: w.needs_attention ? "warn-text" : null }, fmtPercent(w.error_rate)) },
        { label: "Orders", align: "right", render: (w) => fmtNumber(w.orders) },
      ], workers.workers.filter((w) => w.scans > 0), { empty: "No scans in this period." })),
    card("Export",
      h("p", { class: "muted small" }, "Every scan is kept permanently. Exports are the audit trail if a customer disputes a shipment."),
      h("div", { class: "row" },
        exportDays,
        h("button", { class: "btn", onclick: () => download(`/api/exports/scans.csv?days=${exportDays.value}`, "autorack-scans.csv") }, "Download scans (CSV)"),
        h("button", { class: "btn", onclick: () => download(`/api/exports/orders.csv?days=${exportDays.value}`, "autorack-orders.csv") }, "Download orders (CSV)"))),
  ]);
}

// ---------------------------------------------------------------------------
// Billing
// ---------------------------------------------------------------------------

export async function billingView(params) {
  if (params.get("checkout") === "success") {
    toast("Thanks! Your subscription is being activated.", "ok", 6000);
    history.replaceState(null, "", "#/billing");
  }
  const b = await api("/api/billing");
  const a = b.access;
  const stateText = {
    pilot: "Free pilot",
    trialing: a.trial_days_left !== null ? `Free trial · ${a.trial_days_left} day${a.trial_days_left === 1 ? "" : "s"} left` : "Free trial",
    trial_expired: "Trial ended",
    active: "Active",
    grace: "Payment failed",
    past_due: "Past due",
    canceled: "Canceled",
  }[a.state] || a.state;
  const tone = a.allowed ? (a.state === "grace" ? "warn" : "ok") : "bad";

  const subscribe = async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/billing/checkout", { method: "POST" });
      location.href = r.url;
    } catch (err) {
      e.target.disabled = false;
      fail(err);
    }
  };
  const manage = async () => {
    try {
      location.href = (await api("/api/billing/portal", { method: "POST" })).url;
    } catch (err) {
      fail(err);
    }
  };

  const owner = isOwner();
  let action = null;
  if (!owner) action = h("p", { class: "muted" }, "Only an owner can change billing.");
  else if (!b.stripe_enabled) action = h("p", { class: "muted" }, "Online billing isn't set up yet. Contact us to subscribe.");
  else if (b.has_subscription && b.status !== "canceled") action = h("button", { class: "btn btn-primary", onclick: manage }, "Manage billing");
  else action = h("div", { class: "row" },
    h("button", { class: "btn btn-primary", onclick: subscribe }, `Subscribe · ${fmtMoney(b.price_cents)}/month`),
    b.can_manage ? h("button", { class: "btn", onclick: manage }, "Billing history") : null);

  layout("#/billing", [
    pageHeader("Billing", "One flat price per warehouse. No per-scan or per-seat charges."),
    h("div", { class: "grid-main" },
      card(null,
        h("div", { class: "plan" },
          h("div", { class: "plan-price" }, fmtMoney(b.price_cents), h("span", null, " / month")),
          h("div", { class: "plan-name" }, "Per warehouse, everything included"),
          h("ul", { class: "checklist" },
            ...["Unlimited workers, phones and scans", "Offline scanning with automatic sync", "CSV import, pick sheets, live dashboard",
              "Full scan history and exports", "English and Spanish worker app"].map((t) => h("li", null, t)))),
        h("div", { class: `banner banner-${tone}` }, h("strong", null, stateText), a.message && a.state !== "active" ? ` — ${a.message}` : ""),
        b.current_period_end ? h("p", { class: "muted" }, b.cancel_at_period_end ? "Ends " : "Renews ", fmtDate(b.current_period_end, tz())) : null,
        action),
      card("How billing works",
        h("ul", { class: "plain-list" },
          h("li", null, "Your trial doesn't need a card. Subscribing early keeps the rest of your trial free."),
          h("li", null, "Cancel any time from “Manage billing”. Your data stays available to view and export."),
          h("li", null, "If a payment fails, scanning keeps working for a grace period while you update your card."),
          h("li", null, "If scanning is paused, scans already made on phones still sync. Nothing is lost.")))),
  ]);
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function timezones() {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return ["UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"];
  }
}

export async function settingsView() {
  const owner = isOwner();
  const [wh, team, aliases, audit] = await Promise.all([
    api("/api/warehouse"),
    api("/api/team"),
    api("/api/aliases"),
    owner ? api("/api/audit?limit=100") : Promise.resolve([]),
  ]);
  const reload = () => settingsView().catch(fail);

  const name = h("input", { class: "input", value: wh.name, disabled: !owner });
  const email = h("input", { class: "input", type: "email", value: wh.owner_email, disabled: !owner });
  const zone = h("select", { class: "input", disabled: !owner },
    ...timezones().map((z) => h("option", { value: z, selected: z === wh.timezone }, z)));
  const loose = h("input", { type: "checkbox", disabled: !owner });
  loose.checked = wh.loose_match_enabled;
  const suffix = h("input", { class: "input input-qty", type: "number", min: "6", max: "14", value: String(wh.suffix_len), disabled: !owner });

  const save = (body, msg) => api("/api/warehouse", { method: "PATCH", body }).then(async () => {
    toast(msg, "ok");
    await loadMe();
    reload();
  }, fail);

  const aliasScanned = h("input", { class: "input mono", placeholder: "Barcode as scanned" });
  const aliasTarget = h("input", { class: "input mono", placeholder: "Barcode on your orders" });

  layout("#/settings", [
    pageHeader("Settings"),
    card("Warehouse",
      h("form", {
        class: "form-grid",
        onsubmit: (e) => {
          e.preventDefault();
          save({ name: name.value, owner_email: email.value, timezone: zone.value }, "Saved");
        },
      },
      h("label", null, "Name", name),
      h("label", null, "Billing email", email),
      h("label", null, "Timezone (for “today” and reports)", zone),
      owner ? h("div", { class: "span-2" }, h("button", { class: "btn btn-primary", type: "submit" }, "Save")) : null)),

    card("Barcode matching",
      h("p", { class: "muted" }, "Autorack already treats the same product's UPC-E, UPC-A, EAN-13 and GTIN-14 forms, GS1 labels, stray spaces and missing check digits as a match. Anything ambiguous goes to review instead of being guessed."),
      h("label", { class: "row check" }, loose, "Also match on the last digits of long numeric codes"),
      h("div", { class: "row" }, h("span", null, "Digits to compare:"), suffix),
      h("p", { class: "muted small" }, "Off by default. Matches found this way are never counted automatically; they're sent to review. Useful when vendors print long internal codes that end in your SKU number."),
      owner ? h("button", {
        class: "btn",
        onclick: () => save({ loose_match_enabled: loose.checked, suffix_len: Number(suffix.value) }, "Matching settings saved. Phones pick them up on next sync."),
      }, "Save matching settings") : null),

    card("Taught barcodes",
      h("p", { class: "muted small" }, "Barcodes that should count as another barcode, like a vendor's case label for your item. You can also teach one from an order's scan history."),
      h("form", {
        class: "inline-form",
        onsubmit: (e) => {
          e.preventDefault();
          api("/api/aliases", { method: "POST", body: { scanned_barcode: aliasScanned.value, target_barcode: aliasTarget.value } })
            .then(() => { toast("Barcode taught", "ok"); reload(); }, fail);
        },
      }, aliasScanned, h("span", null, "counts as"), aliasTarget, h("button", { class: "btn", type: "submit" }, "Add")),
      table([
        { label: "Scanned", render: (a) => h("span", { class: "mono" }, a.alias) },
        { label: "Counts as", render: (a) => h("span", { class: "mono" }, a.target) },
        { label: "Note", render: (a) => a.note || h("span", { class: "muted" }, "–") },
        { label: "Added", render: (a) => fmtDate(a.created_at, tz()) },
        {
          label: "",
          render: (a) => h("button", {
            class: "btn btn-sm btn-ghost",
            onclick: async () => {
              if (!(await confirmDialog("Remove taught barcode?", `${a.alias} will stop counting as ${a.target}.`, { confirmLabel: "Remove", danger: true }))) return;
              await api(`/api/aliases/${a.id}`, { method: "DELETE" }).then(reload, fail);
            },
          }, "Remove"),
        },
      ], aliases, { empty: "None yet." })),

    card("Team",
      h("p", { class: "muted small" }, "Owners manage billing, settings and the team. Managers do everything else. Everyone signs in with an emailed link; there are no passwords."),
      owner ? h("button", { class: "btn", onclick: () => invite(reload) }, "Invite someone") : null,
      table([
        { label: "Email", render: (u) => h("span", null, u.email, u.name ? h("span", { class: "muted" }, ` (${u.name})`) : null) },
        {
          label: "Role",
          render: (u) => owner && u.id !== ctx.me.user.id
            ? h("select", {
              class: "input input-inline",
              onchange: (e) => api(`/api/team/${u.id}`, { method: "PATCH", body: { role: e.target.value } }).then(reload, (err) => { fail(err); reload(); }),
            }, ...["owner", "manager"].map((r) => h("option", { value: r, selected: r === u.role }, r)))
            : u.role,
        },
        { label: "Status", render: (u) => (u.active ? "Active" : h("span", { class: "muted" }, "Removed")) },
        { label: "Last sign-in", render: (u) => h("span", { class: "muted" }, fmtAgo(u.last_login_at)) },
        {
          label: "",
          render: (u) => owner && u.id !== ctx.me.user.id ? h("button", {
            class: "btn btn-sm btn-ghost",
            onclick: () => api(`/api/team/${u.id}`, { method: "PATCH", body: { active: !u.active } }).then(reload, fail),
          }, u.active ? "Remove" : "Restore") : null,
        },
      ], team)),

    owner ? card("Activity log",
      h("p", { class: "muted small" }, "Every change made by your team, the phones, and billing. This log can't be edited."),
      table([
        { label: "When", render: (e) => h("span", { class: "muted nowrap" }, fmtDateTime(e.at, tz())) },
        { label: "Who", render: (e) => e.actor || e.actor_type },
        { label: "What", render: (e) => h("span", { class: "mono small" }, e.action) },
        { label: "Details", render: (e) => h("span", { class: "muted small" }, summarize(e.details)) },
      ], audit, { empty: "Nothing yet." })) : null,
  ]);
}

function summarize(details) {
  if (!details) return "";
  return Object.entries(details)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" · ")
    .slice(0, 160);
}

async function invite(reload) {
  const r = await dialog("Invite someone", (close) => {
    const email = h("input", { class: "input", type: "email", required: true, placeholder: "name@company.com" });
    const role = h("select", { class: "input" }, h("option", { value: "manager" }, "Manager"), h("option", { value: "owner" }, "Owner"));
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close({ email: email.value, role: role.value }); } },
      h("label", null, "Email"), email, h("label", null, "Role"), role,
      h("p", { class: "muted small" }, "They get an email with a sign-in link."),
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn btn-primary", type: "submit" }, "Send invite")));
  });
  if (!r) return;
  await api("/api/team", { method: "POST", body: r }).then(() => { toast(`Invite sent to ${r.email}`, "ok"); reload(); }, fail);
}

