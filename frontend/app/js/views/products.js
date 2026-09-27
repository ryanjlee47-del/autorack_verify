// Products: the catalog. Pictures and packer notes for the phones, extra and
// case barcodes, kits, approved substitutes, CSV import and barcode labels.

import { imageUrl } from "../../../shared/api.js";
import { confirmDialog, dialog, fmtAgo, fmtNumber, h, mount, toast } from "../../../shared/dom.js";
import { api, canManage, card, download, fail, getToken, layout, pageHeader, table } from "../core.js";

const TABS = [
  ["active", "All products"],
  ["kits", "Kits"],
  ["no_image", "No picture"],
  ["no_barcode", "No barcode"],
  ["archived", "Archived"],
];

function thumb(p, cls = "product-thumb") {
  return p.thumb
    ? h("img", { class: cls, src: p.thumb, alt: "" })
    : h("span", { class: [cls, "product-thumb-empty"], "aria-hidden": "true" }, "📦");
}

function grams(g) {
  if (g === null || g === undefined) return "–";
  return g >= 1000 ? `${(g / 1000).toFixed(g % 1000 ? 2 : 0)} kg` : `${g} g`;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export async function productsView(params) {
  const show = params.get("show") || "active";
  const q = params.get("q") || "";
  const selected = new Set();
  const listHost = h("div", null, h("div", { class: "skeleton" }));
  const search = h("input", { class: "input", type: "search", placeholder: "Search name, SKU, barcode or bin", value: q });
  const go = (next) => {
    const p = new URLSearchParams({ show: next.show ?? show, q: next.q ?? search.value });
    location.hash = `#/products?${p}`;
  };
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") go({}); });
  const manage = canManage();

  layout("#/products", [
    pageHeader("Products", "Pictures, bins and notes the phones show, case barcodes, kits and approved substitutes.",
      manage ? h("button", { class: "btn", onclick: () => importDialog() }, "Import CSV") : null,
      h("button", { class: "btn", onclick: () => printLabels([...selected]) }, "Print labels"),
      manage ? h("button", { class: "btn btn-primary", onclick: () => newProduct() }, "New product") : null),
    h("div", { class: "toolbar" },
      h("div", { class: "tabs" }, ...TABS.map(([v, label]) =>
        h("button", { class: ["tab", show === v && "active"], onclick: () => go({ show: v }) }, label))),
      h("div", { class: "row" }, search)),
    card(null, listHost),
  ]);

  const r = await api(`/api/products?${new URLSearchParams({ show, q, limit: "500" })}`);
  const rows = r.products;
  const render = () => {
    const all = h("input", {
      type: "checkbox", "aria-label": "Select all",
      onchange: (e) => { rows.forEach((p) => (e.target.checked ? selected.add(p.id) : selected.delete(p.id))); render(); },
    });
    all.checked = rows.length > 0 && rows.every((p) => selected.has(p.id));
    mount(listHost,
      r.total > rows.length ? h("p", { class: "muted small" }, `Showing ${rows.length} of ${fmtNumber(r.total)}. Search to narrow it down.`) : null,
      table([
        {
          label: all,
          render: (p) => {
            const box = h("input", {
              type: "checkbox", "aria-label": `Select ${p.name}`,
              onclick: (e) => e.stopPropagation(),
              onchange: (e) => (e.target.checked ? selected.add(p.id) : selected.delete(p.id)),
            });
            box.checked = selected.has(p.id);
            return box;
          },
        },
        { label: "", render: (p) => thumb(p) },
        {
          label: "Product",
          render: (p) => h("div", null,
            h("strong", null, p.name),
            p.is_kit ? h("span", { class: "badge badge-kind badge-inline" }, "Kit") : null,
            p.no_barcode ? h("span", { class: "badge badge-warn badge-inline" }, "No barcode") : null,
            p.packer_note ? h("div", { class: "muted small" }, "⚠ ", p.packer_note) : null),
        },
        { label: "SKU", render: (p) => h("span", { class: "mono" }, p.sku || "–") },
        {
          label: "Barcode",
          render: (p) => h("span", null, h("span", { class: "mono" }, p.barcode || "–"),
            p.max_pack > 1 ? h("span", { class: "muted small" }, ` · case of ${p.max_pack}`) : null),
        },
        { label: "Bin", render: (p) => p.location || h("span", { class: "muted" }, "–") },
        { label: "Weight", align: "right", render: (p) => grams(p.weight_grams) },
        { label: "Updated", render: (p) => h("span", { class: "muted small" }, fmtAgo(p.updated_at)) },
      ], rows, {
        empty: show === "active" && !q
          ? "No products yet. Import your catalog as CSV, connect a store (Connections) to bring products in with their pictures, or add one."
          : "No products match.",
        onRow: (p) => { location.hash = `#/products/${p.id}`; },
      }));
  };
  render();
}

function printLabels(ids) {
  if (!ids.length) return toast("Tick the products to print labels for.", "warn");
  dialog("Print labels", (close) => {
    const size = h("select", { class: "input" },
      h("option", { value: "thermal" }, "Label printer: 2.25 × 1.25 in (Zebra, Rollo, DYMO)"),
      h("option", { value: "sheet" }, "Sheet: 30 per page (Avery 5160 / 8160)"));
    const qty = h("input", { class: "input input-qty", type: "number", min: "1", max: "500", value: "1" });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close(true);
        window.open(`/app/print.html?labels=${ids.join(",")}&size=${size.value}&qty=${Number(qty.value) || 1}`, "_blank", "noopener");
      },
    },
    h("label", null, "Label size", size),
    h("label", null, "Copies of each", qty),
    h("p", { class: "muted small" }, "Each label has the product's barcode (or its SKU, if it has no barcode) as a Code 128 barcode every scanner reads, with its name. No barcode or SKU? Open the product and use “Give it a barcode”."),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
      h("button", { class: "btn btn-primary", type: "submit" }, "Print")));
  });
}

async function newProduct() {
  const created = await dialog("New product", (close) => {
    const f = {
      name: h("input", { class: "input", required: true, maxlength: "500", placeholder: "e.g. Blue widget (12 pk)" }),
      sku: h("input", { class: "input mono", maxlength: "100" }),
      barcode: h("input", { class: "input mono", maxlength: "200", placeholder: "Scan it here" }),
      location: h("input", { class: "input", maxlength: "100", placeholder: "e.g. A-01-03" }),
    };
    const err = h("div", { class: "banner banner-bad", hidden: true });
    return h("form", {
      class: "form-grid",
      onsubmit: async (e) => {
        e.preventDefault();
        try {
          close(await api("/api/products", {
            method: "POST",
            body: { name: f.name.value.trim(), sku: f.sku.value.trim() || null, barcode: f.barcode.value.trim() || null, location: f.location.value.trim() || null },
          }));
        } catch (ex) {
          err.textContent = ex.message;
          err.hidden = false;
        }
      },
    },
    h("label", { class: "span-2" }, "Name", f.name),
    h("label", null, "SKU", f.sku), h("label", null, "Barcode", f.barcode),
    h("label", null, "Bin", f.location),
    h("p", { class: "muted small span-2" }, "Add the picture, case barcodes, kit contents and substitutes on the next screen."),
    h("div", { class: "span-2" }, err),
    h("div", { class: "dialog-actions span-2" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
      h("button", { class: "btn btn-primary", type: "submit" }, "Create")));
  });
  if (created) location.hash = `#/products/${created.id}`;
}

function importDialog() {
  dialog("Import products", (close) => {
    const file = h("input", { type: "file", accept: ".csv,.tsv,.txt,text/csv", class: "input" });
    const out = h("div");
    const btn = h("button", { class: "btn btn-primary", type: "submit" }, "Import");
    return h("form", {
      class: "stack",
      onsubmit: async (e) => {
        e.preventDefault();
        if (!file.files[0]) return;
        btn.disabled = true;
        const fd = new FormData();
        fd.append("file", file.files[0]);
        try {
          const r = await api("/api/products/import", { method: "POST", form: fd, timeoutMs: 180000 });
          mount(out,
            h("div", { class: "banner banner-ok" }, `${r.created} added, ${r.updated} updated.`),
            r.error_count ? h("div", { class: "stack" },
              h("div", { class: "banner banner-warn" }, `${r.error_count} row(s) skipped:`),
              table([{ label: "Row", key: "row" }, { label: "Problem", key: "message" }], r.errors)) : null);
          btn.textContent = "Done";
          btn.disabled = false;
          btn.onclick = (ev) => { ev.preventDefault(); close(true); productsView(new URLSearchParams(location.hash.split("?")[1] || "")).catch(fail); };
        } catch (ex) {
          mount(out, h("div", { class: "banner banner-bad" }, ex.message));
          btn.disabled = false;
        }
      },
    },
    h("p", null, "Columns: ", h("span", { class: "mono" }, "sku, barcode, name"), " and optionally ",
      h("span", { class: "mono" }, "location, weight_grams (or weight_oz / weight_lb), case_barcode, case_qty, packer_note, track"),
      ". Existing products are matched by SKU (else barcode) and updated."),
    h("p", null, h("a", { href: "#", onclick: (e) => { e.preventDefault(); download("/api/products/template.csv", "autorack-products-template.csv"); } }, "Download the template")),
    file, out,
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Close"), btn));
  }, { wide: true });
}

// ---------------------------------------------------------------------------
// One product
// ---------------------------------------------------------------------------

export async function productView(id) {
  const p = await api(`/api/products/${id}`);
  const reload = () => productView(id).catch(fail);
  const manage = canManage();

  const f = {
    name: h("input", { class: "input", value: p.name, maxlength: "500", disabled: !manage }),
    sku: h("input", { class: "input mono", value: p.sku || "", maxlength: "100", disabled: !manage }),
    barcode: h("input", { class: "input mono", value: p.barcode || "", maxlength: "200", disabled: !manage }),
    location: h("input", { class: "input", value: p.location || "", maxlength: "100", disabled: !manage }),
    weight: h("input", { class: "input input-qty", type: "number", min: "0", value: p.weight_grams ?? "", disabled: !manage }),
    note: h("input", { class: "input", value: p.packer_note || "", maxlength: "500", placeholder: "e.g. Fragile: wrap in bubble", disabled: !manage }),
  };
  const checks = {};
  for (const k of ["no_barcode", "track_lot", "track_serial", "track_expiry"]) {
    checks[k] = h("input", { type: "checkbox", checked: p[k], disabled: !manage });
  }
  const save = async () => {
    try {
      await api(`/api/products/${id}`, {
        method: "PATCH",
        body: {
          name: f.name.value.trim(), sku: f.sku.value.trim() || null, barcode: f.barcode.value.trim() || null,
          location: f.location.value.trim() || null, weight_grams: f.weight.value === "" ? null : Number(f.weight.value),
          packer_note: f.note.value.trim() || null,
          ...Object.fromEntries(Object.entries(checks).map(([k, el]) => [k, el.checked])),
        },
      });
      toast("Saved. Open orders with this product are updated.", "ok");
      reload();
    } catch (e) {
      fail(e);
    }
  };

  const photo = h("div", { class: "product-photo" }, thumb(p, "product-photo-img"));
  if (p.image_id) {
    imageUrl(`/api/products/${id}/image`, getToken()).then((url) => mount(photo, h("img", { class: "product-photo-img", src: url, alt: p.name })), () => {});
  }
  const upload = h("input", { type: "file", accept: "image/*", class: "visually-hidden", id: "product-photo-file" });
  upload.addEventListener("change", async () => {
    const file = upload.files[0];
    if (!file) return;
    try {
      await api(`/api/products/${id}/image`, { method: "POST", blob: file, timeoutMs: 120000 });
      toast("Picture saved", "ok");
      reload();
    } catch (e) {
      fail(e);
    }
  });

  layout("#/products", [
    h("a", { href: "#/products", class: "back-link" }, "← Products"),
    pageHeader(p.name, [p.sku && `SKU ${p.sku}`, p.barcode && `Barcode ${p.barcode}`, p.active ? null : "Archived"].filter(Boolean).join(" · ") || null,
      h("button", { class: "btn", onclick: () => window.open(`/app/print.html?labels=${id}&size=thermal`, "_blank", "noopener") }, "Print label"),
      manage && !p.barcode ? h("button", {
        class: "btn",
        onclick: () => api(`/api/products/${id}/assign-barcode`, { method: "POST" }).then(() => { toast("Barcode assigned. Print labels for it.", "ok"); reload(); }, fail),
      }, "Give it a barcode") : null,
      manage ? h("button", {
        class: p.active ? "btn btn-ghost danger-text" : "btn",
        onclick: async () => {
          if (p.active) {
            if (!(await confirmDialog("Archive this product?", "It stops being linked to new orders. Past orders keep it. You can restore it later.", { confirmLabel: "Archive", danger: true }))) return;
            await api(`/api/products/${id}`, { method: "DELETE" }).then(() => { location.hash = "#/products"; }, fail);
          } else {
            await api(`/api/products/${id}/restore`, { method: "POST" }).then(reload, fail);
          }
        },
      }, p.active ? "Archive" : "Restore") : null),

    h("div", { class: "grid-main" },
      card("Details",
        h("div", { class: "form-grid" },
          h("label", { class: "span-2" }, "Name", f.name),
          h("label", null, "SKU", f.sku), h("label", null, "Barcode (the unit)", f.barcode),
          h("label", null, "Bin", f.location), h("label", null, "Weight (grams)", f.weight),
          h("label", { class: "span-2" }, "Packer note (shown in big letters on the phone)", f.note),
          h("fieldset", { class: "span-2 trace-fields" },
            h("legend", null, "On the floor"),
            h("label", { class: "check" }, checks.no_barcode, " Has no barcode: workers confirm it by tapping"),
            h("label", { class: "check" }, checks.track_lot, " Record lot"),
            h("label", { class: "check" }, checks.track_serial, " Record serial"),
            h("label", { class: "check" }, checks.track_expiry, " Record expiry"))),
        manage ? h("div", { class: "row" }, h("button", { class: "btn btn-primary", onclick: save }, "Save")) : null),
      h("div", { class: "stack-lg" },
        card("Picture",
          photo,
          manage ? h("div", { class: "row" },
            upload,
            h("label", { class: "btn", for: "product-photo-file" }, p.image_id ? "Replace picture" : "Add picture"),
            p.image_id ? h("button", { class: "btn btn-ghost", onclick: () => api(`/api/products/${id}/image`, { method: "DELETE" }).then(reload, fail) }, "Remove") : null) : null,
          h("p", { class: "muted small" }, "Shown on the phone when this item is picked. Workers catch look-alikes faster with a picture.")))),

    barcodesCard(p, manage, reload),
    kitCard(p, manage, reload),
    substitutesCard(p, manage, reload),
  ]);
}

function barcodesCard(p, manage, reload) {
  const code = h("input", { class: "input mono", placeholder: "Scan or type a barcode" });
  const qty = h("input", { class: "input input-qty", type: "number", min: "1", value: "1", "aria-label": "Units per scan" });
  const label = h("input", { class: "input", placeholder: "Label (optional), e.g. Inner pack" });
  return card("More barcodes and case packs",
    h("p", { class: "muted small" }, "Another barcode for the same item (units per scan 1), or a case/inner-pack barcode that counts several units in one scan. A whole case is refused when fewer units are still needed."),
    table([
      { label: "Barcode", render: (b) => h("span", { class: "mono" }, b.barcode) },
      { label: "Units per scan", align: "right", render: (b) => fmtNumber(b.pack_qty) },
      { label: "Label", render: (b) => b.label || h("span", { class: "muted" }, "–") },
      manage ? {
        label: "",
        render: (b) => h("button", { class: "btn btn-sm btn-ghost", onclick: () => api(`/api/products/${p.id}/barcodes/${b.id}`, { method: "DELETE" }).then(reload, fail) }, "Remove"),
      } : null,
    ].filter(Boolean), p.barcodes, { empty: "Just the one barcode." }),
    manage ? h("form", {
      class: "inline-form",
      onsubmit: (e) => {
        e.preventDefault();
        api(`/api/products/${p.id}/barcodes`, { method: "POST", body: { barcode: code.value.trim(), pack_qty: Number(qty.value) || 1, label: label.value.trim() || null } }).then(reload, fail);
      },
    }, code, qty, label, h("button", { class: "btn", type: "submit" }, "Add")) : null);
}

/** Search the catalog; resolves with the chosen product or null. */
function pickProduct(title, excludeId) {
  return dialog(title, (close) => {
    const q = h("input", { class: "input", type: "search", placeholder: "Search name, SKU or barcode", autofocus: true });
    const results = h("div", { class: "pick-list" });
    let timer = null;
    const run = async () => {
      const r = await api(`/api/products?${new URLSearchParams({ q: q.value, limit: "20" })}`);
      mount(results, ...r.products.filter((x) => x.id !== excludeId).map((x) =>
        h("button", { class: "pick-item", type: "button", onclick: () => close(x) }, thumb(x),
          h("span", null, h("strong", null, x.name), h("span", { class: "muted small mono" }, ` ${x.sku || ""} ${x.barcode || ""}`)))));
      if (!results.children.length) mount(results, h("p", { class: "muted" }, "No products match."));
    };
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => run().catch(fail), 200); });
    run().catch(fail);
    return h("div", { class: "stack" }, q, results,
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", onclick: () => close(null) }, "Cancel")));
  }, { wide: true });
}

function kitCard(p, manage, reload) {
  const items = p.components.map((c) => ({ ...c }));
  const save = () => api(`/api/products/${p.id}/components`, {
    method: "PUT", body: { components: items.map((c) => ({ product_id: c.product_id, quantity: c.quantity })) },
  }).then(() => { toast(items.length ? "Kit saved" : "No longer a kit", "ok"); reload(); }, fail);
  return card("Kit contents",
    h("p", { class: "muted small" }, "Make this a kit (bundle): when it's ordered, workers pick and scan its parts instead. Ordering 3 kits of 2 widgets means 6 widgets."),
    table([
      { label: "Part", render: (c) => h("div", null, c.name, h("div", { class: "muted small mono" }, c.sku || c.barcode || "")) },
      {
        label: "Qty per kit", align: "right",
        render: (c) => manage
          ? h("input", { class: "input input-qty", type: "number", min: "1", value: String(c.quantity), onchange: (e) => { c.quantity = Math.max(1, Number(e.target.value) || 1); } })
          : String(c.quantity),
      },
      manage ? { label: "", render: (c) => h("button", { class: "btn btn-sm btn-ghost", onclick: () => { items.splice(items.indexOf(c), 1); save(); } }, "Remove") } : null,
    ].filter(Boolean), items, { empty: "Not a kit." }),
    manage ? h("div", { class: "row" },
      h("button", {
        class: "btn",
        onclick: async () => {
          const x = await pickProduct("Add a part", p.id);
          if (!x) return;
          const have = items.find((c) => c.product_id === x.id);
          if (have) have.quantity += 1;
          else items.push({ product_id: x.id, name: x.name, sku: x.sku, barcode: x.barcode, quantity: 1 });
          save();
        },
      }, "+ Add part"),
      items.length ? h("button", { class: "btn", onclick: save }, "Save quantities") : null) : null);
}

function substitutesCard(p, manage, reload) {
  return card("Approved substitutes",
    h("p", { class: "muted small" }, "When this item runs out, workers may scan one of these instead. It counts for the order and is recorded as a substitution, not a mistake."),
    table([
      { label: "Substitute", render: (s) => h("div", null, s.name, h("div", { class: "muted small mono" }, s.sku || s.barcode || "")) },
      { label: "Note", render: (s) => s.note || h("span", { class: "muted" }, "–") },
      manage ? { label: "", render: (s) => h("button", { class: "btn btn-sm btn-ghost", onclick: () => api(`/api/products/${p.id}/substitutes/${s.product_id}`, { method: "DELETE" }).then(reload, fail) }, "Remove") } : null,
    ].filter(Boolean), p.substitutes, { empty: "None: only this item counts." }),
    manage ? h("button", {
      class: "btn",
      onclick: async () => {
        const x = await pickProduct("Approve a substitute", p.id);
        if (!x) return;
        const note = await dialog("Why is it OK?", (close) => {
          const input = h("input", { class: "input", maxlength: "300", placeholder: "Optional, e.g. same item, new packaging" });
          return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close(input.value.trim()); } }, input,
            h("div", { class: "dialog-actions" }, h("button", { class: "btn btn-primary", type: "submit" }, "Approve")));
        });
        if (note === null) return;
        api(`/api/products/${p.id}/substitutes`, { method: "POST", body: { substitute_id: x.id, note: note || null } }).then(reload, fail);
      },
    }, "+ Approve a substitute") : null);
}
