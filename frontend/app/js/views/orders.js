// Orders: list, detail, manual entry, CSV import.

import {
  confirmDialog, dialog, fmtAgo, fmtDateTime, fmtNumber, h, mount, svg, toast,
} from "../../../shared/dom.js";
import { api, canManage, card, download, fail, layout, pageHeader, photoStrip, statusBadge, table, tz } from "../core.js";
import { problemLabel } from "./dashboard.js";
import { flagItem } from "./flags.js";

const TABS = [
  ["due:today", "Due today"],
  ["due:late", "Late"],
  ["due:rush", "Rush"],
  ["open", "Open"],
  ["in_progress", "In progress"],
  ["flagged", "Flagged"],
  ["pending", "Not started"],
  ["completed", "Ready to ship"],
  ["shipped", "Shipped"],
  ["cancelled", "Cancelled"],
  ["", "All"],
];

// What each kind of job is called, and what it can do.
export const KINDS = {
  pick: {
    tab: "Picks", title: "Orders", col: "Order", newLabel: "New order", importLabel: "Import CSV",
    sub: "Import your pick list, print sheets, and follow every order to verified.",
    numberLabel: "Order number", numberPh: "e.g. SO-1042", created: "Order created",
  },
  receive: {
    tab: "Receiving", title: "Receiving", col: "PO", newLabel: "New receipt", importLabel: "Import POs",
    sub: "Check each delivery against its purchase order. Workers scan every item; you see what was short, over, or not on the PO.",
    numberLabel: "PO number", numberPh: "e.g. PO-2291", created: "Receipt created",
  },
  return: {
    tab: "Returns", title: "Returns", col: "Return",
    sub: "Returned parcels checked against what shipped. Start one from a shipped order, or on the phone by scanning the parcel's label.",
  },
  count: {
    tab: "Counts", title: "Cycle counts", col: "Count", newLabel: "New count", importLabel: "Import count sheet",
    sub: "Count a location or a list of items. Blind counts hide the expected quantity from the worker.",
    numberLabel: "Count name", numberPh: "e.g. Aisle A, bins 1-20", created: "Count created",
  },
};

const TALLY_TABS = [
  ["open", "Open"],
  ["in_progress", "In progress"],
  ["flagged", "Flagged"],
  ["pending", "Not started"],
  ["completed", "Finished"],
  ["cancelled", "Cancelled"],
  ["", "All"],
];

function kindOf(params) {
  const k = params.get("kind") || "pick";
  return KINDS[k] ? k : "pick";
}

function kindTabs(kind) {
  return h("div", { class: "tabs kind-tabs" }, ...Object.entries(KINDS).map(([k, v]) =>
    h("a", { class: ["tab", kind === k && "active"], href: k === "pick" ? "#/orders" : `#/orders?kind=${k}` }, v.tab)));
}

function printSheets(ids) {
  if (!ids.length) return toast("Select orders to print first.", "warn");
  window.open(`/app/print.html?ids=${ids.join(",")}`, "_blank", "noopener");
}

function openProof(id) {
  window.open(`/app/print.html?proof=${id}`, "_blank", "noopener");
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export async function ordersView(params) {
  const kind = kindOf(params);
  const K = KINDS[kind];
  const tally = kind !== "pick";
  const status = params.get("status") ?? "open";
  const q = params.get("q") || "";
  const clientId = params.get("client") || "";
  const selected = new Set();
  const listHost = h("div", null, h("div", { class: "skeleton" }));
  const moreHost = h("div", { class: "row center-row" });
  const batchesHost = h("div");
  let offset = 0;
  let rows = [];

  const search = h("input", { class: "input", type: "search", placeholder: "Search order #, customer, tracking, barcode, SKU", value: q });
  const go = (next) => {
    const p = new URLSearchParams({ status: next.status ?? status, q: next.q ?? search.value });
    if (tally) p.set("kind", kind);
    if (clientId) p.set("client", clientId);
    location.hash = `#/orders?${p}`;
  };
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") go({});
  });

  const q2 = tally ? `?kind=${kind}` : "";
  layout("#/orders", [
    canManage() && K.newLabel
      ? pageHeader(K.title, K.sub,
        h("a", { class: "btn", href: "/api/orders/template.csv", onclick: (e) => { e.preventDefault(); download("/api/orders/template.csv", "autorack-orders-template.csv"); } }, "CSV template"),
        h("a", { class: "btn", href: `#/orders/new${q2}` }, K.newLabel),
        h("a", { class: "btn btn-primary", href: `#/orders/import${q2}` }, K.importLabel))
      : pageHeader(K.title, kind === "pick" ? "Follow every order to verified and shipped." : K.sub),
    kindTabs(kind),
    h("div", { class: "toolbar" },
      h("div", { class: "tabs" }, ...(tally ? TALLY_TABS : TABS).map(([value, label]) =>
        h("button", { class: ["tab", status === value && "active"], onclick: () => go({ status: value }) }, label))),
      h("div", { class: "row" }, search,
        h("button", { class: "btn", onclick: () => printSheets([...selected]) }, "Print selected"),
        !tally && canManage()
          ? h("button", { class: "btn", title: "Pick several orders in one walk, one tote each", onclick: () => batchPick([...selected]) }, "Batch pick")
          : null)),
    tally ? null : batchesHost,
    card(null, listHost, moreHost),
  ]);
  if (!tally) loadBatches(batchesHost).catch(() => {});

  const render = () => {
    const allBox = h("input", {
      type: "checkbox", "aria-label": "Select all",
      onchange: (e) => {
        rows.forEach((r) => (e.target.checked ? selected.add(r.id) : selected.delete(r.id)));
        render();
      },
    });
    allBox.checked = rows.length > 0 && rows.every((r) => selected.has(r.id));
    const cols = [
      {
        label: allBox,
        render: (o) => {
          const box = h("input", {
            type: "checkbox", "aria-label": "Select",
            onclick: (e) => e.stopPropagation(),
            onchange: (e) => (e.target.checked ? selected.add(o.id) : selected.delete(o.id)),
          });
          box.checked = selected.has(o.id);
          return box;
        },
      },
      {
        label: K.col,
        render: (o) => h("span", null,
          o.rush ? h("span", { class: "badge badge-rush" }, "Rush") : null,
          h("span", { class: "mono strong" }, o.external_order_number || o.id.slice(0, 8)),
          o.tote && o.batch_id ? h("span", { class: "tote-chip", title: "In a batch: this order's tote" }, o.tote) : null,
          o.client ? h("div", { class: "muted small" }, o.client) : null),
      },
      kind === "count" ? null : { label: kind === "receive" ? "Supplier" : "Customer", render: (o) => o.customer || h("span", { class: "muted" }, "–") },
      { label: "Status", render: (o) => statusBadge(tally && o.status === "completed" ? "finished" : o.status) },
      { label: "Lines", align: "right", render: (o) => String(o.line_count) },
      { label: tally ? "Counted" : "Units", align: "right", render: (o) => `${o.units_scanned}/${o.units_expected}${o.blind ? " (blind)" : ""}` },
      tally ? null : { label: "Caught", align: "right", render: (o) => (o.errors_caught ? h("span", { class: "bad-text" }, String(o.errors_caught)) : "0") },
      tally ? null : {
        label: "Ship by",
        render: (o) => (o.due_at
          ? h("span", { class: o.late ? "bad-text strong" : "" }, o.late ? "Late · " : "", fmtDateTime(o.due_at, tz()))
          : h("span", { class: "muted" }, "–")),
      },
      { label: "Assigned", render: (o) => o.assigned_worker || h("span", { class: "muted" }, "Anyone") },
      { label: "Created", render: (o) => h("span", { class: "muted" }, fmtDateTime(o.created_at, tz())) },
    ].filter(Boolean);
    mount(listHost, table(cols, rows, {
      empty: status === "open"
        ? { pick: "No open orders. Import a CSV or create one by hand.", receive: "Nothing to receive. Create a receipt or import your purchase orders.", return: "No open returns. Start one from a shipped order, or on the phone with “Start a return”.", count: "No counts open. Create one or import a count sheet." }[kind]
        : "Nothing matches.",
      onRow: (o) => { location.hash = `#/orders/${o.id}`; },
    }));
  };

  const load = async () => {
    const p = new URLSearchParams({ limit: "100", offset: String(offset), kind });
    if (status.startsWith("due:")) p.set("due", status.slice(4));
    else if (status) p.set("status", status);
    if (clientId) p.set("client_id", clientId);
    if (q) p.set("q", q);
    const r = await api(`/api/orders?${p}`);
    rows = rows.concat(r.orders);
    offset += r.orders.length;
    render();
    mount(moreHost, r.has_more ? h("button", { class: "btn", onclick: () => load().catch(fail) }, "Load more") : null);
  };
  await load();
}

// ---------------------------------------------------------------------------
// Batch picking
// ---------------------------------------------------------------------------

async function batchPick(ids) {
  if (ids.length < 2) return toast("Select 2 to 12 orders to pick together.", "warn");
  if (ids.length > 12) return toast("A batch holds at most 12 orders, one per tote.", "warn");
  const workers = (await api("/api/workers")).workers.filter((w) => w.active);
  const who = h("select", { class: "input" }, h("option", { value: "" }, "Anyone"),
    ...workers.map((w) => h("option", { value: w.worker_id }, w.name)));
  const ok = await dialog("Batch pick", (close) => [
    h("p", null, `${ids.length} orders, picked in one walk. Each gets a tote (A, B, C…); the phone says which tote every item goes in.`),
    h("label", null, "Picked by", who),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", onclick: () => close(false) }, "Cancel"),
      h("button", { class: "btn btn-primary", onclick: () => close(true) }, "Create batch")),
  ]);
  if (!ok) return;
  try {
    const b = await api("/api/batches", { method: "POST", body: { order_ids: ids, worker_id: who.value || null } });
    toast(`Batch ${b.number} is on the phones`, "ok");
    location.hash = `#/orders?status=open&b=${Date.now()}`;
  } catch (e) {
    fail(e);
  }
}

async function loadBatches(host) {
  const r = await api("/api/batches");
  if (!r.open.length) return mount(host);
  mount(host, card(`Batches being picked (${r.open.length})`,
    h("ul", { class: "feed" }, ...r.open.map((b) => h("li", { class: "feed-item batch-item" },
      h("div", null,
        h("strong", null, `Batch ${b.number}`), " · ",
        `${b.orders_left} of ${b.order_count} orders left · ${b.units_scanned}/${b.units_expected} units`,
        b.assigned_worker ? ` · ${b.assigned_worker}` : "",
        h("div", { class: "batch-totes" }, ...b.orders.map((o) => h("a", { href: `#/orders/${o.id}`, class: "batch-tote" },
          h("span", { class: "tote-chip" }, o.tote), " ", o.external_order_number || o.id.slice(0, 8),
          o.status === "completed" || o.status === "shipped" ? " ✓" : "")))),
      canManage() ? h("button", {
        class: "btn btn-sm",
        onclick: async () => {
          if (!(await confirmDialog("Break up this batch?", "Orders not yet picked go back to the normal list. Nothing scanned is lost.", { confirmLabel: "Break up" }))) return;
          api(`/api/batches/${b.id}`, { method: "DELETE" }).then(() => loadBatches(host), fail);
        },
      }, "Break up") : null)))));
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

const TIER_NAMES = ["exact", "normalized", "same GTIN", "digits", "no check digit", "taught alias", "suffix"];

export async function orderDetailView(id) {
  const [order, scans, workers] = await Promise.all([
    api(`/api/orders/${id}`),
    api(`/api/orders/${id}/scans`),
    api("/api/workers"),
  ]);
  // A finished receipt/return/count is a record: reopen it to change it.
  const finishedTally = order.kind && order.kind !== "pick" && Boolean(order.completed_at);
  const editable = canManage() && order.status !== "cancelled" && order.status !== "shipped" && !finishedTally;
  const reload = () => orderDetailView(id).catch(fail);
  const linesById = new Map(order.lines.map((l) => [l.id, l]));

  const assign = h("select", {
    class: "input input-inline",
    disabled: !editable,
    onchange: async (e) => {
      const v = e.target.value;
      try {
        await api(`/api/orders/${id}`, { method: "PATCH", body: v ? { assigned_worker_id: v } : { clear_assignment: true } });
        toast("Assignment saved", "ok");
      } catch (err) {
        fail(err);
      }
    },
  }, h("option", { value: "" }, order.kind && order.kind !== "pick" ? "Anyone" : "Anyone can pick"),
  ...workers.workers.filter((w) => w.active).map((w) =>
    h("option", { value: w.worker_id, selected: w.worker_id === order.assigned_worker_id }, w.name)));

  const shipping = order.kind === "pick" ? await shippingCard(order, editable, reload) : null;
  const openFlags = order.flags.filter((f) => !f.resolved_at);
  const closedFlags = order.flags.filter((f) => f.resolved_at);
  const flagLine = (f) => (f.line_item_id && linesById.get(f.line_item_id) ? lineName(linesById.get(f.line_item_id)) : "Whole order");
  const shipped = order.status === "shipped";
  const tally = order.kind && order.kind !== "pick";
  const K = KINDS[order.kind] || KINDS.pick;
  const back = tally ? `#/orders?kind=${order.kind}` : "#/orders";

  layout("#/orders", [
    h("a", { href: back, class: "back-link" }, `← ${K.title}`),
    h("div", { class: "page-head" },
      h("div", null,
        h("h1", { class: "row" },
          tally ? h("span", { class: `badge badge-kind badge-kind-${order.kind}` }, K.tab.replace(/s$/, "")) : null,
          h("span", { class: "mono" }, order.external_order_number || K.col),
          statusBadge(tally && order.status === "completed" ? "finished" : order.status)),
        order.return_of ? h("p", null, "Return of ", h("a", { href: `#/orders/${order.return_of.id}`, class: "mono" }, order.return_of.number || "order"),
          order.return_of.tracking_number ? h("span", { class: "muted" }, ` · shipped as ${order.return_of.tracking_number}`) : null) : null,
        h("p", { class: "muted" },
          order.customer ? [h("strong", null, order.customer), " · "] : null,
          tally
            ? `${order.line_count} lines · ${order.units_scanned} of ${order.units_expected} counted${order.blind ? " · blind count" : ""}`
            : `${order.line_count} lines · ${order.units_scanned}/${order.units_expected} units verified`,
          order.units_short ? ` · ${order.units_short} short` : "",
          ` · created ${fmtDateTime(order.created_at, tz())}`,
          order.completed_at ? ` · ${tally ? "finished" : "completed"} ${fmtDateTime(order.completed_at, tz())}` : "",
          order.variance && order.variance.finished_by ? ` by ${order.variance.finished_by}` : ""),
        order.returns && order.returns.length ? h("p", null, "Returns: ", ...order.returns.flatMap((r, i) => [
          i ? ", " : "", h("a", { href: `#/orders/${r.id}`, class: "mono" }, r.number)])) : null,
        shipped ? h("p", { class: "ship-line" },
          "Shipped ", fmtDateTime(order.shipped_at, tz()), order.shipped_by ? ` by ${order.shipped_by}` : "",
          " · ", order.carrier ? `${order.carrier} ` : "", h("span", { class: "mono strong" }, order.tracking_number)) : null,
        order.batch ? h("p", null, "Picked in batch ", h("strong", null, order.batch.number), " · tote ", h("span", { class: "tote-chip" }, order.batch.tote || "?")) : null,
        pushLine(order, reload)),
      h("div", { class: "row" },
        !tally && (shipped || order.status === "completed")
          ? h("button", { class: shipped ? "btn btn-primary" : "btn", onclick: () => openProof(id) }, "Shipment proof")
          : null,
        !tally && (shipped || order.status === "completed") && canManage()
          ? h("button", { class: "btn", onclick: () => shareProof(order, reload) }, order.share_url ? "Shared link" : "Share proof")
          : null,
        !tally && (shipped || order.status === "completed")
          ? h("button", {
            class: "btn",
            title: "One PDF with the scan log, boxes, tracking and box photos, for a carrier claim or a marketplace dispute",
            onclick: () => download(`/api/orders/${id}/claim.pdf`, `autorack-evidence-${order.external_order_number || id.slice(0, 8)}.pdf`),
          }, "Evidence pack (PDF)")
          : null,
        !tally && (shipped || order.status === "completed") && canManage()
          ? h("button", {
            class: "btn",
            onclick: async () => {
              if (!(await confirmDialog("Start a return?", "Makes a return listing what shipped on this order. A worker scans what came back, and you see anything missing or not from this order.", { confirmLabel: "Start return" }))) return;
              api(`/api/orders/${id}/return`, { method: "POST" }).then((r) => { location.hash = `#/orders/${r.id}`; }, fail);
            },
          }, "Start a return")
          : null,
        tally ? h("button", {
          class: "btn",
          onclick: () => download(`/api/orders/${id}/variance.csv`, `autorack-${order.kind}-${order.external_order_number || id.slice(0, 8)}.csv`),
        }, "Download results (CSV)") : null,
        tally && order.completed_at && canManage() && order.status !== "cancelled" ? h("button", {
          class: "btn",
          onclick: async () => {
            if (!(await confirmDialog("Reopen?", "Workers can scan it again and finish it again. Nothing counted so far is lost.", { confirmLabel: "Reopen" }))) return;
            api(`/api/orders/${id}/reopen`, { method: "POST" }).then(reload, fail);
          },
        }, "Reopen") : null,
        order.status !== "shipped" && !(tally && order.completed_at) ? h("button", { class: "btn", onclick: () => printSheets([id]) }, tally ? "Print sheet" : "Print pick sheet") : null,
        editable ? h("button", {
          class: "btn btn-danger",
          onclick: async () => {
            if (!(await confirmDialog(tally ? "Cancel this?" : "Cancel this order?", tally ? "It disappears from the phones. What was counted is kept." : "Workers will be told to put its items back. Its scan history is kept.", { confirmLabel: tally ? "Cancel it" : "Cancel order", danger: true }))) return;
            await api(`/api/orders/${id}/cancel`, { method: "POST" }).then(reload, fail);
          },
        }, tally ? "Cancel" : "Cancel order") : null)),

    openFlags.length ? card(`Needs a decision (${openFlags.length})`,
      h("ul", { class: "feed" }, ...openFlags.map((f) => flagItem(id, f, { item: flagLine(f), onDone: reload })))) : null,

    tally ? varianceCard(order) : null,

    h("div", { class: "grid-main" },
      card(tally ? "List" : "Lines",
        linesTable(order, editable, reload),
        editable ? addLineForm(id, reload, order.kind === "count" ? 0 : 1) : null),
      h("div", { class: "stack-lg" },
        card(tally ? "Sheet QR" : "Pick sheet QR",
          h("div", { class: "qr-box" }, svg(order.qr_svg, "qr")),
          h("p", { class: "muted small" }, `Workers scan this (on the printed sheet) to open the ${tally ? K.col.toLowerCase() : "order"} on their phone.`)),
        card("Assignment", assign, h("p", { class: "muted small" }, `Assigned ${tally ? "jobs" : "orders"} show first on that worker's phone and are hidden from others.`)),
        tally ? null : shipping,
        card("Notes", notesEditor(order, editable)))),

    order.packages && order.packages.length > 1
      ? card(`Shipped in ${order.packages.length} boxes`, table([
        { label: "Box", render: (b) => `Box ${b.box}` },
        { label: "Tracking", render: (b) => h("span", { class: "mono" }, `${b.carrier ? `${b.carrier} ` : ""}${b.tracking_number}`) },
        { label: "Labelled", render: (b) => `${fmtDateTime(b.at, tz())}${b.worker ? ` · ${b.worker}` : ""}` },
        { label: "Photos", render: (b) => photoStrip(b.photos) || h("span", { class: "muted" }, "–") },
      ], order.packages))
      : null,
    order.inserts && order.inserts.length
      ? card("Inserts", h("ul", { class: "plain-list" }, ...order.inserts.map((i) =>
        h("li", null, i.done ? "✓ " : "☐ ", i.name, i.done ? "" : h("span", { class: "muted" }, " (not yet)")))))
      : null,
    order.pack_photos && order.pack_photos.length && !(order.packages && order.packages.length > 1)
      ? card(`The packed box (${order.pack_photos.length})`, photoStrip(order.pack_photos),
        h("p", { class: "muted small" }, "Taken at the packing bench before the label went on. Shown on the shipment proof and the shared link."))
      : null,

    closedFlags.length ? card("Resolved problems",
      h("ul", { class: "feed" }, ...closedFlags.map((f) => flagItem(id, f, { item: flagLine(f), onDone: reload })))) : null,

    card("Scan history", scanHistory(order, scans, linesById, reload, editable)),
  ]);
}

/** Rush, ship-by date and client: when and for whom it ships. */
async function shippingCard(order, editable, reload) {
  const clients = await api("/api/clients").catch(() => []);
  const rush = h("input", { type: "checkbox", disabled: !editable });
  rush.checked = Boolean(order.rush);
  const shipBy = h("input", { class: "input", type: "date", disabled: !editable, value: order.ship_by ? order.ship_by.slice(0, 10) : "" });
  const client = h("select", { class: "input", disabled: !editable }, h("option", { value: "" }, "None"),
    ...clients.map((c) => h("option", { value: c.id, selected: c.id === order.client_id }, c.name)));
  const save = () => api(`/api/orders/${order.id}`, {
    method: "PATCH",
    body: {
      rush: rush.checked,
      ...(shipBy.value ? { ship_by: shipBy.value } : { clear_ship_by: true }),
      ...(client.value ? { client_id: client.value } : { clear_client: true }),
    },
  }).then(() => { toast("Saved", "ok"); reload(); }, fail);
  return card("Shipping",
    order.late ? h("div", { class: "banner banner-bad" }, "Late: it should have shipped by ", fmtDateTime(order.due_at, tz()), ".") : null,
    h("label", { class: "row check" }, rush, "Rush (top of every worker's list)"),
    h("label", null, "Ship by", shipBy),
    order.due_at && !order.ship_by ? h("p", { class: "muted small" }, `Due ${fmtDateTime(order.due_at, tz())} by the daily cutoff.`) : null,
    clients.length ? h("label", null, "Client", client) : null,
    editable ? h("button", { class: "btn", onclick: save }, "Save") : null);
}

async function shareProof(order, reload) {
  let url = order.share_url;
  if (!url) {
    try {
      url = (await api(`/api/orders/${order.id}/share`, { method: "POST" })).url;
    } catch (e) {
      fail(e);
      return;
    }
  }
  const off = await dialog("Share proof of shipment", (close) => {
    const input = h("input", { class: "input mono", readonly: true, value: url, onclick: (e) => e.target.select() });
    return [
      h("p", null, "Anyone with this link sees what was ordered, every unit's scan with its time (and lot/serial numbers), the tracking number and how many wrong items were caught. It doesn't show who picked it, notes or problem reports."),
      h("div", { class: "row nowrap copy-row" }, input,
        h("button", { class: "btn btn-primary", onclick: () => navigator.clipboard.writeText(url).then(() => toast("Link copied", "ok"), fail) }, "Copy")),
      h("p", null, h("a", { href: url, target: "_blank", rel: "noopener" }, "Open the page →")),
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn btn-ghost danger-text", onclick: () => close(true) }, "Turn off link"),
        h("button", { class: "btn", onclick: () => close(false) }, "Done")),
    ];
  });
  if (off) {
    await api(`/api/orders/${order.id}/share`, { method: "DELETE" }).then(() => toast("Link turned off", "ok"), fail);
  }
  reload();
}

function varianceCard(order) {
  const v = order.variance;
  if (!v) return null;
  const t = v.totals;
  const stateCell = (r) => {
    if (r.state === "ok") return h("span", { class: "badge badge-ok" }, "Matches");
    return h("span", { class: `badge ${r.state === "short" ? "badge-bad" : "badge-warn"}` },
      r.state === "short" ? `${-r.difference} short` : `${r.difference} over`);
  };
  const differing = v.lines.filter((r) => r.state !== "ok");
  const heading = !v.finished
    ? "Results so far"
    : v.matches ? "Everything matched" : "Differences";
  return card(heading,
    !v.finished ? h("p", { class: "muted small" }, "Not finished yet. The worker taps Finish on the phone when everything is scanned.") : null,
    h("div", { class: "tiles tiles-sm" },
      h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Expected"), h("div", { class: "tile-value" }, fmtNumber(t.expected))),
      h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Counted"), h("div", { class: "tile-value" }, fmtNumber(t.counted))),
      h("div", { class: ["tile", t.lines_short && "tile-bad"] }, h("div", { class: "tile-label" }, "Lines short"), h("div", { class: "tile-value" }, fmtNumber(t.lines_short))),
      h("div", { class: ["tile", t.lines_over && "tile-warn"] }, h("div", { class: "tile-label" }, "Lines over"), h("div", { class: "tile-value" }, fmtNumber(t.lines_over))),
      h("div", { class: ["tile", t.extra && "tile-warn"] }, h("div", { class: "tile-label" }, order.kind === "return" ? "Not from this order" : "Not on the list"), h("div", { class: "tile-value" }, fmtNumber(t.extra)))),
    differing.length ? table([
      { label: "Item", render: (r) => h("div", null, r.description || r.sku || "–", h("div", { class: "muted small mono" }, r.barcode)) },
      { label: "Location", render: (r) => r.location || h("span", { class: "muted" }, "–") },
      { label: "Expected", align: "right", render: (r) => fmtNumber(r.expected) },
      { label: "Counted", align: "right", render: (r) => fmtNumber(r.counted) },
      { label: "", render: stateCell },
    ], differing) : null,
    v.extras.length ? h("div", { class: "stack" },
      h("h3", null, order.kind === "return" ? "Items that didn't ship on this order" : "Scanned but not on the list"),
      table([
        { label: "Barcode", render: (e) => h("span", { class: "mono" }, e.barcode) },
        { label: "Count", align: "right", render: (e) => fmtNumber(e.counted) },
      ], v.extras)) : null);
}

const PROBLEM_LABELS = {
  wrong_lot: "Wrong lot",
  expired: "Expired",
  serial_repeat: "Serial already picked",
  details_missing: "Lot/serial/expiry not recorded",
};

function traceText(s) {
  return [s.lot && `Lot ${s.lot}`, s.serial && `S/N ${s.serial}`, s.expiry && `Exp ${s.expiry}`].filter(Boolean).join(" · ");
}

function traceBadges(l) {
  const tags = [
    l.required_lot ? `Lot ${l.required_lot} only` : l.track_lot ? "Lot" : null,
    l.track_serial ? "Serial" : null,
    l.track_expiry ? "Expiry" : null,
  ].filter(Boolean);
  return tags.length ? h("div", { class: "row trace-tags" }, ...tags.map((t) => h("span", { class: "badge badge-trace" }, t))) : null;
}

const TRACK_OPTIONS = [
  ["", "—"],
  ["lot", "Lot"],
  ["lot+expiry", "Lot + expiry"],
  ["expiry", "Expiry"],
  ["serial", "Serial"],
];

function trackFlags(value) {
  return {
    track_lot: value.includes("lot"),
    track_serial: value.includes("serial"),
    track_expiry: value.includes("expiry"),
  };
}

function lineName(l) {
  return l.description || l.sku || l.expected_barcode;
}

function linesTable(order, editable, reload) {
  const cols = [
    { label: "Location", render: (l) => l.location || h("span", { class: "muted" }, "–") },
    {
      label: "Item",
      render: (l) => {
        const prod = l.product_id && order.products ? order.products[l.product_id] : null;
        return h("div", { class: "line-item-cell" },
          prod && prod.thumb ? h("img", { class: "product-thumb", src: prod.thumb, alt: "" }) : null,
          h("div", null,
            l.product_id ? h("a", { href: `#/products/${l.product_id}` }, l.description || l.sku || "–") : h("div", null, l.description || l.sku || "–"),
            l.sku && l.description ? h("div", { class: "muted small mono" }, l.sku) : null,
            l.kit_name ? h("div", { class: "muted small" }, `Part of kit: ${l.kit_name}`) : null,
            l.confirm_without_scan ? h("div", { class: "muted small" }, "No barcode: confirmed by tap") : null,
            prod && prod.packer_note ? h("div", { class: "warn-text small" }, "⚠ ", prod.packer_note) : null,
            traceBadges(l)));
      },
    },
    { label: "Barcode", render: (l) => h("span", { class: "mono" }, l.expected_barcode) },
    {
      label: order.kind && order.kind !== "pick" ? "Counted" : "Verified", align: "right",
      render: (l) => h("span", null,
        h("span", { class: l.scanned_quantity >= l.expected_quantity ? "ok-text" : null }, `${l.scanned_quantity}/${l.expected_quantity}`),
        l.short_quantity ? h("span", { class: "badge badge-warn badge-inline" }, `${l.short_quantity} short`) : null),
    },
    order.kind && order.kind !== "pick" ? null : { label: "Wrong picks", align: "right", render: (l) => (l.mismatches ? h("span", { class: "bad-text" }, String(l.mismatches)) : "0") },
  ].filter(Boolean);
  if (editable) {
    cols.push({
      label: "",
      render: (l) => h("div", { class: "row nowrap" },
        h("button", { class: "btn btn-sm", onclick: () => editLine(order.id, l, reload) }, "Edit"),
        h("button", {
          class: "btn btn-sm btn-ghost",
          onclick: async () => {
            if (!(await confirmDialog("Delete line?", `Remove ${lineName(l)} from this order?`, { confirmLabel: "Delete", danger: true }))) return;
            await api(`/api/orders/${order.id}/lines/${l.id}`, { method: "DELETE" }).then(reload, fail);
          },
        }, "Delete")),
    });
  }
  return table(cols, order.lines);
}

async function editLine(orderId, line, reload) {
  const result = await dialog("Edit line", (close) => {
    const f = {
      barcode: h("input", { class: "input mono", value: line.expected_barcode }),
      quantity: h("input", { class: "input", type: "number", min: "0", value: String(line.expected_quantity) }),
      sku: h("input", { class: "input", value: line.sku || "" }),
      description: h("input", { class: "input", value: line.description || "" }),
      location: h("input", { class: "input", value: line.location || "" }),
      track_lot: h("input", { type: "checkbox", checked: line.track_lot }),
      track_serial: h("input", { type: "checkbox", checked: line.track_serial }),
      track_expiry: h("input", { type: "checkbox", checked: line.track_expiry }),
      required_lot: h("input", { class: "input mono", value: line.required_lot || "", placeholder: "Any lot" }),
    };
    return h("form", {
      class: "form-grid",
      onsubmit: (e) => {
        e.preventDefault();
        close({
          barcode: f.barcode.value.trim(), quantity: Number(f.quantity.value), sku: f.sku.value, description: f.description.value, location: f.location.value,
          track_lot: f.track_lot.checked, track_serial: f.track_serial.checked, track_expiry: f.track_expiry.checked, required_lot: f.required_lot.value.trim(),
        });
      },
    },
    h("label", null, "Barcode", f.barcode), h("label", null, "Quantity", f.quantity),
    h("label", null, "SKU", f.sku), h("label", null, "Location", f.location),
    h("label", { class: "span-2" }, "Description", f.description),
    h("fieldset", { class: "span-2 trace-fields" },
      h("legend", null, "Record for each unit"),
      h("label", { class: "check" }, f.track_lot, " Lot / batch"),
      h("label", { class: "check" }, f.track_serial, " Serial number"),
      h("label", { class: "check" }, f.track_expiry, " Expiry date")),
    h("label", { class: "span-2" }, "Only this lot may ship", f.required_lot),
    h("p", { class: "muted small span-2" }, "A line's barcode can't change after it has been scanned."),
    h("div", { class: "dialog-actions span-2" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
      h("button", { class: "btn btn-primary", type: "submit" }, "Save")));
  });
  if (!result) return;
  const body = { ...result };
  if (body.barcode === line.expected_barcode) delete body.barcode;
  await api(`/api/orders/${orderId}/lines/${line.id}`, { method: "PATCH", body }).then(reload, fail);
}

function addLineForm(orderId, reload, minQty = 1) {
  const barcode = h("input", { class: "input mono", placeholder: "Barcode", required: true });
  const qty = h("input", { class: "input input-qty", type: "number", min: String(minQty), value: "1", "aria-label": "Quantity" });
  const desc = h("input", { class: "input", placeholder: "Description (optional)" });
  const loc = h("input", { class: "input input-qty", placeholder: "Location" });
  return h("form", {
    class: "inline-form",
    onsubmit: async (e) => {
      e.preventDefault();
      await api(`/api/orders/${orderId}/lines`, {
        method: "POST", body: { barcode: barcode.value, quantity: Number(qty.value), description: desc.value || null, location: loc.value || null },
      }).then(reload, fail);
    },
  }, barcode, qty, desc, loc, h("button", { class: "btn", type: "submit" }, "Add line"));
}

function notesEditor(order, editable) {
  const area = h("textarea", { class: "input", rows: "3", disabled: !editable, placeholder: "Packing instructions, customer notes…" });
  area.value = order.notes || "";
  return h("div", { class: "stack" }, area,
    editable ? h("button", {
      class: "btn btn-sm",
      onclick: () => api(`/api/orders/${order.id}`, { method: "PATCH", body: { notes: area.value } }).then(() => toast("Notes saved", "ok"), fail),
    }, "Save notes") : null);
}

function scanHistory(order, scans, linesById, reload, editable) {
  const resultLabel = { match: "Right item", void: "Undone", counted: "Counted", extra: order.kind === "return" ? "Not from this order" : "Not on the list", uncounted: "Undone", ...{ mismatch: problemLabel("mismatch"), over_pick: problemLabel("over_pick"), review: problemLabel("review") } };
  return table([
    { label: "When", render: (s) => h("span", { class: "muted" }, fmtDateTime(s.at, tz())) },
    { label: "Worker", key: "worker" },
    {
      label: "Result",
      render: (s) => h("span", { class: "row nowrap" },
        h("span", { class: `badge badge-${s.voided ? "void" : s.result}` }, resultLabel[s.result] || s.result),
        s.quantity > 1 ? h("span", { class: "badge badge-inline" }, `×${s.quantity}`) : null,
        s.substitution ? h("span", { class: "badge badge-warn badge-inline", title: "An approved substitute" }, "Substitute") : null,
        s.confirmed ? h("span", { class: "badge badge-warn badge-inline", title: "Confirmed by tapping: no barcode" }, "Tapped") : null,
        s.was_offline ? h("span", { class: "muted small", title: "Scanned offline, synced later" }, "offline") : null),
    },
    {
      label: "Scanned",
      render: (s) => h("div", null, h("span", { class: "mono" }, s.scanned_barcode),
        s.lot || s.serial || s.expiry ? h("div", { class: "muted small mono" }, traceText(s)) : null,
        s.problem ? h("div", { class: "bad-text small" }, PROBLEM_LABELS[s.problem] || s.problem) : null),
    },
    {
      label: "Counted as",
      render: (s) => {
        const l = linesById.get(s.line_item_id);
        if (!l) return h("span", { class: "muted" }, "–");
        if (s.problem) return h("span", null, lineName(l), h("span", { class: "muted small" }, " (not counted)"));
        return h("span", null, lineName(l), s.match_tier > 1 ? h("span", { class: "muted small" }, ` (${TIER_NAMES[s.match_tier]})`) : null);
      },
    },
    {
      label: "",
      // A wrong lot / expired / repeated serial is the right product: nothing to teach.
      render: (s) => (s.result === "mismatch" || s.result === "review") && editable && !s.problem
        ? h("button", { class: "btn btn-sm", onclick: () => teachAlias(s, order, reload) }, "This is actually…")
        : null,
    },
  ], scans, { empty: "No scans yet." });
}

async function teachAlias(scan, order, reload) {
  const choice = await dialog("Teach Autorack a barcode", (close) => {
    const sel = h("select", { class: "input" }, ...order.lines.map((l) => h("option", { value: l.expected_barcode }, `${lineName(l)} (${l.expected_barcode})`)));
    return h("div", { class: "stack" },
      h("p", null, "A worker scanned ", h("strong", { class: "mono" }, scan.scanned_barcode),
        ". If that is really one of these items (a vendor label, a case code), choose it. From now on that barcode counts as this item at this warehouse."),
      sel,
      h("p", { class: "muted small" }, "This doesn't change past scans. The worker should scan the item again."),
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn btn-primary", onclick: () => close(sel.value) }, "Teach barcode")));
  });
  if (!choice) return;
  await api("/api/aliases", { method: "POST", body: { scanned_barcode: scan.scanned_barcode, target_barcode: choice, note: `From order ${order.external_order_number || order.id}` } })
    .then(() => {
      toast("Learned. Scans of that barcode now count as the chosen item.", "ok", 6000);
      reload();
    }, fail);
}

// ---------------------------------------------------------------------------
// New order
// ---------------------------------------------------------------------------

export async function newOrderView(params = new URLSearchParams()) {
  const kind = kindOf(params) === "return" ? "pick" : kindOf(params);
  const K = KINDS[kind];
  const minQty = kind === "count" ? "0" : "1";
  const [workers, clients] = await Promise.all([api("/api/workers"), api("/api/clients")]);
  const number = h("input", { class: "input mono", placeholder: K.numberPh });
  const rush = h("input", { type: "checkbox" });
  const shipBy = h("input", { class: "input", type: "date" });
  const client = h("select", { class: "input" }, h("option", { value: "" }, "None"),
    ...clients.filter((c) => c.active).map((c) => h("option", { value: c.id }, c.name)));
  const customer = h("input", { class: "input", placeholder: kind === "receive" ? "Optional" : "Optional, for per-customer reports" });
  const blind = h("input", { type: "checkbox" });
  const notes = h("input", { class: "input", placeholder: "Optional" });
  const assign = h("select", { class: "input" }, h("option", { value: "" }, "Anyone"),
    ...workers.workers.filter((w) => w.active).map((w) => h("option", { value: w.worker_id }, w.name)));
  const rowsHost = h("tbody");

  const addRow = () => {
    const tr = h("tr", null,
      h("td", null, h("input", { class: "input mono", name: "barcode", placeholder: "Barcode" })),
      h("td", null, h("input", { class: "input input-qty", name: "quantity", type: "number", min: minQty, value: "1" })),
      h("td", null, h("input", { class: "input", name: "description", placeholder: "Description" })),
      h("td", null, h("input", { class: "input", name: "sku", placeholder: "SKU" })),
      h("td", null, h("input", { class: "input input-qty", name: "location", placeholder: "Bin" })),
      h("td", null, h("select", { class: "input", name: "track", "aria-label": "Record" }, ...TRACK_OPTIONS.map(([v, t]) => h("option", { value: v }, t)))),
      h("td", null, h("button", { class: "icon-btn", type: "button", "aria-label": "Remove line", onclick: () => tr.remove() }, "✕")));
    rowsHost.appendChild(tr);
    tr.querySelector("input").focus();
  };

  const submit = async (e) => {
    e.preventDefault();
    const lines = [...rowsHost.querySelectorAll("tr")].map((tr) => {
      const v = (n) => tr.querySelector(`[name=${n}]`).value.trim();
      const qty = v("quantity") === "" ? 1 : Number(v("quantity"));
      return { ...trackFlags(tr.querySelector("[name=track]").value), barcode: v("barcode"), quantity: Number.isFinite(qty) ? qty : 1, description: v("description") || null, sku: v("sku") || null, location: v("location") || null };
    }).filter((l) => l.barcode);
    if (!lines.length) return toast("Add at least one line with a barcode.", "warn");
    try {
      const o = await api("/api/orders", {
        method: "POST",
        body: {
          external_order_number: number.value.trim() || null, customer: customer.value.trim() || null,
          notes: notes.value || null, assigned_worker_id: assign.value || null, lines,
          kind, blind: kind === "count" && blind.checked,
          ...(kind === "pick" ? { rush: rush.checked, ship_by: shipBy.value || null } : {}),
          client_id: client.value || null,
        },
      });
      toast(K.created, "ok");
      location.hash = `#/orders/${o.id}`;
    } catch (err) {
      fail(err);
    }
  };

  layout("#/orders", [
    h("a", { href: kind === "pick" ? "#/orders" : `#/orders?kind=${kind}`, class: "back-link" }, `← ${K.title}`),
    pageHeader(K.newLabel, {
      pick: "For one-off orders. For your daily pick list, CSV import is faster.",
      receive: "List what the purchase order says is coming. The worker scans what actually arrived.",
      count: "List the items to count (with the quantity the system expects, 0 is fine). The worker scans what's actually there.",
    }[kind]),
    h("form", { onsubmit: submit },
      card(null,
        h("div", { class: "form-grid" },
          h("label", null, K.numberLabel, number),
          kind === "count" ? null : h("label", null, kind === "receive" ? "Supplier" : "Customer", customer),
          h("label", null, "Assign to", assign), h("label", null, "Notes", notes),
          kind === "pick" ? h("label", null, "Ship by", shipBy) : null,
          clients.length ? h("label", null, "Client", client) : null,
          kind === "pick" ? h("label", { class: "check span-2" }, rush, " Rush: put it at the top of every worker's list") : null,
          kind === "count" ? h("label", { class: "check span-2" }, blind, " Blind count: don't show the expected quantity on the phone") : null)),
      card("Lines",
        h("div", { class: "table-wrap" },
          h("table", { class: "table table-form" },
            h("thead", null, h("tr", null, ...["Barcode", "Qty", "Description", "SKU", "Location", "Record", ""].map((t) => h("th", null, t)))),
            rowsHost)),
        h("p", { class: "muted small" }, "Tip: click into the barcode field and scan with a USB scanner."),
        h("div", { class: "row" },
          h("button", { class: "btn", type: "button", onclick: addRow }, "+ Add line"),
          h("button", { class: "btn btn-primary", type: "submit" }, K.newLabel.replace("New ", "Create "))))),
  ]);
  addRow();
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

export async function importView(params = new URLSearchParams()) {
  const kind = kindOf(params) === "return" ? "pick" : kindOf(params);
  const K = KINDS[kind];
  const blindBox = h("input", { type: "checkbox" });
  const previewHost = h("div");
  const historyHost = h("div");
  let file = null;

  const fileInput = h("input", { type: "file", accept: ".csv,.tsv,.txt,text/csv", class: "visually-hidden", id: "csv-file" });
  const drop = h("label", { class: "dropzone", for: "csv-file" },
    h("strong", null, "Choose a CSV file"), h("span", { class: "muted" }, " or drop it here"),
    h("div", { class: "muted small" }, "Columns: order_number, barcode, quantity, description (plus optional sku, location, customer; “track” with lot, serial or expiry to record them per unit; “lot” to allow only that lot). Common header names like “Order #”, “UPC”, “Qty”, “Bin” and “Ship To” are recognised."));

  const pick = (f) => {
    file = f;
    preview().catch(fail);
  };
  fileInput.addEventListener("change", () => fileInput.files[0] && pick(fileInput.files[0]));
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("drag");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]);
  });

  const form = (extra = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    if (kind === "count") fd.append("blind", String(blindBox.checked));
    for (const [k, v] of Object.entries(extra)) fd.append(k, v);
    return fd;
  };

  const preview = async () => {
    mount(previewHost, h("div", { class: "skeleton" }));
    let p;
    try {
      p = await api("/api/orders/import/preview", { method: "POST", form: form(), timeoutMs: 120000 });
    } catch (e) {
      mount(previewHost, h("div", { class: "banner banner-bad" }, e.message));
      return;
    }
    const skip = h("input", { type: "checkbox", id: "skip-invalid" });
    const canImport = p.orders_new > 0;
    mount(previewHost, card(`Preview: ${file.name}`,
      h("div", { class: "tiles tiles-sm" },
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "New orders"), h("div", { class: "tile-value" }, fmtNumber(p.orders_new))),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Lines"), h("div", { class: "tile-value" }, fmtNumber(p.lines))),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, "Units"), h("div", { class: "tile-value" }, fmtNumber(p.units))),
        h("div", { class: ["tile", p.error_count && "tile-bad"] }, h("div", { class: "tile-label" }, "Problem rows"), h("div", { class: "tile-value" }, fmtNumber(p.error_count)))),
      h("p", { class: "muted small" }, "Columns found: ", Object.entries(p.columns).map(([k, v]) => `${k} ← “${v}”`).join(" · ")),
      ...p.warnings.map((w) => h("div", { class: "banner banner-warn" }, w)),
      p.error_count ? h("div", { class: "stack" },
        h("h3", null, "Rows that can't be imported"),
        table([{ label: "Row", key: "row" }, { label: "Problem", key: "message" }], p.errors),
        h("label", { class: "row check" }, skip, "Import the valid rows and skip these")) : null,
      p.sample.length ? h("details", null, h("summary", null, "Sample of what will be created"),
        ...p.sample.map((o) => h("div", { class: "sample" },
          h("strong", { class: "mono" }, o.order_number), h("span", { class: "muted" }, `${o.customer ? ` · ${o.customer}` : ""} · ${o.line_count} lines`),
          h("ul", null, ...o.lines.map((l) => h("li", null, h("span", { class: "mono" }, l.barcode), ` × ${l.quantity}`, l.description ? ` — ${l.description}` : "")))))) : null,
      h("div", { class: "row" },
        h("button", {
          class: "btn btn-primary", disabled: !canImport,
          onclick: async (e) => {
            if (p.error_count && !skip.checked) return toast("Fix the problem rows, or tick “skip these”.", "warn");
            e.target.disabled = true;
            try {
              const r = await api("/api/orders/import", { method: "POST", form: form({ skip_invalid_rows: String(skip.checked) }), timeoutMs: 120000 });
              toast(`Imported ${r.orders_created} (${r.lines_created} lines).`, "ok", 6000);
              location.hash = kind === "pick" ? "#/orders?status=pending" : `#/orders?kind=${kind}&status=pending`;
            } catch (err) {
              e.target.disabled = false;
              fail(err);
            }
          },
        }, canImport ? `Import ${p.orders_new} ${{ pick: "order", receive: "PO", count: "count" }[kind]}${p.orders_new === 1 ? "" : "s"}` : "Nothing new to import"),
        h("button", { class: "btn", onclick: () => { file = null; fileInput.value = ""; mount(previewHost); } }, "Choose another file"))));
  };

  layout("#/orders", [
    h("a", { href: kind === "pick" ? "#/orders" : `#/orders?kind=${kind}`, class: "back-link" }, `← ${K.title}`),
    pageHeader(K.importLabel, {
      pick: "Export a pick list from your WMS or spreadsheet as CSV. Nothing is saved until you confirm.",
      receive: "One row per item on the purchase order: PO number in the order column, then barcode and quantity expected. Nothing is saved until you confirm.",
      count: "One row per item to count: a count name (e.g. the aisle) in the order column, barcode, the quantity the system expects (0 is fine) and location. Nothing is saved until you confirm.",
    }[kind],
      h("button", { class: "btn", onclick: () => download("/api/orders/template.csv", "autorack-orders-template.csv") }, "Download template")),
    card(null, fileInput, drop,
      kind === "count" ? h("label", { class: "check" }, blindBox, " Blind count: don't show the expected quantity on the phone") : null,
      kind !== "pick" ? null : h("p", { class: "muted small" }, "Uploading every day? ",
        h("a", { href: "#/connections" }, "Connect Shopify, ShipStation, WooCommerce or a Google Sheet"),
        ", or email the CSV in, and orders arrive by themselves.")),
    previewHost,
    card("Recent imports", historyHost),
  ]);

  const imports = await api("/api/imports");
  mount(historyHost, table([
    { label: "When", render: (b) => fmtDateTime(b.created_at, tz()) },
    { label: "File", render: (b) => b.filename || "–" },
    { label: "Orders", align: "right", key: "orders_created" },
    { label: "Lines", align: "right", key: "lines_created" },
    { label: "Skipped (already existed)", align: "right", key: "orders_skipped" },
  ], imports, { empty: "No imports yet." }));
}


const SOURCE_LABELS = { shopify: "Shopify", shipstation: "ShipStation", woocommerce: "WooCommerce" };

// Did the tracking number reach the store the order came from?
function pushLine(order, reload) {
  const p = order.tracking_push;
  if (!p) return null;
  const store = SOURCE_LABELS[order.source] || "the store";
  if (p.status === "done") return h("p", { class: "push-state muted small" }, `✓ Tracking sent to ${store}.`);
  if (p.status === "pending") return h("p", { class: "push-state muted small" }, `Sending tracking to ${store}…`);
  if (p.status === "skipped") return h("p", { class: "push-state muted small" }, `Tracking not sent to ${store}: ${p.error || "turned off"}.`);
  return h("p", { class: "push-state warn-text small" },
    `Couldn't send tracking to ${store}${p.error ? `: ${p.error.replace(/\.$/, "")}` : ""}. `,
    canManage() ? h("button", {
      class: "link-btn small",
      onclick: () => api(`/api/orders/${order.id}/push-tracking`, { method: "POST" })
        .then((r) => { toast(r.ok ? "Sent." : r.error, r.ok ? "success" : "error"); reload(); }, fail),
    }, "Try again") : null);
}
