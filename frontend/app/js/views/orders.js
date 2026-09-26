// Orders: list, detail, manual entry, CSV import.

import {
  confirmDialog, dialog, fmtAgo, fmtDateTime, fmtNumber, h, mount, svg, toast,
} from "../../../shared/dom.js";
import { api, canManage, card, download, fail, layout, pageHeader, statusBadge, table, tz } from "../core.js";
import { problemLabel } from "./dashboard.js";
import { flagItem } from "./flags.js";

const TABS = [
  ["open", "Open"],
  ["in_progress", "In progress"],
  ["flagged", "Flagged"],
  ["pending", "Not started"],
  ["completed", "Ready to ship"],
  ["shipped", "Shipped"],
  ["cancelled", "Cancelled"],
  ["", "All"],
];

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
  const status = params.get("status") ?? "open";
  const q = params.get("q") || "";
  const selected = new Set();
  const listHost = h("div", null, h("div", { class: "skeleton" }));
  const moreHost = h("div", { class: "row center-row" });
  let offset = 0;
  let rows = [];

  const search = h("input", { class: "input", type: "search", placeholder: "Search order #, customer, tracking, barcode, SKU", value: q });
  const go = (next) => {
    const p = new URLSearchParams({ status: next.status ?? status, q: next.q ?? search.value });
    location.hash = `#/orders?${p}`;
  };
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") go({});
  });

  layout("#/orders", [
    canManage()
      ? pageHeader("Orders", "Import your pick list, print sheets, and follow every order to verified.",
        h("a", { class: "btn", href: "/api/orders/template.csv", onclick: (e) => { e.preventDefault(); download("/api/orders/template.csv", "autorack-orders-template.csv"); } }, "CSV template"),
        h("a", { class: "btn", href: "#/orders/new" }, "New order"),
        h("a", { class: "btn btn-primary", href: "#/orders/import" }, "Import CSV"))
      : pageHeader("Orders", "Follow every order to verified and shipped."),
    h("div", { class: "toolbar" },
      h("div", { class: "tabs" }, ...TABS.map(([value, label]) =>
        h("button", { class: ["tab", status === value && "active"], onclick: () => go({ status: value }) }, label))),
      h("div", { class: "row" }, search,
        h("button", { class: "btn", onclick: () => printSheets([...selected]) }, "Print selected"))),
    card(null, listHost, moreHost),
  ]);

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
      { label: "Order", render: (o) => h("span", { class: "mono strong" }, o.external_order_number || o.id.slice(0, 8)) },
      { label: "Customer", render: (o) => o.customer || h("span", { class: "muted" }, "–") },
      { label: "Status", render: (o) => statusBadge(o.status) },
      { label: "Lines", align: "right", render: (o) => String(o.line_count) },
      { label: "Units", align: "right", render: (o) => `${o.units_scanned}/${o.units_expected}` },
      { label: "Caught", align: "right", render: (o) => (o.errors_caught ? h("span", { class: "bad-text" }, String(o.errors_caught)) : "0") },
      { label: "Assigned", render: (o) => o.assigned_worker || h("span", { class: "muted" }, "Anyone") },
      { label: "Created", render: (o) => h("span", { class: "muted" }, fmtDateTime(o.created_at, tz())) },
    ];
    mount(listHost, table(cols, rows, {
      empty: status === "open" ? "No open orders. Import a CSV or create one by hand." : "No orders match.",
      onRow: (o) => { location.hash = `#/orders/${o.id}`; },
    }));
  };

  const load = async () => {
    const p = new URLSearchParams({ limit: "100", offset: String(offset) });
    if (status) p.set("status", status);
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
// Detail
// ---------------------------------------------------------------------------

const TIER_NAMES = ["exact", "normalized", "same GTIN", "digits", "no check digit", "taught alias", "suffix"];

export async function orderDetailView(id) {
  const [order, scans, workers] = await Promise.all([
    api(`/api/orders/${id}`),
    api(`/api/orders/${id}/scans`),
    api("/api/workers"),
  ]);
  const editable = canManage() && order.status !== "cancelled" && order.status !== "shipped";
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
  }, h("option", { value: "" }, "Anyone can pick"),
  ...workers.workers.filter((w) => w.active).map((w) =>
    h("option", { value: w.worker_id, selected: w.worker_id === order.assigned_worker_id }, w.name)));

  const openFlags = order.flags.filter((f) => !f.resolved_at);
  const closedFlags = order.flags.filter((f) => f.resolved_at);
  const flagLine = (f) => (f.line_item_id && linesById.get(f.line_item_id) ? lineName(linesById.get(f.line_item_id)) : "Whole order");
  const shipped = order.status === "shipped";

  layout("#/orders", [
    h("a", { href: "#/orders", class: "back-link" }, "← Orders"),
    h("div", { class: "page-head" },
      h("div", null,
        h("h1", { class: "row" }, h("span", { class: "mono" }, order.external_order_number || "Order"), statusBadge(order.status)),
        h("p", { class: "muted" },
          order.customer ? [h("strong", null, order.customer), " · "] : null,
          `${order.line_count} lines · ${order.units_scanned}/${order.units_expected} units verified`,
          order.units_short ? ` · ${order.units_short} short` : "",
          ` · created ${fmtDateTime(order.created_at, tz())}`,
          order.completed_at ? ` · completed ${fmtDateTime(order.completed_at, tz())}` : ""),
        shipped ? h("p", { class: "ship-line" },
          "Shipped ", fmtDateTime(order.shipped_at, tz()), order.shipped_by ? ` by ${order.shipped_by}` : "",
          " · ", order.carrier ? `${order.carrier} ` : "", h("span", { class: "mono strong" }, order.tracking_number)) : null),
      h("div", { class: "row" },
        shipped || order.status === "completed"
          ? h("button", { class: shipped ? "btn btn-primary" : "btn", onclick: () => openProof(id) }, "Shipment proof")
          : null,
        order.status !== "shipped" ? h("button", { class: "btn", onclick: () => printSheets([id]) }, "Print pick sheet") : null,
        editable ? h("button", {
          class: "btn btn-danger",
          onclick: async () => {
            if (!(await confirmDialog("Cancel this order?", "Workers will be told to put its items back. Its scan history is kept.", { confirmLabel: "Cancel order", danger: true }))) return;
            await api(`/api/orders/${id}/cancel`, { method: "POST" }).then(reload, fail);
          },
        }, "Cancel order") : null)),

    openFlags.length ? card(`Needs a decision (${openFlags.length})`,
      h("ul", { class: "feed" }, ...openFlags.map((f) => flagItem(id, f, { item: flagLine(f), onDone: reload })))) : null,

    h("div", { class: "grid-main" },
      card("Lines",
        linesTable(order, editable, reload),
        editable ? addLineForm(id, reload) : null),
      h("div", { class: "stack-lg" },
        card("Pick sheet QR",
          h("div", { class: "qr-box" }, svg(order.qr_svg, "qr")),
          h("p", { class: "muted small" }, "Workers scan this (on the printed sheet) to open the order on their phone.")),
        card("Assignment", assign, h("p", { class: "muted small" }, "Assigned orders show first on that worker's phone and are hidden from others.")),
        card("Notes", notesEditor(order, editable)))),

    closedFlags.length ? card("Resolved problems",
      h("ul", { class: "feed" }, ...closedFlags.map((f) => flagItem(id, f, { item: flagLine(f), onDone: reload })))) : null,

    card("Scan history", scanHistory(order, scans, linesById, reload, editable)),
  ]);
}

function lineName(l) {
  return l.description || l.sku || l.expected_barcode;
}

function linesTable(order, editable, reload) {
  const cols = [
    { label: "Location", render: (l) => l.location || h("span", { class: "muted" }, "–") },
    { label: "Item", render: (l) => h("div", null, h("div", null, l.description || l.sku || "–"), l.sku && l.description ? h("div", { class: "muted small mono" }, l.sku) : null) },
    { label: "Barcode", render: (l) => h("span", { class: "mono" }, l.expected_barcode) },
    {
      label: "Verified", align: "right",
      render: (l) => h("span", null,
        h("span", { class: l.scanned_quantity >= l.expected_quantity ? "ok-text" : null }, `${l.scanned_quantity}/${l.expected_quantity}`),
        l.short_quantity ? h("span", { class: "badge badge-warn badge-inline" }, `${l.short_quantity} short`) : null),
    },
    { label: "Wrong picks", align: "right", render: (l) => (l.mismatches ? h("span", { class: "bad-text" }, String(l.mismatches)) : "0") },
  ];
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
      quantity: h("input", { class: "input", type: "number", min: "1", value: String(line.expected_quantity) }),
      sku: h("input", { class: "input", value: line.sku || "" }),
      description: h("input", { class: "input", value: line.description || "" }),
      location: h("input", { class: "input", value: line.location || "" }),
    };
    return h("form", {
      class: "form-grid",
      onsubmit: (e) => {
        e.preventDefault();
        close({
          barcode: f.barcode.value.trim(), quantity: Number(f.quantity.value), sku: f.sku.value, description: f.description.value, location: f.location.value,
        });
      },
    },
    h("label", null, "Barcode", f.barcode), h("label", null, "Quantity", f.quantity),
    h("label", null, "SKU", f.sku), h("label", null, "Location", f.location),
    h("label", { class: "span-2" }, "Description", f.description),
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

function addLineForm(orderId, reload) {
  const barcode = h("input", { class: "input mono", placeholder: "Barcode", required: true });
  const qty = h("input", { class: "input input-qty", type: "number", min: "1", value: "1", "aria-label": "Quantity" });
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
  const resultLabel = { match: "Right item", void: "Undone", ...{ mismatch: problemLabel("mismatch"), over_pick: problemLabel("over_pick"), review: problemLabel("review") } };
  return table([
    { label: "When", render: (s) => h("span", { class: "muted" }, fmtDateTime(s.at, tz())) },
    { label: "Worker", key: "worker" },
    {
      label: "Result",
      render: (s) => h("span", { class: "row nowrap" },
        h("span", { class: `badge badge-${s.voided ? "void" : s.result}` }, resultLabel[s.result] || s.result),
        s.was_offline ? h("span", { class: "muted small", title: "Scanned offline, synced later" }, "offline") : null),
    },
    { label: "Scanned", render: (s) => h("span", { class: "mono" }, s.scanned_barcode) },
    {
      label: "Counted as",
      render: (s) => {
        const l = linesById.get(s.line_item_id);
        if (!l) return h("span", { class: "muted" }, "–");
        return h("span", null, lineName(l), s.match_tier > 1 ? h("span", { class: "muted small" }, ` (${TIER_NAMES[s.match_tier]})`) : null);
      },
    },
    {
      label: "",
      render: (s) => (s.result === "mismatch" || s.result === "review") && editable
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

export async function newOrderView() {
  const workers = await api("/api/workers");
  const number = h("input", { class: "input mono", placeholder: "e.g. SO-1042" });
  const customer = h("input", { class: "input", placeholder: "Optional, for per-customer reports" });
  const notes = h("input", { class: "input", placeholder: "Optional" });
  const assign = h("select", { class: "input" }, h("option", { value: "" }, "Anyone"),
    ...workers.workers.filter((w) => w.active).map((w) => h("option", { value: w.worker_id }, w.name)));
  const rowsHost = h("tbody");

  const addRow = () => {
    const tr = h("tr", null,
      h("td", null, h("input", { class: "input mono", name: "barcode", placeholder: "Barcode" })),
      h("td", null, h("input", { class: "input input-qty", name: "quantity", type: "number", min: "1", value: "1" })),
      h("td", null, h("input", { class: "input", name: "description", placeholder: "Description" })),
      h("td", null, h("input", { class: "input", name: "sku", placeholder: "SKU" })),
      h("td", null, h("input", { class: "input input-qty", name: "location", placeholder: "Bin" })),
      h("td", null, h("button", { class: "icon-btn", type: "button", "aria-label": "Remove line", onclick: () => tr.remove() }, "✕")));
    rowsHost.appendChild(tr);
    tr.querySelector("input").focus();
  };

  const submit = async (e) => {
    e.preventDefault();
    const lines = [...rowsHost.querySelectorAll("tr")].map((tr) => {
      const v = (n) => tr.querySelector(`[name=${n}]`).value.trim();
      return { barcode: v("barcode"), quantity: Number(v("quantity")) || 1, description: v("description") || null, sku: v("sku") || null, location: v("location") || null };
    }).filter((l) => l.barcode);
    if (!lines.length) return toast("Add at least one line with a barcode.", "warn");
    try {
      const o = await api("/api/orders", {
        method: "POST",
        body: {
          external_order_number: number.value.trim() || null, customer: customer.value.trim() || null,
          notes: notes.value || null, assigned_worker_id: assign.value || null, lines,
        },
      });
      toast("Order created", "ok");
      location.hash = `#/orders/${o.id}`;
    } catch (err) {
      fail(err);
    }
  };

  layout("#/orders", [
    h("a", { href: "#/orders", class: "back-link" }, "← Orders"),
    pageHeader("New order", "For one-off orders. For your daily pick list, CSV import is faster."),
    h("form", { onsubmit: submit },
      card(null,
        h("div", { class: "form-grid" },
          h("label", null, "Order number", number), h("label", null, "Customer", customer),
          h("label", null, "Assign to", assign), h("label", null, "Notes", notes))),
      card("Lines",
        h("div", { class: "table-wrap" },
          h("table", { class: "table table-form" },
            h("thead", null, h("tr", null, ...["Barcode", "Qty", "Description", "SKU", "Location", ""].map((t) => h("th", null, t)))),
            rowsHost)),
        h("p", { class: "muted small" }, "Tip: click into the barcode field and scan with a USB scanner."),
        h("div", { class: "row" },
          h("button", { class: "btn", type: "button", onclick: addRow }, "+ Add line"),
          h("button", { class: "btn btn-primary", type: "submit" }, "Create order")))),
  ]);
  addRow();
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

export async function importView() {
  const previewHost = h("div");
  const historyHost = h("div");
  let file = null;

  const fileInput = h("input", { type: "file", accept: ".csv,.tsv,.txt,text/csv", class: "visually-hidden", id: "csv-file" });
  const drop = h("label", { class: "dropzone", for: "csv-file" },
    h("strong", null, "Choose a CSV file"), h("span", { class: "muted" }, " or drop it here"),
    h("div", { class: "muted small" }, "Columns: order_number, barcode, quantity, description (plus optional sku, location, customer). Common header names like “Order #”, “UPC”, “Qty”, “Bin” and “Ship To” are recognised."));

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
              toast(`Imported ${r.orders_created} orders (${r.lines_created} lines).`, "ok", 6000);
              location.hash = "#/orders?status=pending";
            } catch (err) {
              e.target.disabled = false;
              fail(err);
            }
          },
        }, canImport ? `Import ${p.orders_new} order${p.orders_new === 1 ? "" : "s"}` : "Nothing new to import"),
        h("button", { class: "btn", onclick: () => { file = null; fileInput.value = ""; mount(previewHost); } }, "Choose another file"))));
  };

  layout("#/orders", [
    h("a", { href: "#/orders", class: "back-link" }, "← Orders"),
    pageHeader("Import orders", "Export a pick list from your WMS or spreadsheet as CSV. Nothing is saved until you confirm.",
      h("button", { class: "btn", onclick: () => download("/api/orders/template.csv", "autorack-orders-template.csv") }, "Download template")),
    card(null, fileInput, drop),
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
