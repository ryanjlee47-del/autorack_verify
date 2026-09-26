// Worker-raised problems (flags and short picks): how they're shown and
// resolved, on the dashboard queue and on an order's page.

import { dialog, fmtAgo, h } from "../../../shared/dom.js";
import { api, fail, photoStrip } from "../core.js";

export const REASONS = {
  wrong_item_in_location: "Wrong item in the bin",
  out_of_stock: "Out of stock",
  damaged: "Damaged",
  label_unreadable: "Label won't scan",
  short_pick: "Short pick",
  not_found: "Not in the bin",
  other: "Other",
};

export function flagTitle(f) {
  if (f.reason === "short_pick") {
    const why = REASONS[f.short_reason] || "";
    return `Short ${f.short_quantity}${f.expected_quantity ? ` of ${f.expected_quantity}` : ""}${why ? ` · ${why}` : ""}`;
  }
  return REASONS[f.reason] || f.reason;
}

const RESOLUTION = { accepted: "Shipped short", reopened: "Sent back to pick", resolved: "Resolved" };

/**
 * One flag with its photos and the buttons to deal with it.
 * `item`: what line it's about (text). `onDone`: called after resolving.
 */
export function flagItem(orderId, f, { item = null, order = null, onDone }) {
  const isShort = f.reason === "short_pick";
  const resolved = !!f.resolved_at;
  return h("li", { class: ["flag-item", resolved && "flag-resolved"] },
    h("div", { class: "row-between" },
      h("span", { class: "row" },
        h("span", { class: ["badge", isShort ? "badge-warn" : "badge-flagged"] }, flagTitle(f)),
        item ? h("span", null, item) : null,
        order ? h("a", { href: `#/orders/${orderId}`, class: "mono" }, order) : null),
      h("span", { class: "muted small nowrap" }, `${f.worker || "A worker"} · ${fmtAgo(f.created_at)}`)),
    f.note ? h("div", { class: "flag-note" }, `“${f.note}”`) : null,
    photoStrip(f.photos),
    resolved
      ? h("div", { class: "muted small" }, RESOLUTION[f.resolution] || "Resolved", f.resolution_note ? ` · ${f.resolution_note}` : "")
      : h("div", { class: "row" },
        isShort
          ? [
            h("button", { class: "btn btn-sm btn-primary", onclick: () => resolve(orderId, f, "accept", onDone) }, "Ship short"),
            h("button", { class: "btn btn-sm", onclick: () => resolve(orderId, f, "reopen", onDone) }, "Pick again"),
          ]
          : h("button", { class: "btn btn-sm btn-primary", onclick: () => resolve(orderId, f, "resolve", onDone) }, "Resolve")));
}

const PROMPTS = {
  accept: ["Ship it short?", "The order can be completed and shipped without the missing units.", "Ship short"],
  reopen: ["Send back to pick?", "The missing units go back on the worker's list, e.g. after restocking the bin.", "Pick again"],
  resolve: ["Resolve problem", "Clears the flag so the order can finish.", "Resolve"],
};

async function resolve(orderId, f, action, onDone) {
  const [title, text, label] = PROMPTS[action];
  const note = await dialog(title, (close) => {
    const input = h("input", { class: "input", placeholder: "Note for the record (optional)", maxlength: "500" });
    return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close(input.value); } },
      h("p", { class: "muted" }, text), input,
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn btn-primary", type: "submit" }, label)));
  });
  if (note === null) return;
  try {
    await api(`/api/orders/${orderId}/flags/${f.id}/resolve`, { method: "POST", body: { note, action } });
    if (onDone) onDone();
  } catch (e) {
    fail(e);
  }
}
