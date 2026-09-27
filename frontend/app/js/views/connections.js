// Connections: stores and spreadsheet links that bring orders in by
// themselves, and the import email / drop URL.

import { confirmDialog, dialog, fmtAgo, fmtNumber, h, toast } from "../../../shared/dom.js";
import { api, canManage, card, fail, isOwner, layout, pageHeader } from "../core.js";

const KINDS = {
  shopify: {
    label: "Shopify",
    blurb: "Unfulfilled orders come in every 10 minutes. When the label is scanned, the order is marked fulfilled in Shopify with its tracking number, and Shopify emails your customer.",
    fields: [
      ["shop", "Store address", "yourstore.myshopify.com"],
      ["token", "Admin API access token", "shpat_…", true],
    ],
    help: [
      "In Shopify admin: Settings → Apps and sales channels → Develop apps → Create an app (name it “Autorack”).",
      "Configuration → Admin API scopes: read_orders, read_products, read_merchant_managed_fulfillment_orders, write_merchant_managed_fulfillment_orders, write_fulfillments.",
      "Install the app, then reveal the Admin API access token once and paste it here.",
    ],
  },
  shipstation: {
    label: "ShipStation",
    blurb: "Orders awaiting shipment come in every 10 minutes, from every store ShipStation is connected to. When the label is scanned, the order is marked shipped in ShipStation, which tells the store and the customer.",
    fields: [
      ["api_key", "API key", "", true],
      ["api_secret", "API secret", "", true],
    ],
    help: ["In ShipStation: Settings → Account → API Settings. Generate keys if there are none, and paste both here."],
  },
  woocommerce: {
    label: "WooCommerce",
    blurb: "Processing orders come in every 10 minutes. When the label is scanned, the order is completed in WooCommerce and the tracking number is added as a note the customer can see.",
    fields: [
      ["store_url", "Store address", "https://shop.example.com"],
      ["consumer_key", "Consumer key", "ck_…", true],
      ["consumer_secret", "Consumer secret", "cs_…", true],
    ],
    help: [
      "In WordPress: WooCommerce → Settings → Advanced → REST API → Add key. Permissions: Read/Write.",
      "Barcodes come from each product's GTIN, UPC, EAN or ISBN field; products without one use their SKU.",
    ],
  },
  sheet: {
    label: "Google Sheet or CSV link",
    blurb: "Autorack re-reads the sheet every 15 minutes and adds any order number it hasn't seen. Same columns as a CSV import.",
    fields: [["url", "Link", "https://docs.google.com/spreadsheets/d/…"]],
    help: [
      "In Google Sheets: Share → General access → “Anyone with the link” (Viewer), then copy the link. Or File → Share → Publish to web → CSV.",
      "Any https link to a CSV file works too.",
    ],
  },
};

const STATE = {
  ok: ["badge-completed", "Working"],
  waiting: ["badge-pending", "Waiting for first sync"],
  failing: ["badge-flagged", "Needs attention"],
  paused: ["badge-cancelled", "Paused"],
};

export async function connectionsView() {
  const data = await api("/api/integrations");
  const reload = () => connectionsView().catch(fail);
  const owner = isOwner();

  layout("#/connections", [
    pageHeader("Connections", "Orders that arrive by themselves, and tracking that goes back to the store.",
      owner ? h("button", { class: "btn btn-primary", onclick: () => connect(reload) }, "Connect a store") : null),
    data.connections.length
      ? h("div", { class: "stack" }, ...data.connections.map((c) => connectionCard(c, reload, owner)))
      : card(null, h("div", { class: "empty" },
        h("p", null, "Nothing connected yet. Connect Shopify, ShipStation, WooCommerce or a Google Sheet and new orders appear on the phones without anyone uploading a file."),
        owner ? h("button", { class: "btn btn-primary", onclick: () => connect(reload) }, "Connect a store") : h("p", { class: "muted" }, "Ask an owner to connect your store."))),
    canManage() ? importAddressCard(data.import_address, reload, owner) : null,
  ]);
}

function connectionCard(c, reload, owner) {
  const [badge, label] = STATE[c.state] || STATE.waiting;
  const where = c.shop || c.store_url || c.url || "";
  const pushToggle = h("input", { type: "checkbox", checked: c.push_tracking, disabled: !owner });
  pushToggle.addEventListener("change", () =>
    api(`/api/integrations/${c.id}`, { method: "PATCH", body: { push_tracking: pushToggle.checked } })
      .then(() => toast(pushToggle.checked ? "Tracking will go back to the store." : "Tracking stays in Autorack.", "success"), fail));

  return h("section", { class: "card connection" },
    h("div", { class: "connection-head" },
      h("div", null,
        h("div", { class: "muted small" }, c.label),
        h("h2", { class: "connection-name" }, c.name),
        where ? h("div", { class: "muted small mono break" }, where) : null),
      h("span", { class: `badge ${badge}` }, label)),
    c.last_error ? h("div", { class: "banner banner-bad" }, c.last_error) : null,
    h("dl", { class: "facts" },
      h("dt", null, "Last checked"), h("dd", null, c.last_sync_at ? fmtAgo(c.last_sync_at) : "not yet"),
      h("dt", null, "Last success"), h("dd", null, c.last_success_at ? fmtAgo(c.last_success_at) : "never"),
      h("dt", null, "Orders brought in"), h("dd", null, `${fmtNumber(c.total_created)} total`, c.last_created ? ` · ${c.last_created} last time` : ""),
      h("dt", null, "Checks every"), h("dd", null, `${c.sync_minutes} minutes`)),
    c.can_push_tracking
      ? h("label", { class: "check" }, pushToggle, " Send tracking numbers back to ", c.label, " when the label is scanned (the store emails the customer)")
      : null,
    h("div", { class: "row" },
      h("button", {
        class: "btn",
        onclick: async (e) => {
          e.target.disabled = true;
          try {
            const r = await api(`/api/integrations/${c.id}/sync`, { method: "POST" });
            if (r.sync.ok) toast(r.sync.created ? `${r.sync.created} new order${r.sync.created === 1 ? "" : "s"} added.` : "Checked. Nothing new.", "success");
            else toast(r.sync.error, "error", 8000);
            reload();
          } catch (err) {
            fail(err);
            e.target.disabled = false;
          }
        },
      }, "Check now"),
      owner ? h("button", {
        class: "btn btn-ghost",
        onclick: () => api(`/api/integrations/${c.id}`, { method: "PATCH", body: { enabled: !c.enabled } }).then(reload, fail),
      }, c.enabled ? "Pause" : "Resume") : null,
      owner ? h("button", { class: "btn btn-ghost", onclick: () => connect(reload, c) }, "Update key") : null,
      owner ? h("button", {
        class: "btn btn-ghost danger-text",
        onclick: async () => {
          if (!(await confirmDialog(`Disconnect ${c.name}?`,
            "Autorack forgets the key and stops pulling orders. Orders already brought in stay, but their tracking won't be sent to the store.",
            { confirmLabel: "Disconnect", danger: true }))) return;
          api(`/api/integrations/${c.id}`, { method: "DELETE" }).then(reload, fail);
        },
      }, "Disconnect") : null));
}

async function connect(reload, existing = null) {
  let kind = existing ? existing.kind : null;
  const done = await dialog(existing ? `Update ${existing.name}` : "Connect a store", (close) => {
    const body = h("div", { class: "stack" });
    const pick = () => {
      body.replaceChildren(
        h("p", { class: "muted" }, "Where do your orders come from?"),
        h("div", { class: "kind-grid" }, ...Object.entries(KINDS).map(([k, v]) =>
          h("button", { class: "kind-btn", type: "button", onclick: () => { kind = k; form(); } }, h("strong", null, v.label)))));
    };
    const form = () => {
      const spec = KINDS[kind];
      const inputs = {};
      const err = h("div", { class: "banner banner-bad", hidden: true });
      const submit = h("button", { class: "btn btn-primary", type: "submit" }, existing ? "Save" : "Connect");
      const push = h("input", { type: "checkbox", checked: true });
      body.replaceChildren(
        h("form", {
          class: "stack",
          onsubmit: async (e) => {
            e.preventDefault();
            err.hidden = true;
            submit.disabled = true;
            submit.textContent = "Checking…";
            const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value.trim()]));
            try {
              const r = existing
                ? await api(`/api/integrations/${existing.id}`, { method: "PATCH", body: values })
                : await api("/api/integrations", { method: "POST", body: { kind, push_tracking: push.checked, ...values } });
              close(r);
            } catch (ex) {
              err.textContent = ex.message || "Couldn't connect.";
              err.hidden = false;
              submit.disabled = false;
              submit.textContent = existing ? "Save" : "Connect";
            }
          },
        },
        h("h3", null, spec.label),
        h("p", { class: "muted small" }, spec.blurb),
        h("ol", { class: "help-steps small" }, ...spec.help.map((t) => h("li", null, t))),
        ...spec.fields.flatMap(([name, label, placeholder, secret]) => {
          const value = existing && !secret ? existing[name] || "" : "";
          inputs[name] = h("input", {
            class: ["input", secret && "mono"],
            name,
            placeholder,
            value,
            type: secret ? "password" : "text",
            autocomplete: "off",
            required: true,
          });
          return [h("label", null, label), inputs[name]];
        }),
        !existing && kind !== "sheet" ? h("label", { class: "check" }, push, " Send tracking numbers back when the label is scanned") : null,
        h("p", { class: "muted small" }, "Keys are stored encrypted and never shown again. Autorack checks them before saving."),
        err,
        h("div", { class: "dialog-actions" },
          existing ? null : h("button", { class: "btn", type: "button", onclick: pick }, "Back"),
          h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
          submit)));
      const first = body.querySelector("input");
      if (first) first.focus();
    };
    if (kind) form();
    else pick();
    return body;
  }, { wide: true });
  if (!done) return;
  if (done.sync) {
    const s = done.sync;
    toast(s.ok ? `Connected. ${s.created} order${s.created === 1 ? "" : "s"} brought in.` : `Connected, but the first sync failed: ${s.error}`, s.ok ? "success" : "error", 8000);
  } else {
    toast("Saved.", "success");
  }
  reload();
}

function importAddressCard(addr, reload, owner) {
  const reveal = h("button", {
    class: "btn",
    onclick: () => api("/api/integrations/import-address", { method: "POST" }).then(reload, fail),
  }, "Show my import address");
  const copyRow = (label, value) => h("div", { class: "copy-row" },
    h("div", { class: "muted small" }, label),
    h("div", { class: "row" },
      h("input", { class: "input mono", readonly: true, value, onclick: (e) => e.target.select() }),
      h("button", { class: "btn", onclick: () => navigator.clipboard.writeText(value).then(() => toast("Copied.", "success"), fail) }, "Copy")));
  const ready = addr && addr.drop_url;
  return card("Import by email or from a folder",
    h("p", { class: "muted small" }, "For systems that can send a CSV but don't connect to anything: an ERP that emails a nightly pick list, or a folder a program saves exports into. Same columns as a CSV import; order numbers already in Autorack are skipped; rows with problems are left out."),
    ready
      ? h("div", { class: "stack" },
        addr.email ? copyRow("Email a CSV attachment to", addr.email) : h("p", { class: "muted small" }, "Import by email isn't set up on this Autorack server yet (see the deployment guide)."),
        copyRow("Or post a CSV to (drop URL)", addr.drop_url),
        h("p", { class: "small break" },
          "Watched folder: download ", h("a", { href: "/downloads/autorack-watch.py", download: "autorack-watch.py" }, "autorack-watch.py"),
          ", then run ", h("code", null, `python autorack-watch.py "C:\\Exports" ${addr.drop_url}`),
          ". Every CSV saved into the folder is uploaded, then moved into an “imported” subfolder."),
        h("p", { class: "muted small" }, "Anyone with this address can add orders. If it leaks, make a new one."),
        owner ? h("div", { class: "row" }, h("button", {
          class: "btn btn-ghost",
          onclick: async () => {
            if (!(await confirmDialog("Make a new import address?", "The current email address and drop URL stop working immediately. Update anything that sends to them.", { confirmLabel: "Make a new one", danger: true }))) return;
            api("/api/integrations/import-address/rotate", { method: "POST" }).then(reload, fail);
          },
        }, "Make a new address")) : null)
      : reveal);
}
