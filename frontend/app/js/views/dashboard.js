// Dashboard: three questions at a glance.
//   1. How many orders are moving / done today?
//   2. How many mistakes got caught? (the product's value, made visible)
//   3. Does any worker need attention?

import { fmtAgo, fmtNumber, fmtPercent, h, mount } from "../../../shared/dom.js";
import { api, card, layout, pageHeader, poll, statusBadge, table } from "../core.js";

function tile(label, value, { hero = false, tone = null, hint = null } = {}) {
  return h("div", { class: ["tile", hero && "tile-hero", tone && `tile-${tone}`] },
    h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value),
    hint ? h("div", { class: "tile-hint" }, hint) : null);
}

function progressCell(o) {
  const pct = o.units_expected ? Math.round((100 * o.units_scanned) / o.units_expected) : 0;
  return h("div", { class: "progress-cell" },
    h("div", { class: "bar" }, h("span", { style: { width: `${pct}%` } })),
    h("span", { class: "num small" }, `${o.units_scanned}/${o.units_expected}`));
}

export function problemLabel(result) {
  return { mismatch: "Wrong item", over_pick: "Extra unit", review: "Needs review" }[result] || result;
}

function onboarding(summary) {
  const steps = [
    ["Add your workers", "Each gets a 4-digit PIN.", "#/workers"],
    ["Link a phone", "Scan the setup QR with any phone camera.", "#/devices"],
    ["Import today's orders", "Upload the CSV pick list your system already exports.", "#/orders/import"],
    ["Print pick sheets", "Workers scan the sheet's QR to open the order.", "#/orders"],
  ];
  if (summary.orders.completed_all_time > 0) return null;
  return card("Get started",
    h("ol", { class: "steps" }, ...steps.map(([t, d, href]) =>
      h("li", null, h("a", { href }, t), h("span", { class: "muted" }, ` — ${d}`)))));
}

export async function dashboardView() {
  const tiles = h("div", { class: "tiles" }, h("div", { class: "skeleton" }));
  const liveHost = h("div", null, h("div", { class: "skeleton" }));
  const problemsHost = h("div", null);
  const attentionHost = h("div", null);
  const onboardingHost = h("div", null);
  const updated = h("span", { class: "muted small live-dot" }, "Live");

  layout("#/", [
    pageHeader("Today on the floor", null, updated),
    onboardingHost,
    tiles,
    h("div", { class: "grid-main" },
      card("Orders in motion", liveHost),
      h("div", { class: "stack-lg" },
        card("Mistakes caught", problemsHost),
        card("Workers to check on", attentionHost))),
  ]);

  const refresh = async () => {
    const [summary, live] = await Promise.all([api("/api/dashboard/summary"), api("/api/dashboard/live")]);
    const t = summary.today;
    mount(onboardingHost, onboarding(summary));
    mount(tiles,
      tile("Mistakes caught today", fmtNumber(t.errors_caught), {
        hero: true,
        hint: `${fmtNumber(t.mismatches)} wrong item · ${fmtNumber(t.over_picks)} extra unit · ${fmtNumber(summary.all_time.errors_caught)} all time`,
      }),
      tile("In progress", fmtNumber(summary.orders.in_progress)),
      tile("Completed today", fmtNumber(summary.orders.completed_today)),
      tile("Waiting to pick", fmtNumber(summary.orders.pending)),
      tile("Flagged", fmtNumber(summary.orders.flagged), { tone: summary.orders.flagged ? "warn" : null }),
      tile("Units picked today", fmtNumber(t.units_picked), { hint: `${t.active_workers} active worker${t.active_workers === 1 ? "" : "s"}` }),
      tile("First-scan accuracy", fmtPercent(t.accuracy), { hint: "Right item on the first try" }),
      tile("Needs review", fmtNumber(t.reviews), { tone: t.reviews ? "warn" : null }));

    mount(liveHost, table([
      { label: "Order", render: (o) => h("a", { href: `#/orders/${o.id}`, class: "mono" }, o.external_order_number || o.id.slice(0, 8)) },
      { label: "Status", render: (o) => statusBadge(o.status) },
      { label: "Progress", render: progressCell },
      { label: "Caught", align: "right", render: (o) => (o.errors_caught ? h("span", { class: "bad-text" }, String(o.errors_caught)) : "0") },
      { label: "Last scan", render: (o) => h("span", { class: "muted" }, fmtAgo(o.last_scan_at)) },
    ], live.orders, { empty: "Nothing is being picked right now. Orders appear here as soon as a worker scans them.", onRow: (o) => { location.hash = `#/orders/${o.id}`; } }));

    mount(problemsHost, live.problems.length
      ? h("ul", { class: "feed" }, ...live.problems.slice(0, 10).map((p) =>
        h("li", null,
          h("div", { class: "row-between" },
            h("span", { class: `badge badge-${p.result}` }, problemLabel(p.result)),
            h("span", { class: "muted small" }, fmtAgo(p.at))),
          h("div", null,
            h("strong", null, p.worker), " scanned ", h("span", { class: "mono" }, p.scanned_barcode),
            p.intended_description || p.intended_sku ? [" instead of ", h("em", null, p.intended_description || p.intended_sku)] : null,
            " on ", h("a", { href: `#/orders/${p.order_id}`, class: "mono" }, p.order_number || "order")))))
      : h("div", { class: "empty" }, "No mistakes yet today. Every wrong pick a scan catches shows up here."));
  };

  const refreshWorkers = async () => {
    const stats = await api("/api/dashboard/workers?days=7");
    const flagged = stats.workers.filter((w) => w.needs_attention);
    mount(attentionHost, flagged.length
      ? h("ul", { class: "feed" }, ...flagged.map((w) => h("li", null,
        h("div", { class: "row-between" }, h("strong", null, w.name), h("span", { class: "warn-text" }, fmtPercent(w.error_rate))),
        h("div", { class: "muted small" },
          `${w.mismatches + w.over_picks} mistakes in ${w.scans} scans over 7 days (warehouse: ${fmtPercent(stats.warehouse_error_rate)}). `,
          "Worth a look at training or shelf labels."))))
      : h("div", { class: "empty" }, "Nobody stands out this week."));
  };

  await Promise.all([refresh(), refreshWorkers()]);
  let n = 0;
  poll(async () => {
    await refresh();
    if (++n % 12 === 0) await refreshWorkers();
    updated.textContent = `Live · updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
  }, 5000);
}
