// Reports over any date range (by customer, SKU, worker, day), and the
// optional floor board for a TV on the warehouse floor.

import { fmtAgo, fmtMoney, fmtNumber, fmtPercent, h, mount } from "../../../shared/dom.js";
import { columnChart } from "../chart.js";
import { api, card, ctx, isOwner, layout, pageHeader, poll, table, tz } from "../core.js";

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
    ["Today", today, today],
    ["Last 7 days", days(6), today],
    ["Last 30 days", days(29), today],
    ["This month", firstOfMonth, today],
    ["Last month", lastMonthStart, lastMonthEnd],
    ["Last 90 days", days(89), today],
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
    tile("Mistakes caught", fmtNumber(t.errors_caught), `${fmtPercent(t.accuracy)} pick accuracy`, "tile-hero"),
    tile("Money saved (est.)", fmtMoney(t.money_saved_cents), `${fmtMoney(r.cost_per_error_cents)} per mis-ship avoided`, "tile-hero"),
    tile("Orders completed", fmtNumber(t.orders_completed)),
    tile("Orders shipped", fmtNumber(t.orders_shipped), "Label scanned"),
    tile("Units verified", fmtNumber(t.units_picked)),
    tile("Units short", fmtNumber(t.units_short), `${fmtNumber(t.problems_reported)} problem${t.problems_reported === 1 ? "" : "s"} reported`, t.units_short ? "tile-warn" : null),
    tile("Sent for review", fmtNumber(t.reviews)));
}

export function customerTable(r) {
  return table([
    { label: "Customer", render: (c) => h("strong", null, c.customer) },
    { label: "Orders", align: "right", render: (c) => fmtNumber(c.orders_touched) },
    { label: "Shipped", align: "right", render: (c) => fmtNumber(c.orders_shipped) },
    { label: "Units", align: "right", render: (c) => fmtNumber(c.units_picked) },
    { label: "Caught", align: "right", render: (c) => (c.errors_caught ? h("span", { class: "bad-text" }, fmtNumber(c.errors_caught)) : "0") },
    { label: "Saved", align: "right", render: (c) => fmtMoney(c.money_saved_cents) },
    { label: "Short", align: "right", render: (c) => fmtNumber(c.units_short) },
    { label: "Accuracy", align: "right", render: (c) => fmtPercent(c.accuracy) },
  ], r.customers, { empty: "No picking in this range." });
}

export function skuTable(r, limit = 50) {
  return table([
    { label: "Item", render: (s) => h("div", null, s.description || s.sku || "–", h("div", { class: "muted small mono" }, s.sku ? `${s.sku} · ${s.barcode}` : s.barcode)) },
    { label: "Units", align: "right", render: (s) => fmtNumber(s.units_picked) },
    { label: "Mis-picks", align: "right", render: (s) => (s.mispicks ? h("span", { class: "bad-text" }, fmtNumber(s.mispicks)) : "0") },
    { label: "Short", align: "right", render: (s) => (s.units_short ? h("span", { class: "warn-text" }, fmtNumber(s.units_short)) : "0") },
  ], r.skus.slice(0, limit), { empty: "No picking in this range." });
}

export function workerTable(r) {
  return table([
    { label: "Worker", render: (w) => h("strong", null, w.name) },
    { label: "Orders", align: "right", render: (w) => fmtNumber(w.orders) },
    { label: "Units", align: "right", render: (w) => fmtNumber(w.units_picked) },
    { label: "Mistakes caught", align: "right", render: (w) => fmtNumber(w.errors) },
    { label: "Accuracy", align: "right", render: (w) => fmtPercent(w.accuracy) },
    { label: "Short reports", align: "right", render: (w) => fmtNumber(w.short_reports) },
  ], r.workers, { empty: "No picking in this range." });
}

export async function reportsView(params) {
  const today = isoDay(warehouseToday());
  const to = params.get("to") || today;
  const from = params.get("from") || isoDay(new Date(new Date(`${to}T00:00:00Z`).getTime() - 29 * 86400000));
  const r = await api(`/api/reports?from=${from}&to=${to}`);

  const fromInput = h("input", { class: "input input-inline", type: "date", value: r.from, max: today });
  const toInput = h("input", { class: "input input-inline", type: "date", value: r.to, max: today });
  const go = (f, t) => { location.hash = `#/reports?from=${f}&to=${t}`; };

  layout("#/reports", [
    pageHeader("Reports", `${r.from === r.to ? r.from : `${r.from} to ${r.to}`} · ${r.timezone}`,
      h("button", {
        class: "btn btn-primary",
        onclick: () => window.open(`/app/print.html?report=1&from=${r.from}&to=${r.to}`, "_blank", "noopener"),
      }, "Download PDF")),
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
      }, fromInput, h("span", { class: "muted" }, "to"), toInput, h("button", { class: "btn", type: "submit" }, "Apply"))),
    reportTotals(r),
    card("By day",
      columnChart({ title: "Units verified per day", unit: "units", points: r.days.map((d) => ({ date: d.date, value: d.units_picked })) }),
      columnChart({ title: "Mistakes caught per day", unit: "mistakes", points: r.days.map((d) => ({ date: d.date, value: d.errors_caught })) })),
    card("By customer",
      h("p", { class: "muted small" }, "From the customer column of your CSV imports (or typed on the order). Orders without one are grouped together."),
      customerTable(r)),
    h("div", { class: "grid-2col" },
      card("By item", h("p", { class: "muted small" }, "Most mis-picked and most often short first."), skuTable(r)),
      card("By worker", workerTable(r))),
    isOwner() ? h("p", { class: "muted small" },
      "Money saved is an estimate: mistakes caught × ", fmtMoney(r.cost_per_error_cents),
      ", your cost of one wrong shipment (returns, reshipping, credits, time). ", h("a", { href: "#/settings" }, "Change it in Settings"), ".") : null,
  ]);
}

// ---------------------------------------------------------------------------
// Floor board: today's shift, big enough to read across the floor
// ---------------------------------------------------------------------------

export async function boardView(params) {
  const tv = params.get("tv") === "1";
  const host = h("div", { class: "board" }, h("div", { class: "skeleton" }));
  const stamp = h("span", { class: "muted small live-dot" }, "Live");

  if (tv) {
    document.body.classList.add("tv");
    mount(document.getElementById("app"), h("main", { class: "tv-main" },
      h("div", { class: "row-between" },
        h("h1", null, ctx.me.warehouse.name, h("span", { class: "muted" }, " · Today")),
        h("div", { class: "row" }, stamp, h("a", { class: "btn btn-sm", href: "#/board" }, "Exit TV mode"))),
      host));
  } else {
    document.body.classList.remove("tv");
    layout("#/board", [
      pageHeader("Floor board", "Today's shift. Speed never ranks without accuracy next to it.", stamp,
        h("a", { class: "btn", href: "#/board?tv=1" }, "TV mode")),
      host,
    ]);
  }

  const render = (b) => {
    const t = b.totals;
    mount(host,
      h("div", { class: "tiles board-tiles" },
        tile("Units verified", fmtNumber(t.units_picked)),
        tile("Orders completed", fmtNumber(t.orders_completed)),
        tile("Mistakes caught", fmtNumber(t.errors_caught), `${fmtMoney(t.money_saved_cents)} saved`, "tile-hero"),
        tile("Still to pick", fmtNumber(t.open_orders))),
      b.workers.length
        ? h("ol", { class: "board-list" }, ...b.workers.map((w) =>
          h("li", { class: "board-row" },
            h("span", { class: "board-rank" }, String(w.rank)),
            h("span", { class: "board-name" }, w.name),
            h("span", { class: "board-stat" }, h("strong", null, fmtNumber(w.units_picked)), " units"),
            h("span", { class: "board-stat" }, h("strong", null, fmtNumber(w.orders_completed)), " orders"),
            h("span", { class: "board-stat" }, h("strong", null, fmtPercent(w.accuracy)), " accurate"),
            h("span", { class: "board-stat muted" }, w.units_per_hour ? `${w.units_per_hour}/h` : "–", " · ", fmtAgo(w.last_scan_at)))))
        : h("div", { class: "empty" }, "No scans yet today."));
    stamp.textContent = `Live · ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  };
  render(await api("/api/dashboard/board"));
  poll(async () => render(await api("/api/dashboard/board")), tv ? 15000 : 10000);
}
