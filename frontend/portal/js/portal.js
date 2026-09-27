// Client portal: a 3PL's brand follows its own orders. Read-only.
//   #/            orders (open / shipped / all, search)
//   #/orders/ID   one order: status, tracking, what was verified, box photos, evidence PDF
//   #/returns     returns checked in
//   #/report      the month in numbers, and the billing statement

import { ApiError, download as rawDownload, imageUrl, request } from "../../shared/api.js";
import { brandLockup, fmtDateTime, fmtCents, fmtNumber, h, mount, toast } from "../../shared/dom.js";
import { reportErrors } from "../../shared/report-errors.js";

reportErrors("portal");

const TOKEN_KEY = "ar.owner_token";
const state = { me: null };

function token() {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function toLogin() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
  location.replace("/app/login.html");
}

async function api(path) {
  try {
    return await request(path, { token: token() });
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      toLogin();
      return new Promise(() => {});
    }
    if (e instanceof ApiError && e.code === "not_a_client") {
      location.replace("/app/");
      return new Promise(() => {});
    }
    throw e;
  }
}

function fail(e) {
  toast(e instanceof ApiError ? e.message : String(e), "bad", 6000);
}

function tz() {
  return state.me ? state.me.warehouse.timezone : undefined;
}

const STATUS = {
  pending: "Waiting to be picked", in_progress: "Being picked", flagged: "On hold", completed: "Packed",
  shipped: "Shipped", cancelled: "Cancelled",
};

function statusBadge(s) {
  return h("span", { class: `badge badge-${s}` }, STATUS[s] || s);
}

function card(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", { class: "card-title" }, title) : null, ...children);
}

function table(cols, rows, { empty = "Nothing here yet.", onRow } = {}) {
  if (!rows.length) return h("div", { class: "empty" }, empty);
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", null, h("tr", null, ...cols.map((c) => h("th", { class: c.align === "right" ? "t-right" : null }, c.label)))),
    h("tbody", null, ...rows.map((r) => h("tr", { class: onRow ? "clickable" : null, onclick: onRow ? () => onRow(r) : null },
      ...cols.map((c) => h("td", { class: c.align === "right" ? "t-right" : null }, c.render(r))))))));
}

function layout(active, content) {
  const tabs = [["#/", "Orders"], ["#/returns", "Returns"], ["#/report", "Reports & billing"]];
  mount(document.getElementById("app"),
    h("div", { class: "portal" },
      h("header", { class: "portal-head" },
        h("div", { class: "portal-brand" }, brandLockup({ href: "#/" }),
          h("span", { class: "portal-who" }, h("strong", null, state.me.client.name), " at ", state.me.warehouse.name)),
        h("nav", { class: "portal-nav" }, ...tabs.map(([href, label]) =>
          h("a", { href, class: ["portal-tab", active === href && "active"] }, label))),
        h("div", { class: "portal-user" }, h("span", { class: "muted small" }, state.me.user.email),
          h("button", {
            class: "link-btn small",
            onclick: async () => {
              await request("/api/auth/logout", { method: "POST", token: token() }).catch(() => {});
              toLogin();
            },
          }, "Sign out"))),
      h("main", { class: "portal-main", id: "main" }, content)));
}

// ---------------------------------------------------------------------------

async function ordersView(params, kind = "pick") {
  const status = params.get("status") || (kind === "pick" ? "open" : "all");
  const q = params.get("q") || "";
  const host = h("div", null, h("div", { class: "skeleton" }));
  const base = kind === "pick" ? "#/" : "#/returns";
  const search = h("input", { class: "input", type: "search", placeholder: "Order #, customer or tracking", value: q });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") location.hash = `${base}?${new URLSearchParams({ status, q: search.value })}`;
  });
  layout(base, [
    h("div", { class: "page-head" }, h("div", null,
      h("h1", null, kind === "pick" ? "Your orders" : "Returns"),
      h("p", { class: "muted" }, kind === "pick"
        ? "Every item is scanned and checked against the order before it's packed. Open an order for its tracking and proof."
        : "Parcels that came back, checked item by item against what shipped."))),
    h("div", { class: "toolbar" },
      kind === "pick" ? h("div", { class: "tabs" }, ...[["open", "In progress"], ["shipped", "Shipped"], ["all", "All"]].map(([v, label]) =>
        h("a", { class: ["tab", status === v && "active"], href: `${base}?status=${v}` }, label))) : h("div"),
      search),
    card(null, host),
  ]);
  const r = await api(`/api/portal/orders?${new URLSearchParams({ status, kind, q, limit: "100" })}`);
  mount(host, table([
    { label: "Order", render: (o) => h("span", null, o.rush ? h("span", { class: "badge badge-rush" }, "Rush") : null, h("span", { class: "mono strong" }, o.number || o.id.slice(0, 8))) },
    { label: "Customer", render: (o) => o.customer || h("span", { class: "muted" }, "–") },
    { label: "Status", render: (o) => statusBadge(o.status) },
    { label: kind === "pick" ? "Units" : "Counted", align: "right", render: (o) => `${o.units_scanned}/${o.units_expected}` },
    { label: "Tracking", render: (o) => (o.tracking_number ? h("span", { class: "mono" }, `${o.carrier ? `${o.carrier} ` : ""}${o.tracking_number}`) : h("span", { class: "muted" }, "–")) },
    { label: kind === "pick" ? "Shipped" : "Received", render: (o) => h("span", { class: "muted" }, o.shipped_at ? fmtDateTime(o.shipped_at, tz()) : fmtDateTime(o.created_at, tz())) },
  ], r.orders, { empty: "No orders here.", onRow: (o) => { location.hash = `#/orders/${o.id}`; } }));
}

async function orderView(id) {
  const o = await api(`/api/portal/orders/${id}`);
  const back = o.kind === "return" ? "#/returns" : "#/";
  const photoIds = o.boxes.length > 1 ? o.boxes.flatMap((b) => b.photos) : o.pack_photos;
  const photos = photoIds.map((pid) => {
    const img = h("img", { class: "proof-photo", alt: "The packed box" });
    imageUrl(`/api/portal/photos/${pid}`, token()).then((u) => { img.src = u; }, () => img.remove());
    return h("figure", null, img);
  });
  const trace = (u) => [u.lot && `Lot ${u.lot}`, u.serial && `S/N ${u.serial}`, u.expiry && `Exp ${u.expiry}`].filter(Boolean).join(" · ");
  const traced = o.units.some((u) => u.lot || u.serial || u.expiry);
  layout(back, [
    h("a", { href: back, class: "back-link" }, "← Back"),
    h("div", { class: "page-head" },
      h("div", null,
        h("h1", { class: "row" }, h("span", { class: "mono" }, o.number || "Order"), statusBadge(o.status)),
        h("p", { class: "muted" }, o.customer ? [h("strong", null, o.customer), " · "] : null,
          `${o.units_scanned}/${o.units_expected} units verified by scan · created ${fmtDateTime(o.created_at, tz())}`,
          o.shipped_at ? ` · shipped ${fmtDateTime(o.shipped_at, tz())}` : "")),
      o.kind === "pick" && ["completed", "shipped"].includes(o.status)
        ? h("button", {
          class: "btn btn-primary",
          onclick: () => rawDownload(`/api/portal/orders/${id}/claim.pdf`, token(), `evidence-${o.number || id.slice(0, 8)}.pdf`).catch(fail),
        }, "Download evidence (PDF)")
        : null),
    o.boxes.length ? card(o.boxes.length > 1 ? `Shipped in ${o.boxes.length} boxes` : "Tracking",
      h("ul", { class: "plain-list" }, ...o.boxes.map((b) => h("li", null, o.boxes.length > 1 ? `Box ${b.box}: ` : "",
        h("span", { class: "mono strong" }, `${b.carrier ? `${b.carrier} ` : ""}${b.tracking_number}`))))) : null,
    o.errors_caught ? h("div", { class: "banner banner-info" },
      `${o.errors_caught} wrong item${o.errors_caught === 1 ? " was" : "s were"} caught by scan and put back before packing.`) : null,
    card("Items",
      table([
        { label: "Item", render: (l) => l.description || h("span", { class: "mono" }, l.barcode) },
        { label: "SKU", render: (l) => h("span", { class: "mono" }, l.sku || "–") },
        { label: "Ordered", align: "right", render: (l) => String(l.ordered) },
        { label: "Verified", align: "right", render: (l) => (l.verified >= l.ordered ? h("span", { class: "ok-text" }, `✓ ${l.verified}`) : String(l.verified)) },
        { label: "Not shipped", align: "right", render: (l) => (l.short ? String(l.short) : "") },
      ], o.lines)),
    photos.length ? card("The packed box", h("div", { class: "proof-photos" }, ...photos)) : null,
    o.inserts.length ? card("Inserts", h("ul", { class: "plain-list" }, ...o.inserts.map((i) => h("li", null, i.done ? "✓ " : "– ", i.name)))) : null,
    card("Every unit, as scanned",
      table([
        { label: "Time", render: (u) => h("span", { class: "muted" }, fmtDateTime(u.at, tz())) },
        { label: "Item", render: (u) => `${u.item || "–"}${u.quantity > 1 ? ` × ${u.quantity}` : ""}` },
        { label: "Barcode", render: (u) => (u.confirmed ? h("span", { class: "muted" }, "No barcode: checked by hand") : h("span", { class: "mono" }, u.barcode)) },
        traced ? { label: "Lot / serial / expiry", render: (u) => h("span", { class: "mono" }, trace(u)) } : null,
      ].filter(Boolean), o.units, { empty: "Nothing scanned yet." })),
  ]);
}

async function reportView(params) {
  const now = new Date();
  const month = params.get("month") || `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const picker = h("input", { class: "input input-inline", type: "month", value: month, onchange: (e) => { location.hash = `#/report?month=${e.target.value}`; } });
  const host = h("div", null, h("div", { class: "skeleton" }));
  layout("#/report", [
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Reports & billing"),
      h("p", { class: "muted" }, "Your month at the warehouse, from the scan records.")), picker),
    host,
  ]);
  const [r, st] = await Promise.all([api(`/api/portal/report?month=${month}`), api(`/api/portal/statement?month=${month}`)]);
  const tile = (label, value, hint) => h("div", { class: "tile" }, h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value), hint ? h("div", { class: "tile-hint" }, hint) : null);
  const onTime = r.on_time + r.late ? Math.round((100 * r.on_time) / (r.on_time + r.late)) : null;
  mount(host,
    h("div", { class: "tiles" },
      tile("Orders shipped", fmtNumber(r.orders_shipped)),
      tile("Units shipped", fmtNumber(r.units_shipped)),
      tile("Shipped on time", onTime === null ? "–" : `${onTime}%`, r.late ? `${r.late} late` : "Against ship-by dates and cutoffs"),
      tile("Wrong items caught", fmtNumber(r.wrong_items_caught), "Stopped by scan before packing"),
      tile("Returns checked in", fmtNumber(r.returns)),
      tile("Units received", fmtNumber(r.units_received)),
      tile("Open orders", fmtNumber(r.open_orders))),
    card(`Statement · ${st.month}`,
      st.rates_set
        ? table([
          { label: "Item", render: (l) => l.label },
          { label: "Quantity", align: "right", render: (l) => fmtNumber(l.quantity) },
          { label: "Rate", align: "right", render: (l) => fmtCents(l.rate_cents) },
          { label: "Amount", align: "right", render: (l) => fmtCents(l.amount_cents) },
        ], st.lines)
        : h("p", { class: "muted" }, "Your warehouse hasn't set up billing rates here."),
      st.rates_set ? h("p", { class: "statement-total" }, "Total: ", h("strong", null, fmtCents(st.total_cents))) : null,
      h("p", { class: "muted small" }, "Counts come from the warehouse's scan records. Your invoice comes from the warehouse.")));
}

// ---------------------------------------------------------------------------

async function route() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  try {
    let m;
    if ((m = /^\/orders\/([0-9a-f-]{36})$/.exec(path))) await orderView(m[1]);
    else if (path === "/returns") await ordersView(params, "return");
    else if (path === "/report") await reportView(params);
    else await ordersView(params);
  } catch (e) {
    fail(e);
  }
}

async function boot() {
  if (!token()) return toLogin();
  try {
    state.me = await api("/api/portal/me");
  } catch (e) {
    mount(document.getElementById("app"), h("div", { class: "auth-page" }, h("div", { class: "auth-card" },
      h("h1", null, "Can't open the portal"), h("p", { class: "muted" }, e.message),
      h("button", { class: "btn", onclick: toLogin }, "Sign in again"))));
    return;
  }
  document.title = `${state.me.client.name} · Autorack`;
  window.addEventListener("hashchange", route);
  route();
}

boot();
