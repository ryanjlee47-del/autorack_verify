// Public proof of shipment: /proof.html#t=<token>. The token is in the hash
// so it never reaches server logs or a Referer header.

import { imageUrl, request } from "../../shared/api.js";
import { fmtDateTime, h, mount } from "../../shared/dom.js";

const TRACK_URLS = {
  UPS: (t) => `https://www.ups.com/track?tracknum=${t}`,
  USPS: (t) => `https://tools.usps.com/go/TrackConfirmAction?tLabels=${t}`,
  FedEx: (t) => `https://www.fedex.com/fedextrack/?trknbr=${t}`,
  DHL: (t) => `https://www.dhl.com/en/express/tracking.html?AWB=${t}`,
};

function token() {
  const m = /[#&]t=([A-Za-z0-9_-]{20,64})/.exec(location.hash);
  return m ? m[1] : null;
}

function trace(u) {
  return [u.lot && `Lot ${u.lot}`, u.serial && `S/N ${u.serial}`, u.expiry && `Exp ${u.expiry}`].filter(Boolean).join(" · ");
}

function packPhotos(p, t) {
  if (!p.pack_photos || !p.pack_photos.length) return null;
  const figs = p.pack_photos.map((pid) => {
    const img = h("img", { class: "proof-photo", alt: "The packed box before it was sealed" });
    const link = (p.photo_links || {})[pid]; // signed, short-lived, names only this photo
    if (link) imageUrl(link).then((url) => { img.src = url; }, () => img.remove());
    else img.remove();
    return h("figure", null, img);
  });
  return h("section", { class: "card" },
    h("h2", { class: "card-title" }, "The packed box"),
    h("p", { class: "muted" }, "Photographed at the packing bench before the label went on."),
    h("div", { class: "proof-photos" }, ...figs));
}

function trackLink(carrier, number) {
  const url = TRACK_URLS[carrier] ? TRACK_URLS[carrier](encodeURIComponent(number)) : null;
  return url ? h("a", { href: url, rel: "noopener noreferrer", target: "_blank" }, number) : number;
}

function render(p, t) {
  const tz = p.timezone;
  const shipped = p.status === "shipped";
  const trackUrl = p.tracking_number && TRACK_URLS[p.carrier] ? TRACK_URLS[p.carrier](encodeURIComponent(p.tracking_number)) : null;
  const allVerified = p.lines.every((l) => l.verified + (l.short || 0) >= l.ordered);
  const traced = p.units.some((u) => u.lot || u.serial || u.expiry);
  const boxes = p.boxes || [];
  document.title = `Order ${p.order_number || ""} · proof of shipment`;
  return [
    h("div", { class: "proof-head" },
      h("span", { class: "proof-seal" }, "✓ Verified shipment"),
      h("h1", null, `Order ${p.order_number || ""}`),
      h("p", { class: "muted" }, "Shipped by ", h("strong", null, p.warehouse), p.customer ? [" for ", h("strong", null, p.customer)] : null)),
    h("div", { class: "proof-facts" },
      h("div", null, h("div", { class: "proof-label" }, shipped ? "Shipped" : "Packed"),
        h("div", { class: "proof-value" }, fmtDateTime(shipped ? p.shipped_at : p.completed_at, tz))),
      boxes.length > 1
        ? h("div", null, h("div", { class: "proof-label" }, `${boxes.length} boxes`),
          ...boxes.map((b) => h("div", { class: "proof-value mono" }, `${b.box}. `, trackLink(b.carrier, b.tracking_number))))
        : p.tracking_number ? h("div", null, h("div", { class: "proof-label" }, p.carrier ? `${p.carrier} tracking` : "Tracking"),
          h("div", { class: "proof-value mono" }, trackUrl ? h("a", { href: trackUrl, rel: "noopener noreferrer", target: "_blank" }, p.tracking_number) : p.tracking_number)) : null,
      h("div", null, h("div", { class: "proof-label" }, "Units verified"),
        h("div", { class: "proof-value" }, String(p.units.reduce((n, u) => n + (u.quantity || 1), 0))))),
    h("p", { class: "proof-summary" },
      allVerified
        ? "Every item on this order was scanned and matched to the order before it was packed."
        : "Items were scanned and matched to the order before packing. Anything not shipped is shown below.",
      p.errors_caught ? ` ${p.errors_caught} wrong item${p.errors_caught === 1 ? " was" : "s were"} caught and put back before packing.` : ""),
    h("section", { class: "card" },
      h("h2", { class: "card-title" }, "What was ordered"),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", null, h("tr", null, h("th", null, "Item"), h("th", null, "SKU"), h("th", { class: "t-right" }, "Ordered"), h("th", { class: "t-right" }, "Verified"))),
        h("tbody", null, ...p.lines.map((l) => h("tr", null,
          h("td", null, l.description || h("span", { class: "mono" }, l.barcode)),
          h("td", { class: "mono" }, l.sku || "–"),
          h("td", { class: "t-right" }, String(l.ordered)),
          h("td", { class: "t-right" }, l.verified >= l.ordered ? h("span", { class: "ok-text" }, `✓ ${l.verified}`)
            : h("span", null, `${l.verified}`, l.short ? h("span", { class: "muted" }, ` (${l.short} not shipped)`) : null)))))))),
    h("section", { class: "card" },
      h("h2", { class: "card-title" }, "Every unit, as scanned"),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", null, h("tr", null, h("th", null, "Time"), h("th", null, "Item"), h("th", null, "Barcode scanned"), traced ? h("th", null, "Lot / serial / expiry") : null)),
        h("tbody", null, ...p.units.map((u) => h("tr", null,
          h("td", { class: "muted" }, fmtDateTime(u.at, tz)),
          h("td", null, u.item || "–"),
          u.confirmed
            ? h("td", { class: "muted" }, `No barcode: checked by hand${u.quantity > 1 ? ` × ${u.quantity}` : ""}`)
            : h("td", { class: "mono" }, u.barcode, u.quantity > 1 ? h("span", { class: "muted" }, ` × ${u.quantity}`) : null),
          traced ? h("td", { class: "mono" }, trace(u)) : null)))))),
    packPhotos(p, t),
  ];
}

async function main() {
  const host = document.getElementById("proof");
  document.getElementById("print-btn").addEventListener("click", () => window.print());
  const t = token();
  if (!t) {
    mount(host, h("div", { class: "empty" }, h("h1", null, "Link incomplete"), h("p", null, "Copy the whole link from the email or message you were sent.")));
    return;
  }
  try {
    // The token goes in the request body, never in a URL the server logs.
    mount(host, ...render(await request("/api/public/proof", { method: "POST", body: { token: t } }), t));
  } catch (e) {
    mount(host, h("div", { class: "empty" }, h("h1", null, "This link isn't available"),
      h("p", null, e.status === 404 ? "It may have been turned off by the sender. Ask them for a new one." : "Couldn't load it. Try again in a minute.")));
  }
}

main();
