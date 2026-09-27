// Insights, billing, settings.

import {
  confirmDialog, dialog, fmtAgo, fmtCents, fmtDate, fmtDateTime, fmtMoney, fmtNumber, fmtPercent, h, mount, toast,
} from "../../../shared/dom.js";
import { columnChart } from "../chart.js";
import {
  api, card, ctx, download, fail, isOwner, layout, loadMe, pageHeader, photoThumb, switchWarehouse, table, tz,
} from "../core.js";
import { REASONS } from "./flags.js";
import { T } from "../i18n.js";

// ---------------------------------------------------------------------------
// Insights
// ---------------------------------------------------------------------------

export async function insightsView(params) {
  const days = Number(params.get("days") || 30);
  const [trend, skus, workers, photos] = await Promise.all([
    api(`/api/dashboard/trend?days=${days}`),
    api(`/api/dashboard/skus?days=${days}`),
    api(`/api/dashboard/workers?days=${days}`),
    api("/api/photos?limit=48"),
  ]);
  const exportDays = h("select", { class: "input input-inline" },
    ...[7, 30, 90, 365].map((d) => h("option", { value: String(d), selected: d === days }, T("Last {d} days", { d }))));

  layout("#/insights", [
    pageHeader(T("Insights"), T("Where mistakes come from, and whether they're going down."),
      h("select", {
        class: "input input-inline",
        onchange: (e) => { location.hash = `#/insights?days=${e.target.value}`; },
      }, ...[14, 30, 90, 180].map((d) => h("option", { value: String(d), selected: d === days }, T("Last {d} days", { d }))))),
    card(T("Daily activity"),
      columnChart({ title: T("Units picked per day"), unit: "units", points: trend.series.map((p) => ({ date: p.date, value: p.units_picked })) }),
      columnChart({ title: T("Mistakes caught per day"), unit: "mistakes", points: trend.series.map((p) => ({ date: p.date, value: p.errors_caught })) })),
    h("div", { class: "grid-2col" },
      card(T("Most mis-picked items"),
        h("p", { class: "muted small" }, T("The item the worker was trying to pick when they scanned the wrong thing. Frequent entries often mean a look-alike product in a nearby bin, or a bad shelf label.")),
        table([
          { label: T("Item"), render: (r) => h("div", null, r.description || r.sku || "–", h("div", { class: "muted small mono" }, r.barcode)) },
          { label: T("Wrong picks"), align: "right", render: (r) => fmtNumber(r.errors) },
        ], skus.most_mispicked, { empty: T("No mis-picks in this period.") })),
      card(T("Barcodes most often grabbed by mistake"),
        h("p", { class: "muted small" }, T("What was actually scanned instead. If one of these is legitimately the same product, open the order and use “This is actually…” to teach it.")),
        table([
          { label: T("Scanned barcode"), render: (r) => h("span", { class: "mono" }, r.barcode) },
          { label: T("Times"), align: "right", render: (r) => fmtNumber(r.times) },
          { label: T("Orders"), align: "right", render: (r) => fmtNumber(r.orders) },
        ], skus.most_grabbed_wrong, { empty: T("Nothing yet.") }))),
    card(T("Workers"),
      table([
        { label: T("Name"), render: (w) => h("strong", null, w.name) },
        { label: T("Units picked"), align: "right", render: (w) => fmtNumber(w.units_picked) },
        { label: T("Mistakes caught"), align: "right", render: (w) => fmtNumber(w.mismatches + w.over_picks) },
        { label: T("Mistake rate"), align: "right", render: (w) => h("span", { class: w.needs_attention ? "warn-text" : null }, fmtPercent(w.error_rate)) },
        { label: T("Orders"), align: "right", render: (w) => fmtNumber(w.orders) },
      ], workers.workers.filter((w) => w.scans > 0), { empty: T("No scans in this period.") })),
    card(T("Photos from the floor"),
      h("p", { class: "muted small" }, T("Pictures workers took when reporting a problem: damaged stock, empty bins, labels that won't scan. Newest first.")),
      photos.length
        ? h("div", { class: "photo-grid" }, ...photos.map((ph) => h("figure", { class: "photo-card" },
          photoThumb(ph.id, "/api/photos/", T("{p0} · {p1} · order {p2}", { p0: REASONS[ph.reason] || ph.reason, p1: ph.worker || "worker", p2: ph.order_number || "" })),
          h("figcaption", null,
            h("a", { href: `#/orders/${ph.order_id}`, class: "mono" }, ph.order_number || "order"),
            h("span", { class: "muted small" }, ` · ${REASONS[ph.reason] || ph.reason} · ${ph.worker || ""} · ${fmtAgo(ph.at)}`)))))
        : h("div", { class: "empty" }, T("No photos yet. Workers can attach one when they flag a problem or report a short pick."))),
    card(T("Export"),
      h("p", { class: "muted small" }, T("Every scan is kept permanently. Exports are the audit trail if a customer disputes a shipment.")),
      h("div", { class: "row" },
        exportDays,
        h("button", { class: "btn", onclick: () => download(`/api/exports/scans.csv?days=${exportDays.value}`, "autorack-scans.csv") }, T("Download scans (CSV)")),
        h("button", { class: "btn", onclick: () => download(`/api/exports/orders.csv?days=${exportDays.value}`, "autorack-orders.csv") }, T("Download orders (CSV)")))),
  ]);
}

// ---------------------------------------------------------------------------
// Billing
// ---------------------------------------------------------------------------

export async function billingView(params) {
  if (params.get("checkout") === "success") {
    toast(T("Thanks! Your subscription is being activated."), "ok", 6000);
    history.replaceState(null, "", "#/billing");
  }
  const b = await api("/api/billing");
  const a = b.access;
  const stateText = {
    pilot: T("Free pilot"),
    trialing: a.trial_days_left !== null ? T("Free trial · {trial_days_left} day{p1} left", { trial_days_left: a.trial_days_left, p1: a.trial_days_left === 1 ? "" : "s" }) : T("Free trial"),
    trial_expired: T("Trial ended"),
    active: T("Active"),
    grace: T("Payment failed"),
    past_due: T("Past due"),
    canceled: T("Canceled"),
  }[a.state] || a.state;
  const tone = a.allowed ? (a.state === "grace" ? "warn" : "ok") : "bad";

  const plans = b.plans;
  const founding = b.founding;
  const yearSaving = plans.month.price_cents * 12 - plans.year.price_cents;
  let chosen = b.recommended_interval || "year";

  const subscribe = async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/billing/checkout", { method: "POST", body: { interval: chosen } });
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
  const subscribed = b.has_subscription && b.status !== "canceled";
  const perLabel = (i) => (i === "year" ? T("/ year") : T("/ month"));
  const was = (i) => plans[i].list_price_cents > plans[i].price_cents
    ? h("s", { class: "plan-was" }, fmtMoney(plans[i].list_price_cents)) : null;

  // Not subscribed yet: pick yearly (promoted) or monthly.
  const priceBox = h("div", { class: "plan-price-box" });
  const subscribeBtn = h("button", { class: "btn btn-primary btn-lg", onclick: subscribe });
  const toggle = h("div", { class: "period-switch", role: "radiogroup", "aria-label": T("Billing period") });
  const choose = (interval) => {
    chosen = interval;
    for (const btn of toggle.children) {
      const on = btn.dataset.interval === interval;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-checked", String(on));
    }
    const p = plans[interval];
    mount(priceBox,
      h("div", { class: "plan-price" }, was(interval), fmtMoney(p.price_cents), h("span", null, " " + perLabel(interval))),
      h("div", { class: "plan-name" }, interval === "year"
        ? T("Per warehouse · {p0} a month, 2 months free", { p0: fmtCents(Math.round(p.price_cents / 12)) })
        : T("Per warehouse · billed monthly")));
    subscribeBtn.textContent = interval === "year"
      ? T("Subscribe yearly · {p0}/year", { p0: fmtMoney(p.price_cents) })
      : T("Subscribe monthly · {p0}/month", { p0: fmtMoney(p.price_cents) });
  };
  for (const [interval, label, note] of [["year", T("Yearly"), yearSaving > 0 ? T("Save {p0}", { p0: fmtMoney(yearSaving) }) : ""], ["month", T("Monthly"), ""]]) {
    toggle.append(h("button", {
      type: "button", role: "radio", class: "period-option", "data-interval": interval, onclick: () => choose(interval),
    }, label, note ? h("em", null, note) : null));
  }
  choose(chosen);

  let planTop;
  let action = null;
  if (subscribed) {
    const i = b.interval;
    planTop = h("div", null,
      h("div", { class: "plan-price" }, was(i), fmtMoney(plans[i].price_cents), h("span", null, " " + perLabel(i))),
      h("div", { class: "plan-name" }, i === "year" ? T("Per warehouse, billed yearly") : T("Per warehouse, billed monthly")));
    action = owner ? h("button", { class: "btn btn-primary", onclick: manage }, T("Manage billing")) : null;
  } else {
    planTop = h("div", null, toggle, priceBox);
    if (b.stripe_enabled && owner) {
      action = h("div", { class: "row" }, subscribeBtn,
        b.can_manage ? h("button", { class: "btn", onclick: manage }, T("Billing history")) : null);
    }
  }
  if (!owner) action = h("p", { class: "muted" }, T("Only an owner can change billing."));
  else if (!b.stripe_enabled && !subscribed) action = h("p", { class: "muted" }, T("Online billing isn't set up yet. Contact us to subscribe."));

  const foundingBox = founding.member
    ? h("div", { class: "founding-box" },
      h("div", { class: "founding-badge" }, "★ " + T("Founding customer")),
      h("p", null, T("Your price is locked at {p0}/year or {p1}/month for as long as you stay subscribed, even when prices go up for everyone else.", {
        p0: fmtMoney(plans.year.price_cents), p1: fmtMoney(plans.month.price_cents),
      })),
      founding.since ? h("p", { class: "muted small" }, T("Founding member since {p0}.", { p0: fmtDate(founding.since, tz()) })) : null)
    : null;

  const monthlyTip = subscribed && b.interval === "month" && yearSaving > 0
    ? h("p", { class: "banner banner-info" }, T("Switch to yearly and save {p0} a year. Open “Manage billing” and choose “Update plan”.", { p0: fmtMoney(yearSaving) }))
    : null;

  layout("#/billing", [
    pageHeader(T("Billing"), T("One flat price per warehouse. No per-scan or per-seat charges.")),
    h("div", { class: "grid-main" },
      card(null,
        h("div", { class: "plan" },
          planTop,
          h("ul", { class: "checklist" },
            ...[T("Unlimited workers, phones and scans"), T("Offline scanning with automatic sync"), T("CSV import, pick sheets, live dashboard"),
              T("Full scan history and exports"), T("Worker app in English, Spanish, Chinese and Vietnamese")].map((t) => h("li", null, t)))),
        h("div", { class: `banner banner-${tone}` }, h("strong", null, stateText), a.message && a.state !== "active" ? ` — ${a.message}` : ""),
        b.current_period_end ? h("p", { class: "muted" }, b.cancel_at_period_end ? T("Ends") + " " : T("Renews") + " ", fmtDate(b.current_period_end, tz())) : null,
        founding.member && b.cancel_at_period_end
          ? h("p", { class: "banner banner-warn" }, T("Your subscription is set to end. When it does, your founding price is gone for good. Keep it by resuming in “Manage billing”."))
          : null,
        monthlyTip,
        action),
      h("div", { class: "stack" },
        foundingBox,
        card(T("How billing works"),
          h("ul", { class: "plain-list" },
            h("li", null, T("Your trial doesn't need a card. Subscribing early keeps the rest of your trial free.")),
            h("li", null, T("Yearly is two months free. You can switch between yearly and monthly any time from “Manage billing”.")),
            h("li", null, T("Cancel any time from “Manage billing”. Your data stays available to view and export.")),
            founding.member ? h("li", null, T("Cancelling ends your founding price. If you come back later, you'll pay the price at that time.")) : null,
            h("li", null, T("If a payment fails, scanning keeps working for a grace period while you update your card.")),
            h("li", null, T("If scanning is paused, scans already made on phones still sync. Nothing is lost.")))))),
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
  const [wh, team, aliases, audit, agreement] = await Promise.all([
    api("/api/warehouse"),
    api("/api/team"),
    api("/api/aliases"),
    owner ? api("/api/audit?limit=100") : Promise.resolve([]),
    api("/api/agreement"),
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

  const cost = h("input", { class: "input input-qty", type: "number", min: "0", step: "1", value: String(Math.round(wh.cost_per_error_cents / 100)), disabled: !owner });
  const checkbox = (checked, disabled = !owner) => {
    const el = h("input", { type: "checkbox", disabled });
    el.checked = checked;
    return el;
  };
  const summaryOn = checkbox(wh.daily_summary_enabled);
  const summaryHour = h("select", { class: "input input-inline", disabled: !owner },
    ...Array.from({ length: 24 }, (_, hr) => h("option", { value: String(hr), selected: hr === wh.daily_summary_hour },
      new Date(Date.UTC(2020, 0, 1, hr)).toLocaleTimeString([], { hour: "numeric", timeZone: "UTC" }))));
  const alertFlag = checkbox(wh.alert_on_flag);
  const alertRate = checkbox(wh.alert_error_rate);
  const monthlyOn = checkbox(wh.monthly_report_enabled);
  const board = checkbox(wh.leaderboard_enabled);
  const shipScan = checkbox(wh.require_ship_scan);
  const packPhoto = checkbox(wh.require_pack_photo);
  const timeClock = checkbox(wh.time_clock_enabled);
  const cutoff = h("input", { class: "input input-inline", type: "time", value: wh.ship_cutoff || "", disabled: !owner });
  const mine = ctx.me.membership;
  const myName = h("input", { class: "input", value: ctx.me.user.name || "", placeholder: T("Your name") });
  const mySummary = checkbox(mine.email_daily_summary, false);
  const myAlerts = checkbox(mine.email_alerts, false);

  const aliasScanned = h("input", { class: "input mono", placeholder: T("Barcode as scanned") });
  const aliasTarget = h("input", { class: "input mono", placeholder: T("Barcode on your orders") });

  layout("#/settings", [
    pageHeader(T("Settings")),
    card(T("Warehouse"),
      h("form", {
        class: "form-grid",
        onsubmit: (e) => {
          e.preventDefault();
          save({ name: name.value, owner_email: email.value, timezone: zone.value }, T("Saved"));
        },
      },
      h("label", null, T("Name"), name),
      h("label", null, T("Billing email"), email),
      h("label", null, T("Timezone (for “today” and reports)"), zone),
      owner ? h("div", { class: "span-2" }, h("button", { class: "btn btn-primary", type: "submit" }, T("Save"))) : null)),

    card(T("Money saved"),
      h("p", { class: "muted" }, T("What one wrong shipment costs you: the return, reshipping, a credit, the time on the phone. The dashboard, reports and daily email multiply each mistake caught by this.")),
      h("div", { class: "row" }, h("span", null, T("Cost of one mis-ship: $")), cost,
        owner ? h("button", { class: "btn", onclick: () => save({ cost_per_error_cents: Math.max(0, Math.round(Number(cost.value) * 100)) }, T("Saved")) }, T("Save")) : null)),

    card(T("Emails and alerts"),
      h("div", { class: "settings-list" },
        h("label", { class: "row check" }, summaryOn, h("span", null, h("strong", null, T("Daily summary")), h("span", { class: "muted" }, " " + T("— orders shipped, mistakes caught and money saved, sent at") + " "), summaryHour, h("span", { class: "muted" }, " " + T("warehouse time. Skipped on days with no picking.")))),
        h("label", { class: "row check" }, alertFlag, h("span", null, h("strong", null, T("Problem alerts")), h("span", { class: "muted" }, " " + T("— an email within a minute when a worker flags a problem or reports a short pick.")))),
        h("label", { class: "row check" }, alertRate, h("span", null, h("strong", null, T("Error-rate alerts")), h("span", { class: "muted" }, " " + T("— when someone's mistake rate over the last hour jumps well above normal (at most once a day per worker).")))),
        h("label", { class: "row check" }, monthlyOn, h("span", null, h("strong", null, T("Monthly report")), h("span", { class: "muted" }, " " + T("— a PDF to the owners on the 1st: what Autorack caught and saved last month, the items most often picked wrong, and the team's numbers."))))),
      owner ? h("button", {
        class: "btn",
        onclick: () => save({
          daily_summary_enabled: summaryOn.checked, daily_summary_hour: Number(summaryHour.value),
          alert_on_flag: alertFlag.checked, alert_error_rate: alertRate.checked, monthly_report_enabled: monthlyOn.checked,
        }, T("Email settings saved")),
      }, T("Save")) : h("p", { class: "muted small" }, T("An owner decides which emails this warehouse sends. You choose which you get, below."))),

    card(T("My emails"),
      h("p", { class: "muted small" }, T("For you ({email}) at {name}.", { email: ctx.me.user.email, name: ctx.me.warehouse.name })),
      h("div", { class: "settings-list" },
        h("label", null, T("Your name"), myName),
        h("label", { class: "row check" }, mySummary, T("Send me the daily summary")),
        h("label", { class: "row check" }, myAlerts, T("Send me problem and error-rate alerts"))),
      h("button", {
        class: "btn",
        onclick: () => api("/api/auth/preferences", {
          method: "PATCH", body: { email_daily_summary: mySummary.checked, email_alerts: myAlerts.checked, name: myName.value },
        }).then(async () => { toast(T("Saved"), "ok"); await loadMe(); }, fail),
      }, T("Save"))),

    card(T("On the floor"),
      h("div", { class: "settings-list" },
        h("label", { class: "row check" }, shipScan, h("span", null, h("strong", null, T("Scan the shipping label on every order")), h("span", { class: "muted" }, " " + T("— after picking, the worker scans the box's shipping label, tying the order to its tracking number. Proof of what went in which parcel. Off: it's optional.")))),
        h("label", { class: "row check" }, packPhoto, h("span", null, h("strong", null, T("Photo of every packed box")), h("span", { class: "muted" }, " " + T("— before the label goes on, the worker photographs the open box. It shows on the order, the shipment proof and the shared proof link: your answer to “it wasn't in the box”. Off: a photo is optional.")))),
        h("label", { class: "row check" }, timeClock, h("span", null, h("strong", null, T("Time clock")), h("span", { class: "muted" }, " " + T("— workers clock in when they sign in on a phone and out when they end their shift. Hours show on the Time clock page and give each worker's units per hour.")))),
        h("div", { class: "row" }, h("strong", null, T("Daily ship cutoff")), cutoff,
          h("span", { class: "muted" }, " " + T("— orders in before this time are due out the same day; later ones the next day. Orders past it show as late. Leave empty for none."))),
        h("label", { class: "row check" }, board, h("span", null, h("strong", null, T("Floor board")), h("span", { class: "muted" }, " " + T("— a live shift leaderboard (units, orders, accuracy) for a TV on the floor. Some teams love it, some don't; it's off until you turn it on."))))),
      owner ? h("button", {
        class: "btn",
        onclick: () => save({
          require_ship_scan: shipScan.checked, require_pack_photo: packPhoto.checked, leaderboard_enabled: board.checked,
          time_clock_enabled: timeClock.checked, ship_cutoff: cutoff.value || "",
        }, T("Saved")),
      }, T("Save")) : null),

    card(T("Barcode matching"),
      h("p", { class: "muted" }, T("Autorack already treats the same product's UPC-E, UPC-A, EAN-13 and GTIN-14 forms, GS1 labels, stray spaces and missing check digits as a match. Anything ambiguous goes to review instead of being guessed.")),
      h("label", { class: "row check" }, loose, T("Also match on the last digits of long numeric codes")),
      h("div", { class: "row" }, h("span", null, T("Digits to compare:")), suffix),
      h("p", { class: "muted small" }, T("Off by default. Matches found this way are never counted automatically; they're sent to review. Useful when vendors print long internal codes that end in your SKU number.")),
      owner ? h("button", {
        class: "btn",
        onclick: () => save({ loose_match_enabled: loose.checked, suffix_len: Number(suffix.value) }, T("Matching settings saved. Phones pick them up on next sync.")),
      }, T("Save matching settings")) : null),

    card(T("Taught barcodes"),
      h("p", { class: "muted small" }, T("Barcodes that should count as another barcode, like a vendor's case label for your item. You can also teach one from an order's scan history.")),
      h("form", {
        class: "inline-form",
        onsubmit: (e) => {
          e.preventDefault();
          api("/api/aliases", { method: "POST", body: { scanned_barcode: aliasScanned.value, target_barcode: aliasTarget.value } })
            .then(() => { toast(T("Barcode taught"), "ok"); reload(); }, fail);
        },
      }, aliasScanned, h("span", null, T("counts as")), aliasTarget, h("button", { class: "btn", type: "submit" }, T("Add"))),
      table([
        { label: T("Scanned"), render: (a) => h("span", { class: "mono" }, a.alias) },
        { label: T("Counts as"), render: (a) => h("span", { class: "mono" }, a.target) },
        { label: T("Note"), render: (a) => a.note || h("span", { class: "muted" }, "–") },
        { label: T("Added"), render: (a) => fmtDate(a.created_at, tz()) },
        {
          label: "",
          render: (a) => h("button", {
            class: "btn btn-sm btn-ghost",
            onclick: async () => {
              if (!(await confirmDialog(T("Remove taught barcode?"), T("{alias} will stop counting as {target}.", { alias: a.alias, target: a.target }), { confirmLabel: T("Remove"), danger: true }))) return;
              await api(`/api/aliases/${a.id}`, { method: "DELETE" }).then(reload, fail);
            },
          }, T("Remove")),
        },
      ], aliases, { empty: T("None yet.") })),

    card(T("Team"),
      h("p", { class: "muted small" }, T("Owners: everything, including billing, settings and the team. Managers: orders, workers, phones and problems. Supervisors: watch the floor and resolve problems; they can't change orders or see billing. Everyone signs in with the Google account for their email; there are no passwords.")),
      owner ? h("button", { class: "btn", onclick: () => invite(reload) }, T("Invite someone")) : null,
      table([
        { label: T("Email"), render: (u) => h("span", null, u.email, u.name ? h("span", { class: "muted" }, ` (${u.name})`) : null) },
        {
          label: T("Role"),
          render: (u) => owner && u.id !== ctx.me.user.id
            ? h("select", {
              class: "input input-inline",
              onchange: (e) => api(`/api/team/${u.id}`, { method: "PATCH", body: { role: e.target.value } }).then(reload, (err) => { fail(err); reload(); }),
            }, ...["owner", "manager", "supervisor"].map((r) => h("option", { value: r, selected: r === u.role }, r)))
            : u.role,
        },
        { label: T("Status"), render: (u) => (u.active ? T("Active") : h("span", { class: "muted" }, T("Removed"))) },
        { label: T("Last sign-in"), render: (u) => h("span", { class: "muted" }, fmtAgo(u.last_login_at)) },
        {
          label: "",
          render: (u) => owner && u.id !== ctx.me.user.id ? h("button", {
            class: "btn btn-sm btn-ghost",
            onclick: () => api(`/api/team/${u.id}`, { method: "PATCH", body: { active: !u.active } }).then(reload, fail),
          }, u.active ? T("Remove") : T("Restore")) : null,
        },
      ], team)),

    card(T("License agreement"), agreementCard(agreement)),

    owner ? card(T("Your data"), dataCard()) : null,

    card(T("Warehouses"),
      h("p", { class: "muted small" }, T("One sign-in can run several sites. Each warehouse has its own orders, workers, phones and team, and its own subscription.")),
      table([
        { label: T("Warehouse"), render: (w) => h("strong", null, w.name) },
        { label: T("Your role"), key: "role" },
        {
          label: "",
          render: (w) => (w.id === ctx.me.warehouse.id
            ? h("span", { class: "muted small" }, T("Viewing"))
            : h("button", { class: "btn btn-sm", onclick: () => switchWarehouse(w.id).catch(fail) }, T("Switch"))),
        },
      ], ctx.me.warehouses),
      owner ? h("button", { class: "btn", onclick: addWarehouse }, T("Add another warehouse")) : null),

    owner ? accountCard() : null,

    owner ? card(T("Activity log"),
      h("p", { class: "muted small" }, T("Every change made by your team, the phones, and billing. This log can't be edited.")),
      table([
        { label: T("When"), render: (e) => h("span", { class: "muted nowrap" }, fmtDateTime(e.at, tz())) },
        { label: T("Who"), render: (e) => e.actor || e.actor_type },
        { label: T("What"), render: (e) => h("span", { class: "mono small" }, e.action) },
        { label: T("Details"), render: (e) => h("span", { class: "muted small" }, summarize(e.details)) },
      ], audit, { empty: T("Nothing yet.") })) : null,
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
  const r = await dialog(T("Invite someone"), (close) => {
    const email = h("input", { class: "input", type: "email", required: true, placeholder: "name@company.com" });
    const role = h("select", { class: "input" },
      h("option", { value: "manager" }, T("Manager: runs orders, workers and phones")),
      h("option", { value: "supervisor" }, T("Supervisor: resolves problems, read-only otherwise")),
      h("option", { value: "owner" }, T("Owner: everything, including billing")));
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close({ email: email.value, role: role.value }); } },
      h("label", null, T("Email")), email, h("label", null, T("Role")), role,
      h("p", { class: "muted small" }, T("They get an email, then sign in with the Google account for that address.")),
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("Cancel")),
        h("button", { class: "btn btn-primary", type: "submit" }, T("Send invite"))));
  });
  if (!r) return;
  await api("/api/team", { method: "POST", body: r }).then(() => { toast(T("Invite sent to {email}", { email: r.email }), "ok"); reload(); }, fail);
}


async function addWarehouse() {
  const r = await dialog(T("Add a warehouse"), (close) => {
    const name = h("input", { class: "input", required: true, maxlength: "200", placeholder: T("e.g. Reno DC") });
    const zone = h("select", { class: "input" },
      ...timezones().map((z) => h("option", { value: z, selected: z === ctx.me.warehouse.timezone }, z)));
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close({ name: name.value, timezone: zone.value }); } },
      h("label", null, T("Name")), name, h("label", null, T("Timezone")), zone,
      h("p", { class: "muted small" }, T("It starts with its own free trial. You'll be its owner; invite its team from Settings once you've switched to it.")),
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("Cancel")),
        h("button", { class: "btn btn-primary", type: "submit" }, T("Create warehouse"))));
  });
  if (!r) return;
  try {
    await api("/api/auth/warehouses", { method: "POST", body: r });
    toast(T("{name} created. You're now looking at it.", { name: r.name }), "ok", 6000);
    location.hash = "#/";
    location.reload();
  } catch (e) {
    fail(e);
  }
}

function agreementCard(a) {
  const sig = a.signature;
  if (!sig) return h("p", { class: "muted" }, T("Not signed yet."));
  return h("div", { class: "stack" },
    h("p", null, T("Signed by") + " ", h("strong", null, sig.signer_name), T(", {signer_title}, for", { signer_title: sig.signer_title }) + " ", h("strong", null, sig.company_name),
      " " + T("on {signed_at} (version {version}).", { signed_at: fmtDateTime(sig.signed_at, tz()), version: sig.version })),
    h("p", { class: "muted small" }, T("The signed copy includes a signature certificate: who signed, when, from where, and a fingerprint of the exact document.")),
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => download("/api/agreement/signed.pdf", "Autorack-License-Agreement-signed.pdf") }, T("Download signed copy")),
      h("a", { class: "btn btn-ghost", href: "/privacy.html", target: "_blank", rel: "noopener" }, T("Privacy policy"))));
}

function fmtLongDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
}

function dataCard() {
  return h("div", { class: "stack" },
    h("p", null, T("Download everything this warehouse has in Autorack as one ZIP file: orders and lines, every scan, problem reports and their photos, workers (without PINs), phones, your team, taught barcodes, imports, the activity log, and your signed license agreement.")),
    h("p", { class: "muted small" }, T("Spreadsheet files (CSV) open in Excel, Numbers or Google Sheets. Large warehouses can take a minute.")),
    h("div", { class: "row" },
      h("button", {
        class: "btn btn-primary",
        onclick: async (e) => {
          e.target.disabled = true;
          e.target.textContent = T("Preparing…");
          await download("/api/account/export.zip", "autorack-export.zip");
          e.target.disabled = false;
          e.target.textContent = T("Download all data (ZIP)");
        },
      }, T("Download all data (ZIP)"))));
}

function accountCard() {
  const wh = ctx.me.warehouse;
  if (wh.closed_at) {
    return h("section", { class: "card card-danger" },
      h("h2", { class: "card-title" }, T("Account closed")),
      h("p", null, T("Closed") + " ", fmtDateTime(wh.closed_at, tz()), T(". Scanning is off and billing has stopped.")),
      h("p", null, h("strong", null, T("All data will be permanently deleted on {deletion_due_at}.", { deletion_due_at: fmtLongDate(wh.deletion_due_at) })),
        " " + T("Download a copy above before then.")),
      h("div", { class: "row" },
        h("button", {
          class: "btn btn-primary",
          onclick: async () => {
            if (!(await confirmDialog(T("Reopen this account?"), T("The deletion is cancelled and your data stays. To scan again, subscribe on the Billing page (or finish your trial)."), { confirmLabel: T("Reopen account") }))) return;
            try {
              await api("/api/account/reopen", { method: "POST" });
              toast(T("Account reopened"), "ok");
              await loadMe();
              settingsView().catch(fail);
            } catch (e) {
              fail(e);
            }
          },
        }, T("Reopen account"))));
  }
  return h("section", { class: "card card-danger" },
    h("h2", { class: "card-title" }, T("Close account")),
    h("p", null, T("Closing {name} cancels its subscription and stops all scanning right away. Your data stays available to view and download for {p1} days, then it's permanently deleted. You can reopen any time before then.", { name: wh.name, p1: ctx.me.warehouse.retention_days || 45 })),
    h("button", { class: "btn btn-danger", onclick: closeAccount }, T("Close this account…")));
}

async function closeAccount() {
  const wh = ctx.me.warehouse;
  const r = await dialog(T("Close this account?"), (close) => {
    const name = h("input", { class: "input", autocomplete: "off", placeholder: wh.name });
    const reason = h("textarea", { class: "input", rows: "2", maxlength: "500", placeholder: T("Optional: why are you leaving? It helps us improve.") });
    const btn = h("button", { class: "btn btn-danger", type: "submit", disabled: true }, T("Close account"));
    name.addEventListener("input", () => { btn.disabled = name.value.trim().toLowerCase() !== wh.name.trim().toLowerCase(); });
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close({ confirm_name: name.value, reason: reason.value || null }); } },
      h("ul", { class: "plain-list" },
        h("li", null, T("Every phone stops scanning now, and workers are signed out.")),
        h("li", null, T("The subscription is cancelled; you won't be charged again.")),
        h("li", null, T("Your data is deleted permanently after 45 days, unless you reopen before then.")),
        h("li", null, T("Download a copy first if you might need it."))),
      h("label", null, T("Type") + " ", h("strong", null, wh.name), " " + T("to confirm")), name,
      reason,
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => close(null) }, T("Keep account")), btn));
  });
  if (!r) return;
  try {
    await api("/api/account/close", { method: "POST", body: r });
    toast(T("Account closed. We've emailed you the deletion date."), "ok", 7000);
    await loadMe();
    settingsView().catch(fail);
  } catch (e) {
    fail(e);
  }
}
