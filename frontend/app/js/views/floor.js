// The floor around the pick: restock tasks, the time clock, pack inserts,
// and 3PL clients.

import { confirmDialog, dialog, fmtAgo, fmtDateTime, fmtCents, h, mount, toast } from "../../../shared/dom.js";
import { api, canManage, card, ctx, download, fail, layout, pageHeader, table, tz, wantsNew } from "../core.js";
import { pickProduct } from "./products.js";

// ---------------------------------------------------------------------------
// Restock
// ---------------------------------------------------------------------------

const SOURCES = { worker: "Worker: bin empty", short_pick: "Short pick", flag: "Problem report" };

export async function restockView(params) {
  const status = params.get("status") || "open";
  const host = h("div", null, h("div", { class: "skeleton" }));
  layout("#/restock", [
    pageHeader("Restock", "Bins workers found empty or short. Refill them, then mark them done here or on a phone."),
    h("div", { class: "tabs" }, ...[["open", "To refill"], ["done", "Refilled"], ["cancelled", "Dismissed"]].map(([v, label]) =>
      h("a", { class: ["tab", status === v && "active"], href: `#/restock?status=${v}` }, label))),
    card(null, host),
  ]);
  const r = await api(`/api/restock?status=${status}`);
  const manage = canManage() && status === "open";
  const act = (t, what) => api(`/api/restock/${t.id}/${what}`, { method: "POST" })
    .then(() => { toast(what === "done" ? "Marked refilled" : "Dismissed", "ok"); restockView(params).catch(fail); }, fail);
  mount(host, table([
    { label: "Bin", render: (t) => h("span", { class: "mono strong" }, t.location || "–") },
    { label: "Item", render: (t) => h("div", null, t.description || t.sku || "", h("div", { class: "muted small mono" }, [t.sku, t.barcode].filter(Boolean).join(" · "))) },
    { label: "Why", render: (t) => h("div", null, SOURCES[t.source] || t.source, t.note ? h("div", { class: "muted small" }, `“${t.note}”`) : null) },
    { label: "Reported", render: (t) => h("span", { class: "muted" }, `${fmtAgo(t.created_at)}${t.reported_by ? ` · ${t.reported_by}` : ""}`) },
    status === "open" ? null : { label: "Closed", render: (t) => h("span", { class: "muted" }, `${fmtDateTime(t.done_at, tz())}${t.done_by ? ` · ${t.done_by}` : ""}`) },
    t0(manage, (t) => h("div", { class: "row" },
      h("button", { class: "btn btn-sm btn-primary", onclick: () => act(t, "done") }, "Refilled"),
      h("button", { class: "btn btn-sm btn-ghost", onclick: () => act(t, "cancel") }, "Dismiss"))),
  ].filter(Boolean), r.tasks, {
    empty: status === "open" ? "Nothing to refill. When a worker taps “Bin empty” or reports a short pick, it shows up here." : "Nothing here.",
  }));
}

function t0(show, render) {
  return show ? { label: "", render } : null;
}

// ---------------------------------------------------------------------------
// Time clock
// ---------------------------------------------------------------------------

function isoDay(d) {
  return d.toISOString().slice(0, 10);
}

export async function timeView(params) {
  const today = new Date();
  const to = params.get("to") || isoDay(today);
  const from = params.get("from") || isoDay(new Date(today.getTime() - 6 * 86400000));
  const fromIn = h("input", { class: "input input-inline", type: "date", value: from });
  const toIn = h("input", { class: "input input-inline", type: "date", value: to });
  const host = h("div", null, h("div", { class: "skeleton" }));
  const totalsHost = h("div");
  layout("#/time", [
    pageHeader("Time clock", "Workers clock in and out on the phone. Units per hour on the Workers page use these hours.",
      h("button", { class: "btn", onclick: () => download(`/api/exports/timesheet.csv?from=${fromIn.value}&to=${toIn.value}`, `autorack-timesheet-${fromIn.value}.csv`) }, "Download timesheet (CSV)")),
    ctx.me.warehouse.time_clock_enabled ? null : h("div", { class: "banner banner-info" },
      "The time clock is off. Turn it on in ", h("a", { href: "#/settings" }, "Settings → On the floor"), " and workers will be asked to clock in when they sign in."),
    h("div", { class: "toolbar" },
      h("div", { class: "row" }, "From", fromIn, "to", toIn,
        h("button", { class: "btn", onclick: () => { location.hash = `#/time?from=${fromIn.value}&to=${toIn.value}`; } }, "Show"))),
    h("div", { class: "grid-main" }, card("Shifts", host), card("Hours per worker", totalsHost)),
  ]);
  const r = await api(`/api/shifts?from=${from}&to=${to}`);
  const zone = tz();
  mount(totalsHost, table([
    { label: "Worker", render: (t) => t.worker },
    { label: "Hours", align: "right", render: (t) => t.hours.toFixed(2) },
  ], r.totals, { empty: "No hours in this range." }));
  mount(host, table([
    { label: "Worker", render: (s) => s.worker || "–" },
    { label: "In", render: (s) => fmtDateTime(s.clock_in, zone) },
    {
      label: "Out",
      render: (s) => s.clock_out
        ? h("span", null, fmtDateTime(s.clock_out, zone),
          s.closed_by === "auto" ? h("span", { class: "badge badge-flagged", title: "Nobody clocked out; closed automatically at the last scan" }, " auto") : null,
          s.edited ? h("span", { class: "muted small" }, " · edited") : null)
        : h("span", { class: "badge badge-in_progress" }, "On the clock"),
    },
    { label: "Hours", align: "right", render: (s) => s.hours.toFixed(2) },
    canManage() ? { label: "", render: (s) => h("button", { class: "btn btn-sm btn-ghost", onclick: () => editShift(s, () => timeView(params).catch(fail)) }, "Fix") } : null,
  ].filter(Boolean), r.shifts, { empty: "No shifts in this range." }));
}

function localInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function editShift(s, reload) {
  const inIn = h("input", { class: "input", type: "datetime-local", value: localInput(s.clock_in) });
  const outIn = h("input", { class: "input", type: "datetime-local", value: localInput(s.clock_out) });
  const ok = await dialog(`Fix ${s.worker || "shift"}`, (close) => [
    h("p", { class: "muted small" }, "Times in this browser's time zone. The change is recorded in the activity log."),
    h("label", null, "Clock in", inIn),
    h("label", null, "Clock out", outIn),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", onclick: () => close(false) }, "Cancel"),
      h("button", { class: "btn btn-primary", onclick: () => close(true) }, "Save")),
  ]);
  if (!ok) return;
  const body = {};
  if (inIn.value) body.clock_in = new Date(inIn.value).toISOString();
  if (outIn.value) body.clock_out = new Date(outIn.value).toISOString();
  api(`/api/shifts/${s.id}`, { method: "PATCH", body }).then(() => { toast("Shift saved", "ok"); reload(); }, fail);
}

// ---------------------------------------------------------------------------
// Pack inserts
// ---------------------------------------------------------------------------

export async function insertsView(params = new URLSearchParams()) {
  const [inserts, clients] = await Promise.all([api("/api/inserts"), api("/api/clients")]);
  const reload = () => insertsView().catch(fail);
  const clientName = new Map(clients.map((c) => [c.id, c.name]));
  const manage = canManage();
  const scope = (i) => i.product_id ? "Orders with one product" : i.client_id ? `${clientName.get(i.client_id) || "A client"}'s orders` : "Every order";
  layout("#/inserts", [
    pageHeader("Pack inserts", "Flyers, thank-you cards, samples: anything that goes in the box besides the order. The packer ticks each one off (or scans it) before the label goes on.",
      manage ? h("button", { class: "btn btn-primary", onclick: () => editInsert(null, clients, reload) }, "New insert") : null),
    card(null, table([
      { label: "Insert", render: (i) => h("div", null, h("strong", null, i.name), i.barcode ? h("div", { class: "muted small mono" }, i.barcode) : null) },
      { label: "Goes in", render: scope },
      { label: "Packer", render: (i) => (i.scan_required ? "Must scan it" : "Ticks it off") },
      { label: "", render: (i) => (i.active ? h("span", { class: "badge badge-completed" }, "Active") : h("span", { class: "badge" }, "Retired")) },
      manage ? {
        label: "", render: (i) => h("div", { class: "row" },
          h("button", { class: "btn btn-sm", onclick: () => editInsert(i, clients, reload) }, "Edit"),
          i.active ? h("button", {
            class: "btn btn-sm btn-ghost",
            onclick: async () => {
              if (!(await confirmDialog("Retire this insert?", "Packers stop being asked for it. Orders that already had it keep the record.", { confirmLabel: "Retire" }))) return;
              api(`/api/inserts/${i.id}`, { method: "DELETE" }).then(reload, fail);
            },
          }, "Retire") : h("button", { class: "btn btn-sm btn-ghost", onclick: () => api(`/api/inserts/${i.id}`, { method: "PATCH", body: { active: true } }).then(reload, fail) }, "Bring back")),
      } : null,
    ].filter(Boolean), inserts, { empty: "No inserts. Add one and packers will be asked for it on every matching order." })),
  ]);
  if (wantsNew(params)) editInsert(null, clients, reload);
}

async function editInsert(ins, clients, reload) {
  const name = h("input", { class: "input", value: ins ? ins.name : "", placeholder: "e.g. Spring sale flyer", maxlength: "200" });
  const barcode = h("input", { class: "input mono", value: ins && ins.barcode ? ins.barcode : "", placeholder: "Optional" });
  const mustScan = h("input", { type: "checkbox" });
  mustScan.checked = Boolean(ins && ins.scan_required);
  const where = h("select", { class: "input" },
    h("option", { value: "all" }, "Every order"),
    h("option", { value: "client", disabled: !clients.length }, clients.length ? "One client's orders" : "One client's orders (add a client first)"),
    h("option", { value: "product" }, "Orders with a particular product"));
  where.value = ins && ins.product_id ? "product" : ins && ins.client_id ? "client" : "all";
  const clientSel = h("select", { class: "input" }, ...clients.map((c) => h("option", { value: c.id, selected: ins && ins.client_id === c.id }, c.name)));
  let product = ins && ins.product_id ? { id: ins.product_id, name: "(current product)" } : null;
  const productLabel = h("span", { class: "muted" }, product ? product.name : "None chosen");
  const productBtn = h("button", {
    class: "btn btn-sm", type: "button",
    onclick: async () => {
      const p = await pickProduct("Orders containing…");
      if (p) {
        product = p;
        productLabel.textContent = p.name;
      }
    },
  }, "Choose product");
  const clientRow = h("label", null, "Client", clientSel);
  const productRow = h("div", { class: "row" }, productBtn, productLabel);
  const sync = () => {
    clientRow.hidden = where.value !== "client";
    productRow.hidden = where.value !== "product";
  };
  where.addEventListener("change", sync);
  sync();
  const ok = await dialog(ins ? "Edit insert" : "New insert", (close) => h("form", {
    class: "stack",
    onsubmit: (e) => {
      e.preventDefault();
      close(true);
    },
  },
  h("label", null, "Name", name),
  h("label", null, "Goes in", where), clientRow, productRow,
  h("label", null, "Barcode", barcode),
  h("label", { class: "row check" }, mustScan, "Packer must scan it (needs a barcode)"),
  h("div", { class: "dialog-actions" },
    h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"),
    h("button", { class: "btn btn-primary", type: "submit" }, "Save"))));
  if (!ok) return;
  const body = {
    name: name.value.trim(),
    barcode: barcode.value.trim(),
    scan_required: mustScan.checked,
  };
  if (where.value === "client" && clientSel.value) body.client_id = clientSel.value;
  if (where.value === "product" && product) body.product_id = product.id;
  if (ins) {
    if (!body.client_id) body.clear_client = true;
    if (!body.product_id) body.clear_product = true;
  } else if (!body.barcode) {
    delete body.barcode;
  }
  try {
    await api(ins ? `/api/inserts/${ins.id}` : "/api/inserts", { method: ins ? "PATCH" : "POST", body });
    toast("Insert saved. Phones pick it up on their next sync.", "ok");
    reload();
  } catch (e) {
    fail(e);
  }
}

// ---------------------------------------------------------------------------
// Clients (3PL brands)
// ---------------------------------------------------------------------------

function thisMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export async function clientsView(params = new URLSearchParams()) {
  const month = params.get("month") || thisMonth();
  const manage = canManage();
  const [clients, billing] = await Promise.all([
    api("/api/clients"),
    manage ? api(`/api/billing/clients?month=${month}`) : Promise.resolve(null),
  ]);
  const reload = () => clientsView(params).catch(fail);
  const totals = new Map((billing ? billing.clients : []).map((r) => [r.client.id, r]));
  const picker = h("input", { class: "input input-inline", type: "month", value: month, onchange: (e) => { location.hash = `#/clients?month=${e.target.value}`; } });
  layout("#/clients", [
    pageHeader("Clients", "For 3PLs: the brands you ship for. Tag orders and products with a client, give each one a portal login to follow its own orders, and bill them from the scan records.",
      manage ? h("button", { class: "btn btn-primary", onclick: () => editClient(null, reload) }, "New client") : null),
    card(null,
      manage ? h("div", { class: "row" }, h("span", { class: "muted" }, "Billing month"), picker) : null,
      table([
        { label: "Client", render: (c) => h("div", null, h("strong", null, c.name), c.code ? h("span", { class: "muted mono small" }, ` ${c.code}`) : null, c.active ? null : h("span", { class: "badge" }, " Inactive")) },
        manage ? { label: "Orders shipped", align: "right", render: (c) => String((totals.get(c.id) || {}).orders || 0) } : null,
        manage ? { label: "Units", align: "right", render: (c) => String((totals.get(c.id) || {}).units || 0) } : null,
        manage ? {
          label: "To bill", align: "right",
          render: (c) => {
            const t = totals.get(c.id);
            return t && t.rates_set ? fmtCents(t.total_cents) : h("span", { class: "muted" }, "No rates");
          },
        } : null,
        { label: "", render: (c) => h("a", { class: "btn btn-sm", href: `#/orders?client=${c.id}` }, "Orders") },
      ].filter(Boolean), clients, {
        empty: "No clients yet. If you're a 3PL, add the brands you ship for.",
        onRow: manage ? (c) => { location.hash = `#/clients/${c.id}?month=${month}`; } : null,
      })),
  ]);
  if (wantsNew(params)) editClient(null, reload);
}

const RATE_FIELDS = [
  ["monthly_fee", "Monthly account fee"],
  ["per_order", "Per order shipped"],
  ["per_unit", "Per unit picked"],
  ["per_extra_box", "Per extra box"],
  ["per_insert", "Per insert"],
  ["per_return", "Per return checked in"],
  ["per_receive_unit", "Per unit received"],
];

export async function clientView(id, params = new URLSearchParams()) {
  const month = params.get("month") || thisMonth();
  const clients = await api("/api/clients");
  const c = clients.find((x) => x.id === id);
  if (!c) throw new Error("Client not found");
  const [logins, st] = await Promise.all([api(`/api/clients/${id}/users`), api(`/api/clients/${id}/statement?month=${month}`)]);
  const reload = () => clientView(id, params).catch(fail);
  const rateInputs = RATE_FIELDS.map(([key, label]) => {
    const input = h("input", { class: "input input-qty", type: "number", min: "0", step: "0.01", value: c.rates[key] ? (c.rates[key] / 100).toFixed(2) : "" });
    return [key, label, input];
  });
  const email = h("input", { class: "input", type: "email", placeholder: "name@brand.com" });
  const picker = h("input", { class: "input input-inline", type: "month", value: month, onchange: (e) => { location.hash = `#/clients/${id}?month=${e.target.value}`; } });
  layout("#/clients", [
    h("a", { href: "#/clients", class: "back-link" }, "← Clients"),
    pageHeader(c.name, c.contact_email || "",
      h("button", { class: "btn", onclick: () => editClient(c, reload) }, "Edit"),
      h("a", { class: "btn", href: `#/orders?client=${id}` }, "Orders")),
    h("div", { class: "grid-main" },
      card(`Statement · ${st.month}`,
        h("div", { class: "row" }, h("span", { class: "muted" }, "Month"), picker,
          h("button", { class: "btn btn-sm", onclick: () => download(`/api/clients/${id}/statement.csv?month=${month}`, `autorack-${c.code || c.name}-${month}.csv`) }, "Download CSV")),
        table([
          { label: "Item", render: (l) => l.label },
          { label: "Quantity", align: "right", render: (l) => String(l.quantity) },
          { label: "Rate", align: "right", render: (l) => (l.rate_cents ? fmtCents(l.rate_cents) : h("span", { class: "muted" }, "–")) },
          { label: "Amount", align: "right", render: (l) => fmtCents(l.amount_cents) },
        ], st.lines),
        h("p", { class: "statement-total" }, "Total: ", h("strong", null, fmtCents(st.total_cents))),
        h("p", { class: "muted small" }, "Counted from the scan records: orders out the door this month, units verified, extra boxes, inserts packed, returns and receipts finished.")),
      h("div", { class: "stack-lg" },
        card("Rates",
          h("div", { class: "settings-list" }, ...rateInputs.map(([, label, input]) => h("label", { class: "row" }, h("span", { class: "rate-label" }, label), "$", input))),
          h("button", {
            class: "btn",
            onclick: () => {
              const rates = {};
              for (const [key, , input] of rateInputs) if (input.value) rates[key] = Math.round(Number(input.value) * 100);
              api(`/api/clients/${id}`, { method: "PATCH", body: { rates } }).then(() => { toast("Rates saved", "ok"); reload(); }, fail);
            },
          }, "Save rates")),
        card("Portal logins",
          h("p", { class: "muted small" }, "People at the brand who can sign in (with Google) to follow this client's orders, tracking, proofs, returns, reports and statements. Nothing else."),
          table([
            {
              label: "Login",
              render: (u) => h("div", null, u.email,
                h("div", { class: "muted small" }, `${u.name ? `${u.name} · ` : ""}signed in ${u.last_login_at ? fmtAgo(u.last_login_at) : "never"}`)),
            },
            {
              label: "",
              render: (u) => (u.active ? h("button", {
                class: "btn btn-sm btn-ghost",
                onclick: async () => {
                  if (!(await confirmDialog("Remove this login?", `${u.email} won't be able to sign in to the portal any more.`, { confirmLabel: "Remove", danger: true }))) return;
                  api(`/api/clients/${id}/users/${u.id}`, { method: "DELETE" }).then(reload, fail);
                },
              }, "Remove") : h("span", { class: "muted" }, "Removed")),
            },
          ], logins, { empty: "No portal logins yet." }),
          h("form", {
            class: "row",
            onsubmit: (e) => {
              e.preventDefault();
              if (!email.value.trim()) return;
              api(`/api/clients/${id}/users`, { method: "POST", body: { email: email.value.trim() } })
                .then(() => { toast("Invite sent. They sign in with Google.", "ok"); reload(); }, fail);
            },
          }, email, h("button", { class: "btn btn-primary", type: "submit" }, "Give access"))))),
  ]);
}

async function editClient(c, reload) {
  const name = h("input", { class: "input", value: c ? c.name : "", maxlength: "200" });
  const code = h("input", { class: "input mono", value: c && c.code ? c.code : "", placeholder: "Short code used in CSVs, e.g. GLOW", maxlength: "40" });
  const email = h("input", { class: "input", type: "email", value: c && c.contact_email ? c.contact_email : "" });
  const active = h("input", { type: "checkbox" });
  active.checked = !c || c.active;
  const ok = await dialog(c ? "Edit client" : "New client", (close) => h("form", {
    class: "stack",
    onsubmit: (e) => {
      e.preventDefault();
      close(true);
    },
  },
  h("label", null, "Name", name),
  h("label", null, "Code", code),
  h("label", null, "Contact email", email),
  c ? h("label", { class: "row check" }, active, "Active") : null,
  h("div", { class: "dialog-actions" },
    h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"),
    h("button", { class: "btn btn-primary", type: "submit" }, "Save"))));
  if (!ok) return;
  const body = { name: name.value.trim(), code: code.value.trim() };
  if (email.value.trim()) body.contact_email = email.value.trim();
  if (c) body.active = active.checked;
  try {
    await api(c ? `/api/clients/${c.id}` : "/api/clients", { method: c ? "PATCH" : "POST", body });
    toast("Client saved", "ok");
    reload();
  } catch (e) {
    fail(e);
  }
}
