// Reports over any date range (by customer, SKU, worker, day), and the
// optional floor board for a TV on the warehouse floor.

import { fmtAgo, fmtMoney, fmtNumber, fmtPercent, h, mount } from "../../../shared/dom.js";
import { columnChart } from "../chart.js";
import { api, card, ctx, download, isOwner, layout, pageHeader, poll, table, tz } from "../core.js";
import { T } from "../i18n.js";

function isoDay(d) {
  return d.toISOString().slice(0, 10);
}

/** Today in the warehouse's timezone, as a Date at local midnight UTC. */
function warehouseToday() {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit" })
    .format(new Date());
  return new Date(`${parts}T00:00:00Z`);
}

function presets() {
  const today = warehouseToday();
  const days = (n) => new Date(today.getTime() - n * 86400000);
  const firstOfMonth = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1));
  const lastMonthEnd = new Date(firstOfMonth.getTime() - 86400000);
  const lastMonthStart = new Date(Date.UTC(lastMonthEnd.getUTCFullYear(), lastMonthEnd.getUTCMonth(), 1));
  return [
    [T("Today"), today, today],
    [T("Last 7 days"), days(6), today],
    [T("Last 30 days"), days(29), today],
    [T("This month"), firstOfMonth, today],
    [T("Last month"), lastMonthStart, lastMonthEnd],
    [T("Last 90 days"), days(89), today],
  ];
}

function tile(label, value, hint, cls) {
  return h("div", { class: ["tile", cls] },
    h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value),
    hint ? h("div", { class: "tile-hint" }, hint) : null);
}

export function reportTotals(r) {
  const t = r.totals;
  return h("div", { class: "tiles" },
    tile(T("Mistakes caught"), fmtNumber(t.errors_caught), T("{pct} pick accuracy", { pct: fmtPercent(t.accuracy) }), "tile-hero"),
    tile(T("Money saved (est.)"), fmtMoney(t.money_saved_cents), T("{cost_per_error_cents} per mis-ship avoided", { cost_per_error_cents: fmtMoney(r.cost_per_error_cents) }), "tile-hero"),
    tile(T("Orders completed"), fmtNumber(t.orders_completed)),
    tile(T("Orders shipped"), fmtNumber(t.orders_shipped), T("Label scanned")),
    tile(T("Units verified"), fmtNumber(t.units_picked)),
    tile(T("Units short"), fmtNumber(t.units_short), T("{n} problem(s) reported", { n: fmtNumber(t.problems_reported) }), t.units_short ? "tile-warn" : null),
    tile(T("Sent for review"), fmtNumber(t.reviews)));
}

export function customerTable(r) {
  return table([
    { label: T("Customer"), render: (c) => h("strong", null, c.customer) },
    { label: T("Orders"), align: "right", render: (c) => fmtNumber(c.orders_touched) },
    { label: T("Shipped"), align: "right", render: (c) => fmtNumber(c.orders_shipped) },
    { label: T("Units"), align: "right", render: (c) => fmtNumber(c.units_picked) },
    { label: T("Caught"), align: "right", render: (c) => (c.errors_caught ? h("span", { class: "bad-text" }, fmtNumber(c.errors_caught)) : "0") },
    { label: T("Saved"), align: "right", render: (c) => fmtMoney(c.money_saved_cents) },
    { label: T("Short"), align: "right", render: (c) => fmtNumber(c.units_short) },
    { label: T("Accuracy"), align: "right", render: (c) => fmtPercent(c.accuracy) },
  ], r.customers, { empty: T("No picking in this range.") });
}

export function skuTable(r, limit = 50) {
  return table([
    { label: T("Item"), render: (s) => h("div", null, s.description || s.sku || "–", h("div", { class: "muted small mono" }, s.sku ? `${s.sku} · ${s.barcode}` : s.barcode)) },
    { label: T("Units"), align: "right", render: (s) => fmtNumber(s.units_picked) },
    { label: T("Mis-picks"), align: "right", render: (s) => (s.mispicks ? h("span", { class: "bad-text" }, fmtNumber(s.mispicks)) : "0") },
    { label: T("Short"), align: "right", render: (s) => (s.units_short ? h("span", { class: "warn-text" }, fmtNumber(s.units_short)) : "0") },
  ], r.skus.slice(0, limit), { empty: T("No picking in this range.") });
}

export function workerTable(r) {
  return table([
    { label: T("Worker"), render: (w) => h("strong", null, w.name) },
    { label: T("Orders"), align: "right", render: (w) => fmtNumber(w.orders) },
    { label: T("Units"), align: "right", render: (w) => fmtNumber(w.units_picked) },
    { label: T("Mistakes caught"), align: "right", render: (w) => fmtNumber(w.errors) },
    { label: T("Accuracy"), align: "right", render: (w) => fmtPercent(w.accuracy) },
    { label: T("Short reports"), align: "right", render: (w) => fmtNumber(w.short_reports) },
  ], r.workers, { empty: T("No picking in this range.") });
}

export async function reportsView(params) {
  const today = isoDay(warehouseToday());
  const to = params.get("to") || today;
  const from = params.get("from") || isoDay(new Date(new Date(`${to}T00:00:00Z`).getTime() - 29 * 86400000));
  const r = await api(`/api/reports?from=${from}&to=${to}`);

  const fromInput = h("input", { class: "input input-inline", type: "date", value: r.from, max: today });
  const toInput = h("input", { class: "input input-inline", type: "date", value: r.to, max: today });
  const go = (f, t) => { location.hash = `#/reports?from=${f}&to=${t}`; };
  // The last 12 months, for the "here's what Autorack saved you" PDF.
  const months = [];
  for (let i = 0; i < 12; i++) {
    const d = new Date(`${today.slice(0, 7)}-01T12:00:00Z`);
    d.setUTCMonth(d.getUTCMonth() - i);
    months.push([d.toISOString().slice(0, 7), d.toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" })]);
  }
  const monthPick = h("select", { class: "input input-inline", "aria-label": T("Month") },
    ...months.map(([v, label], i) => h("option", { value: v, selected: i === 1 }, label)));

  layout("#/reports", [
    pageHeader(T("Reports"), `${r.from === r.to ? r.from : `${r.from} to ${r.to}`} · ${r.timezone}`,
      monthPick,
      h("button", {
        class: "btn",
        onclick: () => download(`/api/reports/monthly.pdf?month=${monthPick.value}`, `autorack-${monthPick.value}.pdf`),
      }, T("Monthly report")),
      h("button", {
        class: "btn btn-primary",
        onclick: () => window.open(`/app/print.html?report=1&from=${r.from}&to=${r.to}`, "_blank", "noopener"),
      }, T("Download PDF"))),
    h("div", { class: "toolbar" },
      h("div", { class: "tabs" }, ...presets().map(([label, f, t]) => {
        const active = isoDay(f) === r.from && isoDay(t) === r.to;
        return h("button", { class: ["tab", active && "active"], onclick: () => go(isoDay(f), isoDay(t)) }, label);
      })),
      h("form", {
        class: "row",
        onsubmit: (e) => {
          e.preventDefault();
          go(fromInput.value, toInput.value);
        },
      }, fromInput, h("span", { class: "muted" }, "to"), toInput, h("button", { class: "btn", type: "submit" }, T("Apply")))),
    reportTotals(r),
    card(T("By day"),
      columnChart({ title: T("Units verified per day"), unit: "units", points: r.days.map((d) => ({ date: d.date, value: d.units_picked })) }),
      columnChart({ title: T("Mistakes caught per day"), unit: "mistakes", points: r.days.map((d) => ({ date: d.date, value: d.errors_caught })) })),
    card(T("By customer"),
      h("p", { class: "muted small" }, T("From the customer column of your CSV imports (or typed on the order). Orders without one are grouped together.")),
      customerTable(r)),
    h("div", { class: "grid-2col" },
      card(T("By item"), h("p", { class: "muted small" }, T("Most mis-picked and most often short first.")), skuTable(r)),
      card(T("By worker"), workerTable(r))),
    isOwner() ? h("p", { class: "muted small" },
      T("Money saved is an estimate: mistakes caught ×") + " ", fmtMoney(r.cost_per_error_cents),
      T(", your cost of one wrong shipment (returns, reshipping, credits, time).") + " ", h("a", { href: "#/settings" }, T("Change it in Settings")), ".") : null,
  ]);
}

// ---------------------------------------------------------------------------
// Floor board: today's shift, big enough to read across the floor
// ---------------------------------------------------------------------------

export async function boardView(params) {
  const tv = params.get("tv") === "1";
  const host = h("div", { class: "board" }, h("div", { class: "skeleton" }));
  const stamp = h("span", { class: "muted small live-dot" }, T("Live"));

  if (tv) {
    document.body.classList.add("tv");
    mount(document.getElementById("app"), h("main", { class: "tv-main" },
      h("div", { class: "row-between" },
        h("h1", null, ctx.me.warehouse.name, h("span", { class: "muted" }, " " + T("· Today"))),
        h("div", { class: "row" }, stamp, h("a", { class: "btn btn-sm", href: "#/board" }, T("Exit TV mode")))),
      host));
  } else {
    document.body.classList.remove("tv");
    layout("#/board", [
      pageHeader(T("Floor board"), T("Today's shift. Speed never ranks without accuracy next to it."), stamp,
        h("a", { class: "btn", href: "#/board?tv=1" }, T("TV mode"))),
      host,
    ]);
  }

  const render = (b) => {
    const t = b.totals;
    mount(host,
      h("div", { class: "tiles board-tiles" },
        tile(T("Units verified"), fmtNumber(t.units_picked)),
        tile(T("Orders completed"), fmtNumber(t.orders_completed)),
        tile(T("Mistakes caught"), fmtNumber(t.errors_caught), T("{amount} saved", { amount: fmtMoney(t.money_saved_cents) }), "tile-hero"),
        tile(T("Still to pick"), fmtNumber(t.open_orders))),
      b.workers.length
        ? h("ol", { class: "board-list" }, ...b.workers.map((w) =>
          h("li", { class: "board-row" },
            h("span", { class: "board-rank" }, String(w.rank)),
            h("span", { class: "board-name" }, w.name),
            h("span", { class: "board-stat" }, h("strong", null, fmtNumber(w.units_picked)), " " + T("units")),
            h("span", { class: "board-stat" }, h("strong", null, fmtNumber(w.orders_completed)), " " + T("orders")),
            h("span", { class: "board-stat" }, h("strong", null, fmtPercent(w.accuracy)), " " + T("accurate")),
            h("span", { class: "board-stat muted" }, w.units_per_hour ? `${w.units_per_hour}/h` : "–", " · ", fmtAgo(w.last_scan_at)))))
        : h("div", { class: "empty" }, T("No scans yet today.")));
    stamp.textContent = T("Live · {p0}", { p0: new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) });
  };
  render(await api("/api/dashboard/board"));
  poll(async () => render(await api("/api/dashboard/board")), tv ? 15000 : 10000);
}
