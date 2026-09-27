// Printable pick sheets (one per page), the phone-setup poster, shipment
// proof certificates, and date-range reports ("Save as PDF" from the print
// dialog: no PDF library, and it looks the same on every machine).

import { imageUrl, request } from "../../shared/api.js";
import { brandLockup, fmtDateTime, fmtMoney, fmtNumber, fmtPercent, h, mount, svg } from "../../shared/dom.js";
import { getToken } from "./core.js";
import { customerTable, skuTable, workerTable } from "./views/reports.js";
import { reportErrors } from "../../shared/report-errors.js";
import { svg as code128 } from "../../shared/code128.js";

reportErrors("print");

const host = document.getElementById("sheets");
const status = document.getElementById("print-status");
const params = new URLSearchParams(location.search);
document.getElementById("print-btn").addEventListener("click", () => window.print());

function sheet(o, warehouse) {
  return h("section", { class: "sheet" },
    h("header", { class: "sheet-head" },
      h("div", null,
        h("div", { class: "sheet-brand" }, brandLockup(), h("span", { class: "sheet-wh" }, warehouse)),
        h("div", { class: "sheet-order" }, o.external_order_number || o.id.slice(0, 8)),
        h("div", { class: "sheet-meta" }, `${o.line_count} lines · ${o.units_expected} units`),
        o.notes ? h("div", { class: "sheet-notes" }, o.notes) : null),
      h("div", { class: "sheet-qr" }, svg(o.qr_svg), h("div", null, "Scan to open"))),
    h("table", { class: "sheet-table" },
      h("thead", null, h("tr", null, ...["", "Location", "Item", "SKU", "Barcode", "Qty"].map((t) => h("th", null, t)))),
      h("tbody", null, ...o.lines.map((l) => h("tr", null,
        h("td", { class: "box" }, "☐"),
        h("td", { class: "strong" }, l.location || ""),
        h("td", null, l.description || ""),
        h("td", { class: "mono" }, l.sku || ""),
        h("td", { class: "mono" }, l.expected_barcode),
        h("td", { class: "qty" }, String(l.expected_quantity)))))),
    h("footer", { class: "sheet-foot" }, "Every item is verified by scan in the Autorack app. Wrong items are caught before they ship."));
}

const REASONS = {
  wrong_item_in_location: "Wrong item in the bin", out_of_stock: "Out of stock", damaged: "Damaged",
  label_unreadable: "Label won't scan", short_pick: "Short pick", not_found: "Not in the bin", other: "Other",
};

function docHead(title, subtitle, warehouse) {
  return h("header", { class: "doc-head" },
    h("div", { class: "sheet-brand" }, brandLockup({ tagline: true }), h("span", { class: "sheet-wh" }, warehouse)),
    h("h1", { class: "doc-title" }, title),
    subtitle ? h("div", { class: "doc-sub" }, subtitle) : null);
}

function kv(rows) {
  return h("dl", { class: "doc-kv" }, ...rows.filter(Boolean).map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, v))));
}

/** One order's proof of shipment: what was picked, by whom, when, and the parcel it left in. */
async function proof(id, token, me) {
  const p = await request(`/api/orders/${id}/proof`, { token });
  const zone = me.warehouse.timezone;
  const lines = new Map(p.lines.map((l) => [l.id, l]));
  const photos = p.flags.flatMap((f) => f.photos.map((pid) => ({ pid, f })));
  const photoEls = await Promise.all(photos.map(async ({ pid, f }) => {
    try {
      const url = await imageUrl(`/api/photos/${pid}`, token);
      return h("figure", { class: "doc-photo" }, h("img", { src: url, alt: "" }), h("figcaption", null, `${REASONS[f.reason] || f.reason} · ${f.worker || ""}`));
    } catch {
      return null;
    }
  }));
  const packEls = await Promise.all((p.pack_photos || []).map(async (pid) => {
    try {
      const url = await imageUrl(`/api/photos/${pid}`, token);
      return h("figure", { class: "doc-photo doc-photo-pack" }, h("img", { src: url, alt: "The packed box" }));
    } catch {
      return null;
    }
  }));
  const shipped = p.status === "shipped";
  document.title = `Shipment proof ${p.external_order_number || ""} · Autorack`;
  mount(host, h("section", { class: "sheet doc" },
    docHead(shipped ? "Proof of shipment" : "Pick verification record", `Order ${p.external_order_number || p.id}`, p.warehouse),
    kv([
      p.customer ? ["Customer", p.customer] : null,
      ["Status", shipped ? "Shipped" : p.status.replace("_", " ")],
      shipped ? ["Tracking", h("span", { class: "mono strong" }, `${p.carrier ? `${p.carrier} ` : ""}${p.tracking_number}`)] : null,
      shipped ? ["Label scanned", `${fmtDateTime(p.shipped_at, zone)}${p.shipped_by ? ` by ${p.shipped_by}` : ""}`] : null,
      ["Picking completed", fmtDateTime(p.completed_at, zone)],
      ["Units verified by scan", `${p.units_scanned} of ${p.units_expected}${p.units_short ? ` (${p.units_short} short, approved)` : ""}`],
      ["Wrong items caught and put back", String(p.errors_caught)],
    ]),
    h("h2", { class: "doc-h2" }, "Items"),
    h("table", { class: "sheet-table" },
      h("thead", null, h("tr", null, ...["Item", "SKU", "Barcode", "Ordered", "Verified", "Short"].map((t) => h("th", null, t)))),
      h("tbody", null, ...p.lines.map((l) => h("tr", null,
        h("td", null, l.description || ""), h("td", { class: "mono" }, l.sku || ""), h("td", { class: "mono" }, l.expected_barcode),
        h("td", { class: "qty" }, String(l.expected_quantity)), h("td", { class: "qty" }, String(l.scanned_quantity)),
        h("td", { class: "qty" }, l.short_quantity ? String(l.short_quantity) : ""))))),
    h("h2", { class: "doc-h2" }, "Every unit, as scanned"),
    h("table", { class: "sheet-table doc-small" },
      h("thead", null, h("tr", null, ...["Time", "Picked by", "Barcode scanned", "Counted as", ...(p.picks.some(traced) ? ["Lot / serial / expiry"] : [])].map((t) => h("th", null, t)))),
      h("tbody", null, ...p.picks.map((s) => {
        const l = lines.get(s.line_item_id);
        return h("tr", null, h("td", null, fmtDateTime(s.at, zone)), h("td", null, s.worker || ""),
          s.confirmed
            ? h("td", { class: "muted" }, "No barcode: confirmed by hand")
            : h("td", { class: "mono" }, s.scanned_barcode),
          h("td", null, l ? l.description || l.sku || l.expected_barcode : "",
            s.quantity > 1 ? ` × ${s.quantity}` : "", s.substitution ? " (approved substitute)" : ""),
          p.picks.some(traced) ? h("td", { class: "mono" }, traceText(s)) : null);
      }))),
    p.packages && p.packages.length > 1 ? [
      h("h2", { class: "doc-h2" }, `Shipped in ${p.packages.length} boxes`),
      h("table", { class: "sheet-table doc-small" },
        h("thead", null, h("tr", null, ...["Box", "Tracking", "Label scanned"].map((t) => h("th", null, t)))),
        h("tbody", null, ...p.packages.map((b) => h("tr", null,
          h("td", null, String(b.box)), h("td", { class: "mono" }, `${b.carrier ? `${b.carrier} ` : ""}${b.tracking_number}`),
          h("td", null, `${fmtDateTime(b.at, zone)}${b.worker ? ` by ${b.worker}` : ""}`))))),
    ] : null,
    p.inserts && p.inserts.length ? [
      h("h2", { class: "doc-h2" }, "Inserts"),
      h("ul", { class: "doc-list" }, ...p.inserts.map((i) => h("li", null, `${i.done ? "✓" : "✗"} ${i.name}`))),
    ] : null,
    packEls.some(Boolean) ? [
      h("h2", { class: "doc-h2" }, p.packages && p.packages.length > 1 ? "The packed boxes" : "The packed box"),
      h("div", { class: "doc-photos" }, ...packEls.filter(Boolean)),
    ] : null,
    p.flags.length ? [
      h("h2", { class: "doc-h2" }, "Problems reported"),
      h("ul", { class: "doc-list" }, ...p.flags.map((f) => h("li", null,
        `${REASONS[f.reason] || f.reason}${f.short_quantity ? ` (${f.short_quantity} short)` : ""} — ${f.worker || "worker"}, ${fmtDateTime(f.created_at, zone)}`,
        f.note ? ` “${f.note}”` : "",
        f.resolved_at ? ` · resolved ${fmtDateTime(f.resolved_at, zone)}${f.resolution_note ? `: ${f.resolution_note}` : ""}` : " · open"))),
      h("div", { class: "doc-photos" }, ...photoEls.filter(Boolean)),
    ] : null,
    h("footer", { class: "sheet-foot" },
      `Generated ${fmtDateTime(p.generated_at, zone)} from Autorack's scan log, which can't be edited after the fact. Order id ${p.id}.`)));
  status.textContent = "Ready. Use Print → Save as PDF.";
}

async function report(from, to, token) {
  const r = await request(`/api/reports?from=${from}&to=${to}`, { token });
  const t = r.totals;
  document.title = `Autorack report ${r.from} to ${r.to}`;
  const stat = (label, value) => h("div", { class: "doc-stat" }, h("div", { class: "doc-stat-v" }, value), h("div", { class: "doc-stat-l" }, label));
  mount(host, h("section", { class: "sheet doc doc-report" },
    docHead("Pick verification report", `${r.from === r.to ? r.from : `${r.from} to ${r.to}`} · ${r.timezone}`, r.warehouse),
    h("div", { class: "doc-stats" },
      stat("Mistakes caught", fmtNumber(t.errors_caught)),
      stat("Money saved (est.)", fmtMoney(t.money_saved_cents)),
      stat("Pick accuracy", fmtPercent(t.accuracy)),
      stat("Orders completed", fmtNumber(t.orders_completed)),
      stat("Orders shipped", fmtNumber(t.orders_shipped)),
      stat("Units verified", fmtNumber(t.units_picked))),
    h("h2", { class: "doc-h2" }, "By customer"), customerTable(r),
    h("h2", { class: "doc-h2" }, "By item"), skuTable(r, 40),
    h("h2", { class: "doc-h2" }, "By worker"), workerTable(r),
    h("footer", { class: "sheet-foot" },
      `Money saved = mistakes caught × ${fmtMoney(r.cost_per_error_cents)} per mis-ship. Generated ${new Date(r.generated_at).toLocaleString()}.`)));
  status.textContent = "Ready. Use Print → Save as PDF.";
  setTimeout(() => window.print(), 400);
}

async function main() {
  const token = getToken();
  if (!token) {
    location.replace("/app/login.html");
    return;
  }
  const me = await request("/api/auth/me", { token });
  if (params.get("proof")) return proof(params.get("proof"), token, me);
  if (params.get("labels")) return labels(params.get("labels"), token);
  if (params.get("report")) return report(params.get("from"), params.get("to"), token);
  if (params.get("setup")) {
    const link = await request("/api/warehouse/device-link", { token });
    mount(host, h("section", { class: "sheet poster" },
      brandLockup({ tagline: true }),
      h("div", { class: "poster-title" }, "Set up your phone for scanning"),
      h("div", { class: "poster-wh" }, me.warehouse.name),
      h("div", { class: "poster-qr" }, svg(link.qr_svg)),
      h("ol", { class: "poster-steps" },
        h("li", null, "Open your phone's camera and point it at the code."),
        h("li", null, "Tap the link that appears."),
        h("li", null, "Add Autorack to your home screen."),
        h("li", null, "Sign in with your 4-digit PIN.")),
      h("div", { class: "poster-code" }, "Setup code: ", h("span", { class: "mono" }, link.join_code)),
      h("div", { class: "poster-es" }, "Escanea el código con la cámara de tu teléfono para configurar Autorack.")));
    status.textContent = "Setup poster ready";
    return;
  }
  const ids = (params.get("ids") || "").split(",").filter(Boolean);
  if (!ids.length) {
    status.textContent = "No orders selected.";
    return;
  }
  const orders = await request(`/api/orders/pick-sheets?ids=${ids.join(",")}`, { token });
  mount(host, ...orders.map((o) => sheet(o, me.warehouse.name)));
  status.textContent = `${orders.length} pick sheet${orders.length === 1 ? "" : "s"} ready`;
  setTimeout(() => window.print(), 300);
}

main().catch((e) => {
  status.textContent = e.message;
});

function traced(s) {
  return Boolean(s.lot || s.serial || s.expiry);
}

function traceText(s) {
  return [s.lot && `Lot ${s.lot}`, s.serial && `S/N ${s.serial}`, s.expiry && `Exp ${s.expiry}`].filter(Boolean).join(" · ");
}

/**
 * Product labels: Code 128 barcode, name and SKU. `size=thermal` prints one
 * 2.25" x 1.25" label per page (Zebra/Rollo/DYMO); `size=sheet` fills Avery
 * 5160-style sheets, 30 per Letter page. `qty` copies of each.
 */
async function labels(ids, token) {
  const size = params.get("size") === "sheet" ? "sheet" : "thermal";
  const copies = Math.min(Math.max(Number(params.get("qty")) || 1, 1), 500);
  const r = await request(`/api/products?ids=${encodeURIComponent(ids)}&limit=500`, { token });
  const skipped = [];
  const one = (p) => {
    const code = p.barcode || p.sku;
    let bars = null;
    try {
      bars = svg(code128(code, { height: 60 }), "label-bars");
    } catch {
      skipped.push(p.name);
      return null;
    }
    return h("div", { class: "label" },
      h("div", { class: "label-name" }, p.name),
      bars,
      h("div", { class: "label-code mono" }, code),
      p.sku && p.sku !== code ? h("div", { class: "label-sku mono" }, p.sku) : null);
  };
  const items = r.products.filter((p) => p.barcode || p.sku).flatMap((p) => Array.from({ length: copies }, () => one(p))).filter(Boolean);
  document.body.classList.add(`labels-${size}`);
  if (size === "sheet") {
    const pages = [];
    for (let i = 0; i < items.length; i += 30) pages.push(h("section", { class: "label-sheet" }, ...items.slice(i, i + 30)));
    mount(host, ...pages);
  } else {
    mount(host, h("div", { class: "label-roll" }, ...items));
  }
  const noCode = r.products.filter((p) => !p.barcode && !p.sku).map((p) => p.name);
  status.textContent = `${items.length} label${items.length === 1 ? "" : "s"} ready` +
    (noCode.length || skipped.length ? ` · skipped (no printable code): ${[...noCode, ...skipped].join(", ")}` : "");
  setTimeout(() => window.print(), 300);
}
