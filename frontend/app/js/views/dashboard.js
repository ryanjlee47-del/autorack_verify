// Dashboard: three questions at a glance.
//   1. How many orders are moving / done today?
//   2. How many mistakes got caught? (the product's value, made visible)
//   3. Does any worker need attention?

import { fmtAgo, fmtMoney, fmtNumber, fmtPercent, h, mount, toast } from "../../../shared/dom.js";
import { api, canManage, card, ctx, fail, layout, pageHeader, poll, statusBadge, table } from "../core.js";
import { flagItem } from "./flags.js";

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

/** First-run checklist, from real data. Hidden once done or dismissed. */
function onboarding(ob, refresh) {
  if (!ob || ob.dismissed || ob.complete) return null;
  const manage = canManage();
  const loadSample = async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/onboarding/sample", { method: "POST" });
      toast(`Loaded ${r.orders_created} sample orders. Print their barcodes and try scanning.`, "ok", 7000);
      refresh();
    } catch (err) {
      e.target.disabled = false;
      fail(err);
    }
  };
  return h("section", { class: "card onboarding" },
    h("div", { class: "row-between" },
      h("h2", { class: "card-title" }, `Get set up · ${ob.done} of ${ob.total} done`),
      manage ? h("button", {
        class: "link-btn small",
        onclick: () => api("/api/onboarding/dismiss", { method: "POST" }).then(refresh, fail),
      }, "Hide") : null),
    h("div", { class: "bar" }, h("span", { style: { width: `${Math.round((100 * ob.done) / ob.total)}%` } })),
    h("ol", { class: "checklist-steps" }, ...ob.steps.map((st) =>
      h("li", { class: st.done ? "done" : null },
        h("span", { class: "step-check", "aria-hidden": "true" }, st.done ? "✓" : ""),
        st.done ? h("span", null, st.label) : h("a", { href: st.href }, st.label)))),
    manage ? h("div", { class: "onboarding-sample" },
      h("div", null,
        h("strong", null, "Try it before importing anything. "),
        h("span", { class: "muted" }, "Load 12 sample orders and print their barcodes: scan them with a phone to see right items go green and wrong ones go red.")),
      h("div", { class: "row" },
        ob.sample_loaded
          ? h("span", { class: "ok-text small" }, "Sample orders loaded")
          : h("button", { class: "btn btn-primary btn-sm", onclick: loadSample }, "Load sample orders"),
        h("a", { class: "btn btn-sm", href: ob.sample_barcodes_url, target: "_blank", rel: "noopener" }, "Sample barcodes (PDF)"))) : null);
}

export async function dashboardView() {
  const tiles = h("div", { class: "tiles" }, h("div", { class: "skeleton" }));
  const liveHost = h("div", null, h("div", { class: "skeleton" }));
  const problemsHost = h("div", null);
  const flagsHost = h("div", null);
  const attentionHost = h("div", null);
  const onboardingHost = h("div", null);
  const updated = h("span", { class: "muted small live-dot" }, "Live");
  const wh = ctx.me.warehouse;

  layout("#/", [
    pageHeader("Today on the floor", null, updated,
      wh.leaderboard_enabled ? h("a", { class: "btn btn-sm", href: "#/board" }, "Floor board") : null,
      h("a", { class: "btn btn-sm", href: "#/reports" }, "Reports")),
    onboardingHost,
    tiles,
    h("div", { class: "grid-main" },
      h("div", { class: "stack-lg" },
        card("Needs a decision", flagsHost),
        card("Orders in motion", liveHost)),
      h("div", { class: "stack-lg" },
        card("Mistakes caught", problemsHost),
        card("Workers to check on", attentionHost))),
  ]);

  let lastFlags = null;
  const refresh = async () => {
    const [summary, live, ob] = await Promise.all([
      api("/api/dashboard/summary"),
      api("/api/dashboard/live"),
      wh.onboarding_dismissed ? Promise.resolve(null) : api("/api/onboarding"),
    ]);
    const t = summary.today;
    const o = summary.orders;
    mount(onboardingHost, onboarding(ob, () => refresh().catch(fail)));
    mount(tiles,
      tile("Mistakes caught today", fmtNumber(t.errors_caught), {
        hero: true,
        hint: `${fmtNumber(t.mismatches)} wrong item · ${fmtNumber(t.over_picks)} extra unit · ${fmtNumber(summary.all_time.errors_caught)} all time`,
      }),
      tile("Money saved today", fmtMoney(t.money_saved_cents), {
        hero: true,
        hint: `${fmtMoney(summary.cost_per_error_cents)} per mis-ship avoided · ${fmtMoney(summary.all_time.money_saved_cents)} all time`,
      }),
      tile("In progress", fmtNumber(o.in_progress)),
      tile("Completed today", fmtNumber(o.completed_today)),
      tile("Shipped today", fmtNumber(o.shipped_today), { hint: o.ready_to_ship ? `${fmtNumber(o.ready_to_ship)} ready to ship` : "Label scanned on the box" }),
      tile("Waiting to pick", fmtNumber(o.pending)),
      tile("Open problems", fmtNumber(o.open_problems), { tone: o.open_problems ? "warn" : null }),
      tile("Units picked today", fmtNumber(t.units_picked), { hint: `${t.active_workers} active worker${t.active_workers === 1 ? "" : "s"}` }),
      tile("First-scan accuracy", fmtPercent(t.accuracy), { hint: "Right item on the first try" }),
      tile("Needs review", fmtNumber(t.reviews), { tone: t.reviews ? "warn" : null }));

    // Rebuilding the queue would reload its photos every 5 seconds.
    const flagKey = live.flags.map((f) => f.id).join(",");
    if (flagKey !== lastFlags) {
      lastFlags = flagKey;
      mount(flagsHost, live.flags.length
        ? h("ul", { class: "feed" }, ...live.flags.map((f) => flagItem(f.order_id, f, {
          item: f.description || f.sku || null,
          order: f.order_number || "order",
          onDone: () => refresh().catch(fail),
        })))
        : h("div", { class: "empty" }, "Nothing waiting. When a worker flags a problem or can't find an item, it lands here with any photos they took."));
    }

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
