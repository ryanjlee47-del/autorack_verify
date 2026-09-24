// Printable pick sheets (one per page) and the phone-setup poster.

import { request } from "../../shared/api.js";
import { h, mount, svg } from "../../shared/dom.js";
import { getToken } from "./core.js";

const host = document.getElementById("sheets");
const status = document.getElementById("print-status");
const params = new URLSearchParams(location.search);
document.getElementById("print-btn").addEventListener("click", () => window.print());

function sheet(o, warehouse) {
  return h("section", { class: "sheet" },
    h("header", { class: "sheet-head" },
      h("div", null,
        h("div", { class: "sheet-wh" }, warehouse),
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

async function main() {
  const token = getToken();
  if (!token) {
    location.replace("/app/login.html");
    return;
  }
  const me = await request("/api/auth/me", { token });
  if (params.get("setup")) {
    const link = await request("/api/warehouse/device-link", { token });
    mount(host, h("section", { class: "sheet poster" },
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
